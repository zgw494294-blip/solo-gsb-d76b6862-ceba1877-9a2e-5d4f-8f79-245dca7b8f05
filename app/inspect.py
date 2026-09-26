"""Minimum-inspection mileage maintenance plan for one fixed trip chain.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
One vehicle must serve a fixed chain of trips in execution order.
Adjacent trips connect at a shared station — the previous trip's last
stop is the next trip's first stop — and the next first-station
departure is never earlier than the previous last-station arrival.
The vehicle accumulates mileage: it starts with ``initial_km`` and
every trip adds its ``trip_km``; after **every** trip the accumulated
mileage must not exceed ``limit_km``.  An inspection resets the
accumulated mileage to zero and is possible **only** during the stop
between two consecutive trips, only at a station with a configured
workshop, and only when the whole work duration fits inside the
intersection of the stop window (previous arrival to next departure)
and the workshop's open interval ``[open, close)``; each inspection
costs the workshop's price.  The goal is the minimum number of
inspections for completing the whole chain; ties take the minimum
total price, then the lexicographically smallest connection-index
list in connection order.

Solution
--------
Dynamic programming over the chain.  ``dp[m]`` holds the best label
``(inspections, cost, indices)`` for finishing the already processed
trips with accumulated mileage ``m``; the first trip must be covered
by the initial mileage alone.  At every connection the vehicle either
continues (mileage grows by the next trip's kilometres and must stay
within the limit) or, when a workshop fits the stop window, inspects
(mileage resets to zero before the next trip).  Keeping only the best
label per mileage level is exact: the remainder of the plan depends
only on the current mileage, and the label order — ascending
inspection count, then ascending cost, then lexicographically
ascending index tuple — is prefix-monotone, so a dominated prefix can
never lead to a better completion.  With at most 40 trips and 301
mileage levels, the table stays tiny.
"""

from __future__ import annotations

from typing import Any, Sequence


def plan_inspect(
    *,
    trips: Sequence[Any],
    trip_ids: Sequence[str],
    trip_km: dict[str, int],
    limit_km: int,
    initial_km: int,
    workshops: Sequence[Any],
) -> dict:
    """Plan the fewest, then cheapest, inspections along the trip chain.

    ``trips`` items need ``.id`` and ``.stops`` with ``.station``,
    ``.arrival`` and ``.departure``; ``workshops`` items need
    ``.station``, ``.open``, ``.close``, ``.minutes`` and
    ``.price_cents`` (works with the pydantic models and with simple
    test doubles alike).  The request is assumed already validated:
    trip ids are distinct and exist, ``trip_km`` covers exactly the
    chained trips, adjacent trips connect at one station with a
    non-negative stop, every workshop interval has ``open < close``,
    and ``0 <= initial_km <= limit_km``.

    Returns a dict with keys: feasible, inspections (one entry per
    chosen connection, in connection order, with ``index`` — the
    0-based connection after ``trip_ids[index]`` — and ``station``;
    empty when infeasible) and total_cents (None when infeasible).
    """
    trip_index = {trip.id: trip for trip in trips}
    chain = [trip_index[trip_id] for trip_id in trip_ids]
    kms = [trip_km[trip_id] for trip_id in trip_ids]
    workshop_at = {workshop.station: workshop for workshop in workshops}

    infeasible = {"feasible": False, "inspections": [], "total_cents": None}

    # The first trip runs on the initial mileage alone: inspection is
    # only possible between two trips.
    first = initial_km + kms[0]
    if first > limit_km:
        return infeasible

    # dp[m]: best (inspection count, total cost, index tuple) of the
    # processed prefix with accumulated mileage m after the last
    # processed trip.
    dp: dict[int, tuple[int, int, tuple[int, ...]]] = {first: (0, 0, ())}
    for i in range(len(chain) - 1):
        arrival = chain[i].stops[-1].arrival
        departure = chain[i + 1].stops[0].departure
        workshop = workshop_at.get(chain[i].stops[-1].station)
        fits = workshop is not None and (
            min(departure, workshop.close) - max(arrival, workshop.open)
            >= workshop.minutes
        )
        price = workshop.price_cents if fits else 0
        km = kms[i + 1]
        nxt: dict[int, tuple[int, int, tuple[int, ...]]] = {}
        for mileage, (count, cost, indices) in dp.items():
            # Continue without inspecting: the mileage keeps growing.
            level = mileage + km
            if level <= limit_km:
                label = (count, cost, indices)
                current = nxt.get(level)
                if current is None or label < current:
                    nxt[level] = label
            # Inspect at this connection: the mileage resets to zero.
            if fits and km <= limit_km:
                label = (count + 1, cost + price, indices + (i,))
                current = nxt.get(km)
                if current is None or label < current:
                    nxt[km] = label
        if not nxt:
            return infeasible
        dp = nxt

    count, cost, indices = min(dp.values())
    return {
        "feasible": True,
        "inspections": [
            {"index": i, "station": chain[i].stops[-1].station}
            for i in indices
        ],
        "total_cents": cost,
    }
