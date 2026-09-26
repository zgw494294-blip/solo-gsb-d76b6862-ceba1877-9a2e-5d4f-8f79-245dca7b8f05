"""Minimum trip cancellations breaking on-time reachability.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
Remove the fewest whole trips from the fixed-schedule network so that the
traveller can no longer get from ``origin`` to ``destination`` — first
boarding no earlier than ``earliest_departure``, arrival no later than
``arrival_deadline`` — under the usual boarding rules (staying on the
same trip needs no buffer, changing trips needs ``transfer_buffer``).
Cancelled trips vanish entirely; every remaining trip keeps its stops and
times.  Ties are broken by the lexicographically smallest ascending list
of cancelled trip ids.

Solution
--------
``find_journey`` replays the journey planner's edge rules as a Dijkstra
over ``(station, last_trip)`` states, pruning arrivals after the
deadline, and returns the trip set of one feasible journey (or ``None``).
The heap is ordered by ``(time, trips_used)`` so every state is settled
at its earliest feasible arrival — an earlier arrival at the same
``(station, last_trip)`` dominates a later one no matter how many trips
each used, which is what keeps a loop's later visit to a station from
shadowing an earlier, still-connectable arrival there.  Each state keeps
the best label seen so far — queued or settled — and a relaxation
records the parent pointer only when it strictly improves that label, so
the reconstructed chain always belongs to the state's best label: a late
arrival's prefix can never be spliced onto a continuation that is
feasible only from an earlier arrival at the same state.  The
fewest-trips tiebreak keeps the reconstructed journey short, which keeps
the recursion below narrow.

The minimum number of cancellations is the minimum hitting set over the
feasible journey trip-sets: every solution must cancel at least one trip
of any feasible journey ``J``, so
``h(active) = min over t in J of 1 + h(active \\ {t})``.  Memoised on the
active-trip bitmask (at most 16 trips), each subset is solved at most
once.  The lexicographically smallest ascending id list among the minimum
solutions is then built greedily: scanning ids in ascending order, an id
is cancelled iff the remaining network can still be disconnected within
the remaining budget — exactly what the same memoised solver answers.
"""

from __future__ import annotations

import heapq
from functools import lru_cache
from typing import Any, Optional, Sequence


def min_disruption(
    *,
    trips: Sequence[Any],
    origin: str,
    destination: str,
    earliest_departure: int,
    arrival_deadline: int,
    transfer_buffer: int,
) -> dict:
    """Find the fewest trip cancellations that break on-time reachability.

    ``trips`` items need ``.id`` and ``.stops``; each stop needs
    ``.station``, ``.arrival`` and ``.departure`` (works with the pydantic
    models and with simple test doubles alike).

    Returns a dict with keys: baseline_reachable, minimum_cancellations,
    cancelled_ids (ascending; the lexicographically smallest list among
    minimum-cardinality solutions).  When the original timetable cannot
    reach the destination by the deadline, returns ``False``, 0 and an
    empty list.
    """
    n = len(trips)
    stations = [[s.station for s in t.stops] for t in trips]
    departures = [[s.departure for s in t.stops] for t in trips]
    arrivals = [[s.arrival for s in t.stops] for t in trips]

    @lru_cache(maxsize=None)
    def find_journey(active_mask: int) -> Optional[frozenset]:
        """Trip indices of one feasible journey over the active trips.

        ``None`` when the destination is unreachable by the deadline.
        """
        # Boarding events per station: station -> [(departure, trip, stop)]
        deps: dict[str, list[tuple[int, int, int]]] = {}
        for t in range(n):
            if not (active_mask >> t) & 1:
                continue
            for s_idx in range(len(stations[t]) - 1):  # cannot board at last
                deps.setdefault(stations[t][s_idx], []).append(
                    (departures[t][s_idx], t, s_idx)
                )

        start = (origin, -1)
        # Best label known per (station, last_trip), queued or settled:
        # (time, trips_used).  Keeping one label per state is sound only
        # because an earlier arrival on the same last trip dominates a
        # later one for every possible continuation (buffer or none),
        # regardless of how many trips either label used.
        best: dict[tuple[str, int], tuple[int, int]] = {
            start: (earliest_departure, 0)
        }
        parent: dict[tuple[str, int], Optional[tuple]] = {start: None}
        # Heap entries: (time, trips_used, station, last_trip).  Time first
        # makes this a true earliest-arrival search even when trips revisit
        # stations; trips_used only orders otherwise identical labels.
        heap = [(earliest_departure, 0, origin, -1)]
        goal = None
        while heap:
            tm, used, station, last = heapq.heappop(heap)
            key = (station, last)
            if best.get(key) != (tm, used):
                continue  # stale: a better label superseded it while queued
            if station == destination:
                goal = key
                break
            for dep, t, s_idx in deps.get(station, ()):
                # First boarding / staying on the same trip needs no buffer.
                ready = tm if last in (-1, t) else tm + transfer_buffer
                if dep < ready:
                    continue
                arr = arrivals[t][s_idx + 1]
                if arr > arrival_deadline:
                    continue
                nxt = (stations[t][s_idx + 1], t)
                label = (arr, used + (0 if t == last else 1))
                cur = best.get(nxt)
                if cur is not None and cur <= label:
                    continue
                # Relax only on a strict improvement over the best label
                # known so far — queued or settled — so the parent chain
                # always belongs to that label.  Comparing against settled
                # labels only would let a late arrival overwrite the parent
                # of a better, still-queued label, and reconstruction could
                # then splice the late arrival's prefix onto a continuation
                # that is feasible only from the earlier one.
                best[nxt] = label
                parent[nxt] = (key, t)
                heapq.heappush(heap, (label[0], label[1], nxt[0], t))
        if goal is None:
            return None
        journey: set[int] = set()
        key = goal
        while parent[key] is not None:
            prev, t = parent[key]
            journey.add(t)
            key = prev
        return frozenset(journey)

    @lru_cache(maxsize=None)
    def solve(active_mask: int) -> int:
        """Minimum further cancellations disconnecting the active trips."""
        journey = find_journey(active_mask)
        if journey is None:
            return 0
        best = n
        for t in journey:
            best = min(best, 1 + solve(active_mask & ~(1 << t)))
            if best == 1:  # cannot do better while journeys remain
                break
        return best

    full = (1 << n) - 1
    if find_journey(full) is None:
        return {
            "baseline_reachable": False,
            "minimum_cancellations": 0,
            "cancelled_ids": [],
        }

    minimum = solve(full)

    # Lexicographically smallest ascending id list among minimum solutions:
    # scanning ids in ascending order, cancel an id iff the remaining
    # network can still be disconnected within the remaining budget.
    cancelled: list[int] = []
    cancelled_mask = 0
    remaining = minimum
    for t in sorted(range(n), key=lambda i: trips[i].id):
        if remaining == 0:
            break
        if solve(full & ~cancelled_mask & ~(1 << t)) <= remaining - 1:
            cancelled.append(t)
            cancelled_mask |= 1 << t
            remaining -= 1

    return {
        "baseline_reachable": True,
        "minimum_cancellations": minimum,
        "cancelled_ids": sorted(trips[t].id for t in cancelled),
    }
