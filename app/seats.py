"""Maximum-passenger group-booking allocation under per-interval seat capacity.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
Every booking is a fixed, already validated ride chain for a party of
``passengers`` people and must be accepted or rejected as a whole.  An
accepted booking occupies ``passengers`` seats on every adjacent-stop
interval of every ride, and the total occupancy of each interval of a trip
must stay within that trip's ``capacity``.  The goal is to maximise the
number of accepted passengers; ties are broken by the lexicographically
smallest ascending list of accepted booking ids.

Solution
--------
Branch and bound over the bookings (at most 18), processed in ascending id
order so the accepted prefix is always sorted.  ``suffix[k]`` (the passenger
sum of bookings ``k:``) bounds what a node can still reach: a node is pruned
when the bound is below the incumbent, and when the bound *equals* the
incumbent the only completion that can still tie is "take every remaining
booking" (parties are positive, so skipping anything loses passengers),
which is checked directly.  Exploring the include branch first makes the
everything-fits case collapse to a single path, and interval loads are
maintained incrementally in one flat list.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def allocate_seats(
    *,
    trips: Sequence[Any],
    capacity: Mapping[str, int],
    bookings: Sequence[Any],
) -> dict:
    """Choose the bookings to accept.

    ``trips`` items need ``.id`` and ``.stops``; ``capacity`` maps every
    trip id to its positive per-interval seat capacity; ``bookings`` items
    need ``.id``, ``.passengers`` and ``.rides`` with ``.trip_id``,
    ``.board_index`` and ``.alight_index`` (works with the pydantic models
    and with simple test doubles alike).

    Returns a dict with keys: accepted_ids (ascending), rejected_ids
    (ascending), accepted_passengers.
    """
    # Flatten every trip's adjacent-stop intervals into one index space.
    offset: dict[str, int] = {}
    caps: list[int] = []
    for trip in trips:
        offset[trip.id] = len(caps)
        caps.extend([capacity[trip.id]] * (len(trip.stops) - 1))

    # Ascending id order keeps every accepted prefix sorted.
    ordered = sorted(bookings, key=lambda b: b.id)
    ids = [b.id for b in ordered]
    parties = [b.passengers for b in ordered]
    spans: list[list[int]] = []  # flat interval indices occupied per booking
    for b in ordered:
        span: list[int] = []
        for ride in b.rides:
            base = offset[ride.trip_id]
            span.extend(range(base + ride.board_index, base + ride.alight_index))
        spans.append(span)

    n = len(ordered)
    suffix = [0] * (n + 1)
    for k in range(n - 1, -1, -1):
        suffix[k] = suffix[k + 1] + parties[k]

    loads = [0] * len(caps)
    chosen: list[str] = []
    best_passengers = -1
    best_ids: tuple[str, ...] = ()

    def fits_all_from(k: int) -> bool:
        """True when every booking from k on can join the current loads."""
        extra: dict[int, int] = {}
        for j in range(k, n):
            for i in spans[j]:
                extra[i] = extra.get(i, 0) + parties[j]
        return all(loads[i] + add <= caps[i] for i, add in extra.items())

    def dfs(k: int, current: int) -> None:
        nonlocal best_passengers, best_ids
        bound = current + suffix[k]
        if bound < best_passengers:
            return
        if bound == best_passengers:
            # Only taking everything left can still tie the incumbent.
            if fits_all_from(k):
                candidate = tuple(chosen + ids[k:])
                if candidate < best_ids:
                    best_ids = candidate
            return
        if k == n:
            best_passengers = current
            best_ids = tuple(chosen)
            return
        party, span = parties[k], spans[k]
        if all(loads[i] + party <= caps[i] for i in span):
            for i in span:
                loads[i] += party
            chosen.append(ids[k])
            dfs(k + 1, current + party)
            chosen.pop()
            for i in span:
                loads[i] -= party
        dfs(k + 1, current)

    dfs(0, 0)

    accepted = set(best_ids)
    return {
        "accepted_ids": sorted(accepted),
        "rejected_ids": sorted(b.id for b in bookings if b.id not in accepted),
        "accepted_passengers": best_passengers,
    }
