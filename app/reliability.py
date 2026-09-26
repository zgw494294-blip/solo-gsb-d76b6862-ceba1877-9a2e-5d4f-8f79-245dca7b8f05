"""On-time completion probability of a fixed ride chain under random delays.

Pure standard-library module so it can be unit-tested without FastAPI.

Problem
-------
A fixed chain of rides (each on a distinct trip) is given, together with a
discrete delay distribution per ridden trip.  Every trip independently draws
one delay, which shifts *all* of its arrival and departure times by the same
amount.  The itinerary succeeds when every transfer satisfies

    actual departure of ride i >= actual arrival of ride i-1 + transfer_buffer

and the last ride's actual arrival is no later than ``arrival_deadline``.
The answer is the exact success probability as a reduced fraction.

Solution
--------
Only adjacent rides constrain each other, so the success event factors along
a chain and a single forward sweep (sum-product on a chain) accumulates it
exactly.  After processing ride ``i``, ``dp[d]`` holds the combined weight
of all delay assignments to rides ``0..i`` that satisfy every transfer so
far and end with ride ``i`` delayed by ``d`` minutes.  Weights are integers
out of 100 per ride, so after ``i + 1`` rides the grand total is exactly
``100 ** (i + 1)`` and no floating point is ever involved.  Extending to
ride ``i + 1`` keeps exactly the predecessor delays with

    departure[i+1] + d_next >= arrival[i] + d + transfer_buffer

and the final answer sums the states whose last arrival meets the deadline.
With at most 12 rides and 6 outcomes per ride the sweep is O(12 * 6 * 6);
the fraction ``success / 100 ** n`` is returned reduced (``0/1`` when the
itinerary can never succeed).
"""

from __future__ import annotations

from math import gcd
from typing import Any, Sequence


def journey_reliability(
    *,
    legs: Sequence[Any],
    transfer_buffer: int,
    arrival_deadline: int,
) -> dict:
    """Exact on-time probability of a fixed ride chain.

    ``legs`` items are ``(departure, arrival, outcomes)`` triples: the
    scheduled departure at the boarding stop, the scheduled arrival at the
    alighting stop, and the trip's delay outcomes as ``(delay_minutes,
    weight)`` pairs whose weights sum to 100.

    Returns a dict with keys: success_numerator, success_denominator —
    decimal strings of the reduced success fraction (``"0"``/``"1"`` when
    no delay assignment completes the itinerary on time).
    """
    # dp[delay] = combined weight of the assignments to the rides processed
    # so far that satisfy every transfer up to the current ride and delay
    # the current ride by `delay` minutes.
    dp: dict[int, int] = {delay: weight for delay, weight in legs[0][2]}
    for i in range(1, len(legs)):
        departure = legs[i][0]
        prev_arrival = legs[i - 1][1]
        ndp: dict[int, int] = {}
        for delay, weight in legs[i][2]:
            # Predecessor delays from which this transfer still works.
            reachable = sum(
                prev_weight
                for prev_delay, prev_weight in dp.items()
                if departure + delay >= prev_arrival + prev_delay + transfer_buffer
            )
            ndp[delay] = weight * reachable
        dp = ndp

    arrival = legs[-1][1]
    success = sum(
        weight
        for delay, weight in dp.items()
        if arrival + delay <= arrival_deadline
    )
    total = 100 ** len(legs)
    if success == 0:
        return {"success_numerator": "0", "success_denominator": "1"}
    common = gcd(success, total)
    return {
        "success_numerator": str(success // common),
        "success_denominator": str(total // common),
    }
