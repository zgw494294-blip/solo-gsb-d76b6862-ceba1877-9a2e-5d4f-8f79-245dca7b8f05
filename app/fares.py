"""Minimum-fare ticket optimisation over a fixed, validated ride sequence.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
A ticket is purchased at the departure of the *first* ride it covers and may
only cover a *consecutive* run of rides. It is usable on that run when:

* the run uses at most the ticket's ``max_rides`` rides, and
* every covered ride departs no later than the purchase time plus the
  ticket's ``valid_minutes``.

Every ride must be covered by exactly one ticket, and a ticket type may be
bought repeatedly.

Solution
--------
Partition dynamic programming over the rides (at most 20 rides, 12 ticket
types)::

    dp[i] = cheapest way to cover rides [0, i)

For each boundary ``i`` and each ticket, every feasible last block
``[j, i)`` ending at ``i`` gives the transition
``dp[i] = dp[j] + price(ticket)``. Labels are compared by
``(total price, number of tickets, tuple of ticket ids in ride order)`` —
exactly the required preference order — so the best label found for each
boundary is the optimal one. Feasibility of every block is O(1): ride
departures strictly increase (changing trips requires a positive transfer
buffer), so only the last ride's departure must fit inside the validity
window that starts at the first ride's departure.
"""

from __future__ import annotations

from typing import Any, Sequence

# Label on prefix boundary i (rides [0, i) covered):
# (total_price_cents, ticket_count, ticket_id_seq_in_ride_order,
#  parent_boundary); the last block of the label is rides [parent_boundary, i)
# covered by seq[-1].
Label = tuple


def quote_fares(
    *,
    rides: Sequence[Any],
    departures: Sequence[int],
    tickets: Sequence[Any],
) -> dict:
    """Price the cheapest valid ticket cover for the given rides.

    ``rides`` only provides the length; ``departures[k]`` is the departure
    minute of ride ``k`` at its boarding stop. ``tickets`` items need
    ``.id``, ``.price_cents``, ``.valid_minutes`` and ``.max_rides`` (works
    with the pydantic models and with simple test doubles alike).

    Returns a dict with keys: total_cents, tickets (a list of
    {"ticket_id", "covers_from", "covers_to"} entries in ride order).
    """
    n = len(rides)

    # One best label per prefix boundary; boundary 0 is the empty cover.
    labels: list[Label | None] = [(0, 0, (), -1)]

    for i in range(1, n + 1):
        candidates: list[Label] = []
        # Ticket types in id order makes iteration deterministic; the label
        # comparison below is the real arbiter of ties.
        for ticket in sorted(tickets, key=lambda t: t.id):
            earliest_j = max(0, i - ticket.max_rides)
            for j in range(i - 1, earliest_j - 1, -1):
                if departures[i - 1] - departures[j] > ticket.valid_minutes:
                    # Shortening the block only narrows the departure span,
                    # so smaller j can never become valid again.
                    break
                parent = labels[j]
                assert parent is not None  # one-ride blocks are always feasible
                candidates.append(
                    (
                        parent[0] + ticket.price_cents,
                        parent[1] + 1,
                        parent[2] + (ticket.id,),
                        j,
                    )
                )
        # Minimum price, then fewest tickets, then lexicographically
        # smallest ticket-id sequence in ride order.
        labels.append(min(candidates, key=lambda lab: lab[:3]))

    # Walk the parent-boundary chain back to reconstruct the blocks.
    blocks: list[tuple[str, int, int]] = []  # (ticket_id, covers_from, covers_to)
    i = n
    while i > 0:
        label = labels[i]
        assert label is not None
        j = label[3]
        blocks.append((label[2][-1], j, i - 1))
        i = j
    blocks.reverse()

    return {
        "total_cents": labels[n][0],  # type: ignore[index]
        "tickets": [
            {"ticket_id": ticket_id, "covers_from": lo, "covers_to": hi}
            for ticket_id, lo, hi in blocks
        ],
    }
