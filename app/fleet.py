"""Minimum-fleet assignment over fixed-schedule trips.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
Every trip must be served by exactly one vehicle, which it occupies from
the departure at its first stop to the arrival at its last stop.  After
finishing a trip, a vehicle may continue with another trip only when that
trip starts at the station where the previous one ended and departs no
earlier than the previous arrival plus ``turnaround_minutes``; empty
repositioning runs are never scheduled.  Single-trip vehicles are allowed
and the fleet size must be the global minimum.

Solution
--------
Build a directed graph on the trips with an edge ``i -> j`` when one
vehicle may serve trip ``j`` right after trip ``i``.  First departures
strictly increase along every edge (``dep_j >= arr_i + turnaround >=
arr_i > dep_i``), so the graph is acyclic and the answer is its minimum
path cover: ``n - |maximum matching|`` in the bipartite graph that places
every trip on both sides and keeps the same edges.  A deterministic
augmenting-path search (Kuhn) over canonically ordered vertices and
neighbours — trips sorted by ``(first departure, id)`` — yields the
matching; trips with no matched predecessor head the chains, and following
the successor pointers reconstructs each vehicle's run in execution order.
"""

from __future__ import annotations

from typing import Any, Sequence


def assign_fleet(*, trips: Sequence[Any], turnaround_minutes: int) -> dict:
    """Assign every trip to a vehicle using the fewest vehicles possible.

    ``trips`` items need ``.id`` and ``.stops``; each stop needs
    ``.station``, ``.arrival`` and ``.departure`` (works with the pydantic
    models and with simple test doubles alike).

    Returns a dict with keys: vehicle_count, vehicles (a list of
    {"trip_ids": [...]} entries in execution order; the list is sorted by
    first-trip departure time, then first-trip id).
    """
    n = len(trips)
    first_dep = [t.stops[0].departure for t in trips]
    last_arr = [t.stops[-1].arrival for t in trips]
    first_sta = [t.stops[0].station for t in trips]
    last_sta = [t.stops[-1].station for t in trips]

    # Canonical trip order: first departure, then id (ids are unique).
    order = sorted(range(n), key=lambda i: (first_dep[i], trips[i].id))

    # Bipartite edges left i -> right j when j may directly follow i.
    # first_dep[j] >= last_arr[i] + turnaround > first_dep[i], so j != i
    # and the graph is acyclic.
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in order:
        ready = last_arr[i] + turnaround_minutes
        for j in order:
            if first_dep[j] >= ready and last_sta[i] == first_sta[j]:
                adj[i].append(j)

    # Maximum matching: nxt[i] = j means one vehicle serves j right after i.
    nxt = [-1] * n
    prv = [-1] * n

    def augment(u: int, seen: set[int]) -> bool:
        for v in adj[u]:
            if v in seen:
                continue
            seen.add(v)
            if prv[v] == -1 or augment(prv[v], seen):
                prv[v] = u
                nxt[u] = v
                return True
        return False

    for u in order:
        augment(u, set())

    # Trips with no matched predecessor head the vehicle chains; visiting
    # heads in canonical order yields the required (departure, id) sorting.
    chains: list[list[int]] = []
    for i in order:
        if prv[i] == -1:
            chain = []
            u = i
            while u != -1:
                chain.append(u)
                u = nxt[u]
            chains.append(chain)

    return {
        "vehicle_count": len(chains),
        "vehicles": [
            {"trip_ids": [trips[u].id for u in chain]} for chain in chains
        ],
    }
