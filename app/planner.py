"""Earliest-arrival journey planner over fixed-schedule trips.

Pure standard-library module so it can be unit-tested without FastAPI.

Model
-----
A state is ``(station, last_trip)``: the traveller is at ``station`` having
last ridden ``last_trip`` (``-1`` at the origin before boarding anything).
Each state carries Pareto-optimal labels ``(time, transfers, trip_id_seq)``.

Edges ride one trip from the current stop to the *next* stop of that trip.
Riding on through intermediate stops is therefore a chain of zero-transfer
edges on the same trip, which is why "staying on the vehicle" never counts
as a transfer and never needs the transfer buffer.

Labels are popped from a heap ordered by ``(time, transfers, trip_id_seq)``
— exactly the required preference order (earliest arrival, then fewest
transfers, then lexicographically smallest trip-id sequence) — so the first
label popped for the destination is the optimal answer.
"""

from __future__ import annotations

import heapq
from typing import Any, Sequence

# A node is one label in the search graph:
# (station, last_trip_idx, time, transfers, trip_id_seq, path_key,
#  parent_node_idx, ride_trip_idx, board_stop_idx, alight_stop_idx)
Node = tuple


def plan_journey(
    *,
    trips: Sequence[Any],
    origin: str,
    destination: str,
    earliest_departure: int,
    transfer_buffer: int,
) -> dict:
    """Find the optimal journey.

    ``trips`` items need ``.id`` and ``.stops``; each stop needs
    ``.station``, ``.arrival`` and ``.departure`` (works with the pydantic
    models and with simple test doubles alike).

    Returns a dict with keys: reachable, arrival_time, transfers,
    trip_ids, segments.
    """
    # Boarding events per station: station -> [(departure, trip_idx, stop_idx)]
    departures: dict[str, list[tuple[int, int, int]]] = {}
    for t_idx, trip in enumerate(trips):
        for s_idx in range(len(trip.stops) - 1):  # cannot board at the last stop
            stop = trip.stops[s_idx]
            departures.setdefault(stop.station, []).append(
                (stop.departure, t_idx, s_idx)
            )

    nodes: list[Node] = [
        (origin, -1, earliest_departure, 0, (), (), -1, -1, -1, -1)
    ]
    # Heap entries: (time, transfers, trip_id_seq, path_key, node_idx).
    # The first three are the required preference order; path_key (the chain
    # of ridden edges) only orders fully equivalent itineraries so results
    # stay deterministic; node_idx guarantees a total order.
    heap: list[tuple] = [(earliest_departure, 0, (), (), 0)]
    # Settled Pareto labels per state: (station, last_trip_idx) -> labels
    settled: dict[tuple[str, int], list[tuple[int, int, tuple]]] = {}

    while heap:
        time, transfers, seq, _path_key, u = heapq.heappop(heap)
        station, last_trip = nodes[u][0], nodes[u][1]
        key = (station, last_trip)
        kept = settled.setdefault(key, [])
        # Skip if an already-settled label is at least as good on every
        # criterion (its extensions then dominate this label's extensions).
        if any(k[0] <= time and k[1] <= transfers and k[2] <= seq for k in kept):
            continue
        kept.append((time, transfers, seq))

        if station == destination:
            return _build_result(nodes, u, trips)

        for dep, t_idx, s_idx in departures.get(station, ()):
            if last_trip == -1 or t_idx == last_trip:
                ready = time  # first boarding / staying on the same trip
            else:
                ready = time + transfer_buffer  # changing trips
            if dep < ready:
                continue
            trip = trips[t_idx]
            nxt = trip.stops[s_idx + 1]
            if t_idx == last_trip:
                n_transfers, n_seq = transfers, seq
            else:
                # First boarding (last_trip == -1) is not a transfer.
                n_transfers = transfers + (0 if last_trip == -1 else 1)
                n_seq = seq + (trip.id,)
            v = len(nodes)
            path_key = nodes[u][5] + ((dep, t_idx, s_idx),)
            nodes.append(
                (nxt.station, t_idx, nxt.arrival, n_transfers, n_seq,
                 path_key, u, t_idx, s_idx, s_idx + 1)
            )
            heapq.heappush(heap, (nxt.arrival, n_transfers, n_seq, path_key, v))

    return {
        "reachable": False,
        "arrival_time": None,
        "transfers": 0,
        "trip_ids": [],
        "segments": [],
    }


def _build_result(nodes: list[Node], dest: int, trips: Sequence[Any]) -> dict:
    """Reconstruct the journey from the destination node back to the start."""
    rides: list[tuple[int, int, int]] = []  # (trip_idx, board_idx, alight_idx)
    u = dest
    while nodes[u][6] != -1:
        rides.append((nodes[u][7], nodes[u][8], nodes[u][9]))
        u = nodes[u][6]
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
        "reachable": True,
        "arrival_time": nodes[dest][2],
        "transfers": nodes[dest][3],
        "trip_ids": list(nodes[dest][4]),
        "segments": segments,
    }
