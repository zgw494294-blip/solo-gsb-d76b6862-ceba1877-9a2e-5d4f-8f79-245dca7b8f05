"""Two-traveller meeting planner over fixed-schedule trips.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
Two travellers start at different origin stations, each with their own
earliest departure minute, and move under the journey planner's boarding
rules (first boarding no earlier than the traveller's earliest
departure, staying on the same trip needs no buffer, changing trips
needs ``transfer_buffer``).  They must meet at one station that is
neither origin, each riding at least one trip.  The meeting time is the
later of the two arrivals; the earlier arrival waits, at most
``max_wait`` minutes.  The best plan is chosen by: earliest meeting
time, then shortest wait, then fewest total transfers, then meeting
station id, then the two travellers' trip-id sequences in request
order.

Solution
--------
Each traveller is explored with the planner's edge rules as a
multi-label search over ``(station, last_trip)`` states, but keeping
*every* distinct arrival time per state rather than Pareto-optimal
labels only: a later arrival at the meeting station can be strictly
better when it shortens the wait without moving the meeting time, so
the usual earlier-dominates-later pruning is unsound here.  One label
per ``(state, arrival_time)`` still suffices: continuation feasibility
depends only on the state and the time, so among labels sharing both,
fewer transfers and then the lexicographically smaller trip-id sequence
dominate for every continuation and every pairing.  Arrival times at
``(station, trip)`` are that trip's stop events there, so the label
count is bounded by the total number of stops.

Labels are settled in ``(time, transfers, trip_id_seq)`` heap order and
relaxations only ever come from strictly earlier labels, so the first
label settled for a ``(state, time)`` pair is already the best one and
later duplicates are skipped.  At every candidate meeting station the
two travellers' labels — collapsed to the best label per arrival time —
are paired within the ``max_wait`` window under the exact preference
key, and the global minimum over all candidate stations is the answer.
"""

from __future__ import annotations

import heapq
from bisect import bisect_left, bisect_right
from typing import Any, Sequence

# A settled label: (station, last_trip_idx, time, transfers, trip_id_seq,
#                   parent_node_idx, ride_trip_idx, board_stop_idx,
#                   alight_stop_idx)
Node = tuple


