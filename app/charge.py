"""Minimum-cost EV charging plan for one vehicle's fixed trip chain.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
One electric vehicle must serve a fixed chain of trips in execution order.
Adjacent trips connect at a shared station — the previous trip's last stop
is the next trip's first stop — and the next first-station departure is
never earlier than the previous last-station arrival.  The battery holds
at most ``capacity`` units and starts with ``initial_charge``; every trip
consumes a fixed amount of energy and may only depart when the battery
covers its consumption.  Charging is possible **only** during the stop
between two consecutive trips, only at stations with a configured
charger, in whole units: at most ``rate * stop minutes`` units and never
above the battery capacity; each charged unit costs the station's unit
price.  The goal is the minimum total price for completing the whole
chain; ties take the lexicographically smallest charge list in connection
order.

Solution
--------
Dynamic programming over the chain.  ``dp[b]`` holds the best label
``(cost, charges)`` for finishing the already processed trips with
battery level ``b``; the first trip must be covered by the initial charge
alone.  A stop of ``m`` minutes at a station with rate ``r`` allows any
integer charge ``0 <= c <= min(r * m, capacity - b)``; the next trip then
needs ``b + c >= consumption`` and leaves ``b + c - consumption`` units.
Keeping only the best label per battery level is exact: the remainder of
the plan depends only on the current level, and the label order —
ascending cost, then lexicographically ascending charge tuple — is
prefix-monotone, so a dominated prefix can never lead to a better
completion.  With at most 12 trips, 61 battery levels and 61 charge
choices per state, the table stays tiny.
"""

from __future__ import annotations

from typing import Any, Sequence


def plan_charge(
    *,
    trips: Sequence[Any],
    trip_ids: Sequence[str],
    consumptions: dict[str, int],
    capacity: int,
    initial_charge: int,
    chargers: Sequence[Any],
) -> dict:
    """Plan the cheapest integer charging amounts along the trip chain.

    ``trips`` items need ``.id`` and ``.stops`` with ``.station``,
    ``.arrival`` and ``.departure``; ``chargers`` items need ``.station``,
    ``.rate`` and ``.price_cents`` (works with the pydantic models and
    with simple test doubles alike).  The request is assumed already
    validated: trip ids are distinct and exist, ``consumptions`` covers
    exactly the chained trips, adjacent trips connect at one station with
    a non-negative stop, and ``0 <= initial_charge <= capacity``.

    Returns a dict with keys: feasible, total_cents (None when
    infeasible) and charges (one entry per connection between consecutive
    trips, in execution order; empty when infeasible).
    """
    trip_index = {trip.id: trip for trip in trips}
    chain = [trip_index[trip_id] for trip_id in trip_ids]
    needs = [consumptions[trip_id] for trip_id in trip_ids]
    charger_at = {charger.station: charger for charger in chargers}

    infeasible = {"feasible": False, "total_cents": None, "charges": []}

    # The first trip runs on the initial charge alone: charging is only
    # possible between two trips.
    if initial_charge < needs[0]:
        return infeasible

    # dp[b]: best (total cost, charge tuple) of the processed prefix with
    # battery level b after the last processed trip.
    dp: dict[int, tuple[int, tuple[int, ...]]] = {
        initial_charge - needs[0]: (0, ())
    }
    for i in range(len(chain) - 1):
        arrival = chain[i].stops[-1].arrival
        departure = chain[i + 1].stops[0].departure
        charger = charger_at.get(chain[i].stops[-1].station)
        rate = charger.rate if charger is not None else 0
        price = charger.price_cents if charger is not None else 0
        limit = rate * (departure - arrival)
        need = needs[i + 1]
        nxt: dict[int, tuple[int, tuple[int, ...]]] = {}
        for battery, (cost, charges) in dp.items():
            top = min(limit, capacity - battery)
            for amount in range(top + 1):
                if battery + amount < need:
                    continue
                level = battery + amount - need
                label = (cost + amount * price, charges + (amount,))
                current = nxt.get(level)
                if current is None or label < current:
                    nxt[level] = label
        if not nxt:
            return infeasible
        dp = nxt

    cost, charges = min(dp.values())
    return {
        "feasible": True,
        "total_cents": cost,
        "charges": list(charges),
    }
