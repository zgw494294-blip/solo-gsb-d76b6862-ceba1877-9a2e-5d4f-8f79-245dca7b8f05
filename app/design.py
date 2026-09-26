"""Demand-driven timetable design over a single line.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
A single line serves an ordered pattern of 2-20 stops; every trip runs the
same pattern, shifted so that its first-station departure (the "headway
minute") is one of the candidate minutes.  Exactly ``trip_count``
departures must be chosen from the candidates: adjacent chosen departures
must be at least ``min_interval`` minutes apart, and every demand batch
(``passengers`` people arriving at the first station at minute
``arrival``) boards the first chosen departure that does not precede its
arrival, waiting at most ``max_wait`` minutes.  The goal is to minimise
the passenger-weighted total waiting time; ties are broken by the
lexicographically smallest ascending departure list.

Solution
--------
Dynamic programming over the sorted candidates.  ``dp[k][i]`` holds the
best label ``(cost, departures)`` for choosing ``k`` departures among the
candidates up to ``c[i]`` with ``c[i]`` chosen as the k-th, serving every
batch that arrived no later than ``c[i]``.  Choosing ``c[i]`` right after
``c[p]`` assigns every batch with arrival in ``(c[p], c[i]]`` to ``c[i]``;
the transition is legal only when the gap respects ``min_interval`` and no
batch arrives in ``(c[p], c[i] - max_wait)`` (those would wait longer than
allowed).  Batch counts and weighted arrival sums are bucketed per minute,
so each transition cost is O(1); the answer is the best ``dp[K][i]`` with
``c[i]`` at or beyond the last batch arrival.  With at most 60 candidates
and 12 departures the table holds 720 states with at most 60 predecessors
each.
"""

from __future__ import annotations

from typing import Any, Sequence

MAX_MINUTE = 2879  # 48 hours in minutes


def _best_departures(
    candidates: Sequence[int],
    arrivals: Sequence[int],
    parties: Sequence[int],
    trip_count: int,
    min_interval: int,
    max_wait: int,
) -> tuple[list[int], int] | None:
    """Return ``(departures, total_wait)`` of the optimal choice, or None.

    ``candidates`` are the distinct candidate headway minutes; ``arrivals``
    and ``parties`` describe the demand batches.  The returned departure
    list is ascending, has exactly ``trip_count`` entries, keeps adjacent
    departures at least ``min_interval`` apart and lets every batch board
    within ``max_wait`` minutes; it minimises the passenger-weighted total
    wait, with ties broken by the lexicographically smallest list.
    """
    c = sorted(candidates)
    n = len(c)
    k_total = trip_count
    if k_total > n:
        return None

    # Bucket the demand per minute; prefix arrays P/W give, for any minute
    # t, the passenger count and the passenger-weighted arrival sum of all
    # batches arriving strictly before minute t.
    count_at = [0] * (MAX_MINUTE + 1)
    for arrival, party in zip(arrivals, parties):
        count_at[arrival] += party
    P = [0] * (MAX_MINUTE + 2)
    W = [0] * (MAX_MINUTE + 2)
    for t in range(MAX_MINUTE + 1):
        P[t + 1] = P[t] + count_at[t]
        W[t + 1] = W[t] + count_at[t] * t

    max_arrival = max(arrivals)

    # dp[k][i]: best (cost, departures) with c[i] chosen as the k-th
    # departure and every batch arriving by c[i] served; None if impossible.
    dp: list[list[tuple[int, tuple[int, ...]] | None]] = [
        [None] * n for _ in range(k_total + 1)
    ]
    for i in range(n):
        ci = c[i]
        # First departure at ci: every batch arriving by ci rides it, so no
        # batch may arrive before ci - max_wait.
        if P[max(ci - max_wait, 0)] == 0:
            dp[1][i] = (ci * P[ci + 1] - W[ci + 1], (ci,))
    for k in range(2, k_total + 1):
        for i in range(n):
            ci = c[i]
            limit = ci - max_wait
            best: tuple[int, tuple[int, ...]] | None = None
            for p in range(i):
                prev = dp[k - 1][p]
                if prev is None:
                    continue
                if ci - c[p] < min_interval:
                    continue
                # Batches arriving in (c[p], ci) would wait past max_wait.
                if P[max(limit, 0)] - P[c[p] + 1] > 0:
                    continue
                # Batches arriving in (c[p], ci] board ci.
                add = ci * (P[ci + 1] - P[c[p] + 1]) - (W[ci + 1] - W[c[p] + 1])
                label = (prev[0] + add, prev[1] + (ci,))
                if best is None or label < best:
                    best = label
            dp[k][i] = best

    best = None
    for i in range(n):
        # The last departure must reach the last batch: batches arriving
        # after it could never board.
        if c[i] >= max_arrival and dp[k_total][i] is not None:
            label = dp[k_total][i]
            if best is None or label < best:
                best = label
    if best is None:
        return None
    total_wait, departures = best
    return list(departures), total_wait


def design_timetable(
    *,
    stops: Sequence[Any],
    candidates: Sequence[int],
    demand: Sequence[Any],
    trip_count: int,
    min_interval: int,
    max_wait: int,
) -> dict:
    """Design the line's timetable from the demand.

    ``stops`` items need ``.station``, ``.arrival`` and ``.departure`` (the
    offsets relative to the first-station departure); ``demand`` items need
    ``.arrival`` and ``.passengers`` (works with the pydantic models and
    with simple test doubles alike).

    Returns a dict with keys: feasible, departures, total_wait, stations,
    trips.  When feasible, ``stations``/``trips`` form a network usable
    with the journey endpoints, one trip ``design@<minute>`` per chosen
    departure; otherwise all lists are empty and ``total_wait`` is None.
    """
    stations = [stop.station for stop in stops]
    offsets = [(stop.arrival, stop.departure) for stop in stops]
    arrivals = [batch.arrival for batch in demand]
    parties = [batch.passengers for batch in demand]

    solved = _best_departures(
        candidates, arrivals, parties, trip_count, min_interval, max_wait
    )
    if solved is None:
        return {
            "feasible": False,
            "departures": [],
            "total_wait": None,
            "stations": [],
            "trips": [],
        }

    departures, total_wait = solved
    trips = [
        {
            "id": f"design@{minute}",
            "stops": [
                {
                    "station": station,
                    "arrival": minute + arrival_offset,
                    "departure": minute + departure_offset,
                }
                for station, (arrival_offset, departure_offset) in zip(
                    stations, offsets
                )
            ],
        }
        for minute in departures
    ]
    return {
        "feasible": True,
        "departures": departures,
        "total_wait": total_wait,
        "stations": stations,
        "trips": trips,
    }
