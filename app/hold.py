"""Departure holding at a transfer station.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
At one transfer station a list of outgoing trips departs in order; each may
be **held** (its departure at this station delayed) by an integer number of
minutes between 0 and ``max_hold``.  Holding a trip shifts every later stop
of that trip (arrivals and departures) by the same amount, and no shifted
time may exceed 2879; after holding, consecutive departures at the station
must still be at least ``min_interval`` minutes apart.  A transfer group
with ``actual_arrival`` at the station **connects** to its target trip when
``actual_arrival + transfer_buffer`` does not exceed the trip's adjusted
departure.  The goals, in order: maximise the connected passengers, then
minimise the onboard delay ``sum(onboard_i * hold_i)``, then take the
lexicographically smallest hold list (in outgoing order).

Solution
--------
Dynamic programming over the outgoing sequence.  The interval constraint
only couples adjacent positions and every objective term is separable per
position, so ``dp[i][h]`` keeps the best label
``(passengers, onboard_delay, holds)`` for the first ``i + 1`` outgoing
trips with ``hold_i = h``: position ``i`` contributes the passengers of the
groups whose required hold ``actual_arrival + transfer_buffer - departure``
is at most ``h``, plus ``onboard_i * h`` delay.  Labels compare by
``(passengers, -onboard_delay)`` descending with ties broken by the
lexicographically smallest hold tuple — keeping only the best prefix per
``(i, h)`` is exact, because replacing a prefix by a better one with the
same last hold can only improve every completion.  With at most 10 outgoing
trips and 31 hold values each, the table holds 310 states with at most 31
predecessors apiece; the all-zero hold vector is always feasible, so a
solution always exists.
"""

from __future__ import annotations

from typing import Any, Sequence

MAX_MINUTE = 2879  # 48 hours in minutes


def _better(
    candidate: tuple[int, int, tuple[int, ...]],
    current: tuple[int, int, tuple[int, ...]],
) -> bool:
    """Whether ``candidate`` beats ``current`` under the decision order."""
    if (candidate[0], -candidate[1]) != (current[0], -current[1]):
        # More connected passengers, then less onboard delay.
        return (candidate[0], -candidate[1]) > (current[0], -current[1])
    # Full tie: the lexicographically smaller hold list wins.
    return candidate[2] < current[2]


def plan_hold(
    *,
    trips: Sequence[Any],
    outgoing: Sequence[Any],
    groups: Sequence[Any],
    transfer_buffer: int,
    max_hold: int,
    min_interval: int,
) -> dict:
    """Choose the hold minutes of every outgoing trip at the station.

    ``trips`` items need ``.id`` and ``.stops`` with ``.departure``;
    ``outgoing`` items need ``.trip_id``, ``.stop_index`` and ``.onboard``;
    ``groups`` items need ``.id``, ``.actual_arrival``, ``.trip_id`` and
    ``.passengers`` (works with the pydantic models and with simple test
    doubles alike).  The request is assumed already validated: outgoing
    trip ids are distinct and sorted by the departure at ``station``, the
    original departures keep ``min_interval`` and every group targets one
    of the outgoing trips.

    Returns a dict with keys: holds (one {"trip_id", "delay_minutes"} entry
    per outgoing trip, in outgoing order), connected_ids (sorted),
    connected_passengers and onboard_delay_minutes.
    """
    trip_index = {trip.id: trip for trip in trips}
    n = len(outgoing)

    departures: list[int] = []  # original departure at the station
    caps: list[int] = []  # largest hold keeping every shifted time <= 2879
    onboard: list[int] = []
    for entry in outgoing:
        trip = trip_index[entry.trip_id]
        departures.append(trip.stops[entry.stop_index].departure)
        # Times are non-decreasing along the trip, so the last stop's
        # departure is the largest shifted time of this trip.
        caps.append(min(max_hold, MAX_MINUTE - trip.stops[-1].departure))
        onboard.append(entry.onboard)

    # Group requirements per outgoing position: a group connects when the
    # hold reaches actual_arrival + transfer_buffer - departure.
    position_of = {entry.trip_id: i for i, entry in enumerate(outgoing)}
    required: list[list[tuple[int, int, str]]] = [[] for _ in range(n)]
    for group in groups:
        i = position_of[group.trip_id]
        need = group.actual_arrival + transfer_buffer - departures[i]
        required[i].append((need, group.passengers, group.id))

    # gain[i][h]: passengers connecting at position i with a hold of h.
    gain = [
        [
            sum(pax for need, pax, _ in required[i] if need <= h)
            for h in range(caps[i] + 1)
        ]
        for i in range(n)
    ]

    # dp[h]: best (passengers, onboard_delay, holds) of the processed
    # prefix ending with hold h at the current position.
    dp: dict[int, tuple[int, int, tuple[int, ...]]] = {
        h: (gain[0][h], onboard[0] * h, (h,)) for h in range(caps[0] + 1)
    }
    for i in range(1, n):
        base = departures[i] - departures[i - 1]
        nxt: dict[int, tuple[int, int, tuple[int, ...]]] = {}
        for h in range(caps[i] + 1):
            best: tuple[int, int, tuple[int, ...]] | None = None
            for prev_h, label in dp.items():
                if base + h - prev_h < min_interval:
                    continue
                candidate = (
                    label[0] + gain[i][h],
                    label[1] + onboard[i] * h,
                    label[2] + (h,),
                )
                if best is None or _better(candidate, best):
                    best = candidate
            if best is not None:
                nxt[h] = best
        dp = nxt

    best = next(iter(dp.values()))
    for label in dp.values():
        if _better(label, best):
            best = label
    connected_passengers, onboard_delay, holds = best

    connected_ids = sorted(
        group_id
        for i in range(n)
        for need, _, group_id in required[i]
        if need <= holds[i]
    )
    return {
        "holds": [
            {"trip_id": entry.trip_id, "delay_minutes": holds[i]}
            for i, entry in enumerate(outgoing)
        ],
        "connected_ids": connected_ids,
        "connected_passengers": connected_passengers,
        "onboard_delay_minutes": onboard_delay,
    }