def plan_meet(
    *,
    trips: Sequence[Any],
    travelers: Sequence[tuple[str, int]],
    transfer_buffer: int,
    max_wait: int,
) -> dict:
    """Find the optimal meeting plan for the two travellers.

    ``trips`` items need ``.id`` and ``.stops``; each stop needs
    ``.station``, ``.arrival`` and ``.departure`` (works with the pydantic
    models and with simple test doubles alike).  ``travelers`` holds
    exactly two ``(origin, earliest_departure)`` pairs.

    Returns a dict with keys: reachable, meeting_station, meeting_time,
    travelers (two dicts with arrival_time, transfers, trip_ids and
    segments, in request order).
    """
    (origin_a, start_a), (origin_b, start_b) = travelers

    # Boarding events per station: station -> [(departure, trip_idx, stop_idx)]
    departures: dict[str, list[tuple[int, int, int]]] = {}
    for t_idx, trip in enumerate(trips):
        for s_idx in range(len(trip.stops) - 1):  # cannot board at the last stop
            stop = trip.stops[s_idx]
            departures.setdefault(stop.station, []).append(
                (stop.departure, t_idx, s_idx)
            )

    labels_a, nodes_a = _explore(
        trips, departures, origin_a, start_a, transfer_buffer
    )
    labels_b, nodes_b = _explore(
        trips, departures, origin_b, start_b, transfer_buffer
    )

    # Preference key: (meeting_time, wait, total_transfers, station,
    # trip_id_seq_a, trip_id_seq_b) — exactly the required order.
    best_key: tuple | None = None
    best: tuple | None = None  # (station, node_idx_a, node_idx_b)
    candidates = set(labels_a) & set(labels_b)
    candidates.discard(origin_a)
    candidates.discard(origin_b)
    for station in candidates:
        arrivals_a = _best_per_time(labels_a[station])
        arrivals_b = _best_per_time(labels_b[station])
        times_a = [label[0] for label in arrivals_a]
        for t_b, k_b, q_b, u_b in arrivals_b:
            lo = bisect_left(times_a, t_b - max_wait)
            hi = bisect_right(times_a, t_b + max_wait)
            for i in range(lo, hi):
                t_a, k_a, q_a, u_a = arrivals_a[i]
                key = (
                    max(t_a, t_b),
                    abs(t_a - t_b),
                    k_a + k_b,
                    station,
                    q_a,
                    q_b,
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best = (station, u_a, u_b)

    if best is None:
        return {
            "reachable": False,
            "meeting_station": None,
            "meeting_time": None,
            "travelers": [_empty_traveler(), _empty_traveler()],
        }

    station, u_a, u_b = best
    return {
        "reachable": True,
        "meeting_station": station,
        "meeting_time": best_key[0],
        "travelers": [
            _traveler_result(nodes_a, u_a, trips),
            _traveler_result(nodes_b, u_b, trips),
        ],
    }


def _explore(
    trips: Sequence[Any],
    departures: dict[str, list[tuple[int, int, int]]],
    origin: str,
    earliest_departure: int,
    transfer_buffer: int,
) -> tuple[dict[str, list[tuple]], list[Node]]:
    """Every useful arrival label of one traveller.

    Returns ``(labels, nodes)``: ``labels`` maps each station to the
    traveller's labels there — ``(time, transfers, trip_id_seq,
    node_idx)`` tuples in ``(time, transfers, trip_id_seq)`` order — and
    ``nodes`` holds the settled labels with parent pointers for segment
    reconstruction.  Only arrivals on an actual trip are recorded, so
    every label rides at least one trip.
    """
    nodes: list[Node] = []
    # Heap entries carry the arrival they produce: (time, transfers,
    # trip_id_seq, station, last_trip_idx, parent_idx, ride_trip_idx,
    # board_stop_idx, alight_stop_idx).  Only the first pop of each
    # (station, last_trip, time) is settled; later duplicates are worse
    # in transfers or trip-id sequence and are skipped.
    heap: list[tuple] = [
        (earliest_departure, 0, (), origin, -1, -1, -1, -1, -1)
    ]
    settled: set[tuple[str, int, int]] = set()
    labels: dict[str, list[tuple]] = {}

    while heap:
        (time, transfers, seq, station, last,
         parent, ride, board, alight) = heapq.heappop(heap)
        key = (station, last, time)
        if key in settled:
            continue
        settled.add(key)
        u = len(nodes)
        nodes.append(
            (station, last, time, transfers, seq, parent, ride, board, alight)
        )
        if last != -1:  # the origin node is not an arrival
            labels.setdefault(station, []).append((time, transfers, seq, u))

        for dep, t_idx, s_idx in departures.get(station, ()):
            if last == -1 or t_idx == last:
                ready = time  # first boarding / staying on the same trip
            else:
                ready = time + transfer_buffer  # changing trips
            if dep < ready:
                continue
            nxt = trips[t_idx].stops[s_idx + 1]
            if t_idx == last:
                n_transfers, n_seq = transfers, seq
            else:
                # First boarding (last == -1) is not a transfer.
                n_transfers = transfers + (0 if last == -1 else 1)
                n_seq = seq + (trips[t_idx].id,)
            heapq.heappush(
                heap,
                (nxt.arrival, n_transfers, n_seq, nxt.station, t_idx,
                 u, t_idx, s_idx, s_idx + 1),
            )

    return labels, nodes


def _best_per_time(labels: list[tuple]) -> list[tuple]:
    """Best label per arrival time, in ascending time order.

    The input is in ``(time, transfers, trip_id_seq)`` order (settle
    order), so the first label of each arrival time already has the
    fewest transfers and the smallest trip-id sequence — exactly the
    label any pairing prefers for that arrival time.
    """
    best: list[tuple] = []
    seen: set[int] = set()
    for label in labels:
        if label[0] in seen:
            continue
        seen.add(label[0])
        best.append(label)
    return best


def _empty_traveler() -> dict:
    return {"arrival_time": None, "transfers": 0, "trip_ids": [], "segments": []}


def _traveler_result(nodes: list[Node], dest: int, trips: Sequence[Any]) -> dict:
    """Reconstruct one traveller's itinerary to the meeting station."""
    rides: list[tuple[int, int, int]] = []  # (trip_idx, board_idx, alight_idx)
    u = dest
    while nodes[u][5] != -1:
        rides.append((nodes[u][6], nodes[u][7], nodes[u][8]))
        u = nodes[u][5]
    rides.reverse()

    # Merge chained rides on the same trip into single segments.
    merged: list[list[int]] = []
    for t_idx, b_idx, a_idx in rides:
        if merged and merged[-1][0] == t_idx and merged[-1][2] == b_idx:
            merged[-1][2] = a_idx
        else:
            merged.append([t_idx, b_idx, a_idx])

    segments = []
    for t_idx, b_idx, a_idx in merged:
        trip = trips[t_idx]
        board, alight = trip.stops[b_idx], trip.stops[a_idx]
        segments.append(
            {
                "trip_id": trip.id,
                "from": board.station,
                "to": alight.station,
                "departure": board.departure,
                "arrival": alight.arrival,
            }
        )

    # transfers / trip_ids come from the destination node's label so that
    # re-boarding the same trip never inflates the transfer count.
    return {
        "arrival_time": nodes[dest][2],
        "transfers": nodes[dest][3],
        "trip_ids": list(nodes[dest][4]),
        "segments": segments,
    }
