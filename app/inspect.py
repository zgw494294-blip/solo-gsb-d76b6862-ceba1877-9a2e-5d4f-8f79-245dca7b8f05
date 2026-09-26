"""Mileage-based inspection planning for one vehicle's fixed trip chain.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
One vehicle must serve a fixed chain of trips in execution order.
Adjacent trips connect at a shared station — the previous trip's last stop
is the next trip's first stop — and the next first-station departure is
never earlier than the previous last-station arrival.  The vehicle
accumulates mileage: it starts with ``initial_km`` kilometres already on
the clock and every trip adds its own distance; after **every** trip the
accumulated mileage must not exceed ``limit_km``.  An **inspection**
resets the accumulated mileage to zero.  It is possible **only** between
two consecutive trips, at the connection station, and only when that
station has a workshop whose open interval ``[open, close)`` can host the
whole job: the ``duration_minutes`` of work must fit inside the
intersection of the stop (previous arrival to next departure) and the
open interval.  Each inspection costs the workshop's flat ``cost_cents``.
The goals, in order: minimise the number of inspections, then the total
cost, then take the lexicographically smallest list of connection indices
(0-based, in connection order).

Solution
--------
Dynamic programming over the chain.  ``dp[km]`` holds the best label
``(inspections, cost, indices)`` for finishing the already processed
trips with ``km`` accumulated since the last inspection.  Processing trip
``i`` adds its distance (states beyond ``limit_km`` die) and then either
keeps the mileage or — when connection ``i`` can host an inspection —
resets it to zero at the workshop's price.  Keeping only the best label
per mileage level is exact: the remainder of the plan depends only on the
current mileage, and the label order — ascending inspection count, then
ascending cost, then lexicographically ascending index tuple — is
prefix-monotone, so a dominated prefix can never lead to a better
completion.  With at most 40 trips and 301 mileage levels the table
stays tiny.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence


def plan_inspect(
    *,
    trips: Sequence[Any],
    trip_ids: Sequence[str],
    trip_km: dict[str, int],
    limit_km: int,
    initial_km: int,
    workshops: Sequence[Any],
) -> dict:
    """Plan the inspections of one vehicle along its fixed trip chain.

    ``trips`` items need ``.id`` and ``.stops`` with ``.station``,
    ``.arrival`` and ``.departure``; ``workshops`` items need ``.station``,
    ``.open``, ``.close``, ``.duration_minutes`` and ``.cost_cents`` (works
    with the pydantic models and with simple test doubles alike).  The
    request is assumed already validated: trip ids are distinct and exist,
    ``trip_km`` covers exactly the chained trips, adjacent trips connect
    at one station with a non-negative stop, workshop stations are unique
    and declared, and ``0 <= initial_km <= limit_km``.

    Returns a dict with keys: feasible, inspections (one
    {"connection_index", "station"} entry per inspection, in connection
    order; empty when infeasible) and total_cents (None when infeasible).
    """
    trip_index = {trip.id: trip for trip in trips}
    chain = [trip_index[trip_id] for trip_id in trip_ids]
    distances = [trip_km[trip_id] for trip_id in trip_ids]
    workshop_at = {workshop.station: workshop for workshop in workshops}

    infeasible = {"feasible": False, "inspections": [], "total_cents": None}

    # hostable[i]: the workshop's price when connection i (between trip i
    # and trip i + 1) can host a full inspection, else None.  The job of
    # ``duration_minutes`` must fit inside [arrival, departure] as well as
    # inside [open, close), so it can start at
    # max(arrival, open) and must end by min(departure, close).
    n = len(chain)
    hostable: list[Optional[int]] = []
    for i in range(n - 1):
        workshop = workshop_at.get(chain[i].stops[-1].station)
        if workshop is None:
            hostable.append(None)
            continue
        start = max(chain[i].stops[-1].arrival, workshop.open)
        end = min(chain[i + 1].stops[0].departure, workshop.close)
        if start + workshop.duration_minutes <= end:
            hostable.append(workshop.cost_cents)
        else:
            hostable.append(None)

    # dp[km]: best (inspections, total cost, index tuple) of the processed
    # prefix with ``km`` accumulated since the last inspection.
    dp: dict[int, tuple[int, int, tuple[int, ...]]] = {
        initial_km: (0, 0, ())
    }
    for i in range(n):
        nxt: dict[int, tuple[int, int, tuple[int, ...]]] = {}
        for km, (count, cost, indices) in dp.items():
            driven = km + distances[i]
            if driven > limit_km:
                continue
            # No inspection after trip i: carry the mileage on.
            label = (count, cost, indices)
            current = nxt.get(driven)
            if current is None or label < current:
                nxt[driven] = label
            # Inspect at connection i (only between two trips): reset.
            if i < n - 1 and hostable[i] is not None:
                label = (count + 1, cost + hostable[i], indices + (i,))
                current = nxt.get(0)
                if current is None or label < current:
                    nxt[0] = label
        if not nxt:
            return infeasible
        dp = nxt

    _, cost, indices = min(dp.values())
    return {
        "feasible": True,
        "inspections": [
            {
                "connection_index": i,
                "station": chain[i].stops[-1].station,
            }
            for i in indices
        ],
        "total_cents": cost,
    }
