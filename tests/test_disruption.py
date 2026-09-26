"""End-to-end tests for POST /v1/network/disruption."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def payload(**overrides):
    base = {
        "stations": ["A", "B", "C", "D"],
        "trips": [
            {
                "id": "T1",
                "stops": [stop("A", 0, 60), stop("B", 120, 125), stop("C", 200, 200)],
            },
            {
                "id": "T2",
                "stops": [stop("C", 210, 215), stop("D", 300, 300)],
            },
        ],
        "origin": "A",
        "destination": "D",
        "earliest_departure": 0,
        "arrival_deadline": 300,
        "transfer_buffer": 10,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/network/disruption", json=body)


# ---------------------------------------------- repeated stops (looping trips)
# A trip may call the same station more than once.  The disruption search must
# only consider journeys that the journey planner would actually accept: an
# earlier arrival at (station, last_trip) must never be shadowed by a loop's
# later visit to that station, and staying aboard across a repeated stop needs
# no transfer buffer.

def loop_payload(**overrides):
    base = {
        "stations": ["O", "X", "B", "D"],
        "trips": [
            {"id": "T0", "stops": [stop("O", 0, 10), stop("X", 40, 40)]},
            {
                "id": "T1",
                "stops": [
                    stop("X", 45, 50),
                    stop("B", 100, 100),
                    stop("O", 200, 210),
                    stop("B", 300, 300),
                    stop("D", 400, 400),
                ],
            },
            {"id": "T2", "stops": [stop("B", 145, 150), stop("D", 250, 250)]},
        ],
        "origin": "O",
        "destination": "D",
        "earliest_departure": 0,
        "arrival_deadline": 350,
        "transfer_buffer": 5,
    }
    base.update(overrides)
    return base


def test_looping_trip_does_not_shadow_earlier_transfer():
    # T0 O10->X40, T1 X50->B100->O210->B300->D400, T2 B150->D250.
    # The real journey T0->T1->T2 reaches D at 250.  A search that settles
    # (B, T1) via the loop's second visit (300) before the earlier transfer
    # arrival (100) wrongly discards the only on-time connection.
    r = post(loop_payload())
    assert r.status_code == 200
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T0"],
    }
    # Each of T0, T1 and T2 is alone a hitting set; "T0" wins the
    # lexicographic tie over "T1" and "T2".


def test_loop_second_visit_arriving_exactly_at_deadline_still_allows_earlier_transfer():
    # With deadline 300 the loop's second call at B (arrival 300) is itself
    # on time but leads nowhere; the 250 transfer remains the journey to hit.
    r = post(loop_payload(arrival_deadline=300))
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T0"],
    }


def test_loop_repeated_station_deadline_boundary_250_vs_249():
    # 250 arrival meets the deadline exactly; 249 rules every journey out
    # (the loop only reaches D at 400).
    assert post(loop_payload(arrival_deadline=250)).json()["baseline_reachable"] is True
    r = post(loop_payload(arrival_deadline=249))
    assert r.json() == {
        "baseline_reachable": False,
        "minimum_cancellations": 0,
        "cancelled_ids": [],
    }


def test_transfer_buffer_boundary_at_loop_transfer_point():
    # X: alight at 40, T1 departs 50 -> buffer up to 10 works, 11 fails.
    assert post(loop_payload(transfer_buffer=10)).json()["baseline_reachable"] is True
    assert post(loop_payload(transfer_buffer=11)).json()["baseline_reachable"] is False


def test_staying_aboard_through_repeated_stop_needs_no_buffer():
    # One trip loops O->B->O->B->D.  Even with a 60-minute buffer the whole
    # ride is a single boarding (O at 10, D at 60); no transfer happens.
    body = loop_payload(
        stations=["O", "B", "D"],
        trips=[
            {
                "id": "T1",
                "stops": [
                    stop("O", 0, 10),
                    stop("B", 20, 20),
                    stop("O", 30, 30),
                    stop("B", 40, 50),
                    stop("D", 60, 60),
                ],
            }
        ],
        origin="O",
        destination="D",
        arrival_deadline=60,
        transfer_buffer=60,
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T1"],
    }


def test_tie_break_between_loop_and_direct_journeys_uses_id_order():
    # Two independent on-time journeys:
    #   T0+T1 loops X->Y->X and reaches D at 120
    #   T2+T3 goes direct via Y and reaches D at 110
    # Minimum hitting sets of size 2: {T0,T2}, {T0,T3}, {T1,T2}, {T1,T3};
    # ["T0", "T2"] is lexicographically smallest.
    body = loop_payload(
        stations=["O", "X", "Y", "D"],
        trips=[
            {"id": "T0", "stops": [stop("O", 0, 10), stop("X", 20, 20)]},
            {
                "id": "T1",
                "stops": [
                    stop("X", 30, 30),
                    stop("Y", 40, 40),
                    stop("X", 60, 70),
                    stop("D", 120, 120),
                ],
            },
            {"id": "T2", "stops": [stop("O", 0, 15), stop("Y", 80, 80)]},
            {"id": "T3", "stops": [stop("Y", 90, 90), stop("D", 110, 110)]},
        ],
        arrival_deadline=120,
        transfer_buffer=5,
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 2,
        "cancelled_ids": ["T0", "T2"],
    }
    # Tightening the deadline to 119 removes T1's final leg (D at 120) but
    # T0+T1 still reaches Y at 40, so {T0,T1,T3} survives alongside {T2,T3};
    # T3 is the unique common trip, hence the only singleton solution.
    assert post(loop_payload(
        stations=["O", "X", "Y", "D"],
        trips=body["trips"],
        arrival_deadline=119,
        transfer_buffer=5,
    )).json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T3"],
    }


# ------------------------------------- parent chain of unsettled candidates
# Regression: T1 calls at X twice (100 on the way out, 120 on the way back).
# The only ways to reach D by 170 are T0+T1+T2 (150), T0+T5 (165) and T4
# (160), so the minimum disconnection is 2: ["T0", "T4"].  The search must
# not splice the late second visit to X (via T3, arriving 120) onto the T2
# ride that only the earlier arrival (100, via T0) can catch — T2 leaves X
# at 105.  Treating that phantom {T1, T2, T3} chain as a real journey
# overestimates the minimum as 3 (["T0", "T1", "T4"]).

def revisit_payload(**overrides):
    base = {
        "stations": ["O", "A", "X", "B", "D"],
        "trips": [
            {"id": "T0", "stops": [stop("O", 0, 10), stop("A", 20, 20)]},
            {
                "id": "T1",
                "stops": [
                    stop("A", 25, 30),
                    stop("X", 100, 100),
                    stop("B", 110, 110),
                    stop("X", 120, 120),
                    stop("D", 200, 200),
                ],
            },
            {"id": "T2", "stops": [stop("X", 105, 105), stop("D", 150, 150)]},
            {"id": "T3", "stops": [stop("O", 0, 10), stop("B", 50, 50)]},
            {"id": "T4", "stops": [stop("O", 0, 10), stop("D", 160, 160)]},
            {"id": "T5", "stops": [stop("A", 30, 30), stop("D", 165, 165)]},
        ],
        "origin": "O",
        "destination": "D",
        "earliest_departure": 0,
        "arrival_deadline": 170,
        "transfer_buffer": 5,
    }
    base.update(overrides)
    return base


def test_revisit_late_prefix_is_not_spliced_onto_early_continuation():
    r = post(revisit_payload())
    assert r.status_code == 200
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 2,
        "cancelled_ids": ["T0", "T4"],
    }
    # {T0, T4} hits every on-time journey: {T0, T1, T2}, {T0, T5} and {T4};
    # no singleton does, and no other pair is lexicographically smaller.


def test_revisit_late_x_path_alone_is_not_a_journey():
    # Only T1, T2, T3 remain: T3 reaches B at 50 and T1's second call at X
    # is 120 — after T2 left (105) — while riding T1 on to D arrives 200,
    # past the deadline.  There is no on-time journey at all.
    r = post(revisit_payload(trips=revisit_payload()["trips"][1:4]))
    assert r.json() == {
        "baseline_reachable": False,
        "minimum_cancellations": 0,
        "cancelled_ids": [],
    }


def test_revisit_network_deadline_boundary_150_vs_149():
    # Deadline 150 admits only the T0+T1+T2 chain (150); every singleton
    # cancellation breaks it and T0 is the lexicographically smallest id.
    assert post(revisit_payload(arrival_deadline=150)).json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T0"],
    }
    # At 149 nothing arrives on time (T4 needs 160, T0+T5 needs 165).
    assert post(revisit_payload(arrival_deadline=149)).json() == {
        "baseline_reachable": False,
        "minimum_cancellations": 0,
        "cancelled_ids": [],
    }


def test_revisit_network_transfer_buffer_boundary():
    # Buffer 10: T0+T1+T2 dies at X (100 + 10 > 105) but T0+T5 still boards
    # at A (20 + 10 <= 30 exactly), so two cancellations are still needed.
    assert post(revisit_payload(transfer_buffer=10)).json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 2,
        "cancelled_ids": ["T0", "T4"],
    }
    # Buffer 11 kills T0+T5 as well (20 + 11 > 30); T4 is the only journey.
    assert post(revisit_payload(transfer_buffer=11)).json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T4"],
    }


# ---------------------------------------------------------------- happy path

def test_single_chain_one_cancellation_lexicographically_smallest():
    # The only journey is T1+T2 arriving exactly at the deadline (300).
    # Cancelling either trip breaks it; ["T1"] < ["T2"] lexicographically.
    r = post(payload())
    assert r.status_code == 200
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T1"],
    }


def test_arrival_exactly_at_deadline_counts_as_on_time():
    # Deadline 300 is met by the 300 arrival; 299 is not.
    assert post(payload(arrival_deadline=300)).json()["baseline_reachable"] is True
    r = post(payload(arrival_deadline=299))
    assert r.json() == {
        "baseline_reachable": False,
        "minimum_cancellations": 0,
        "cancelled_ids": [],
    }


def test_parallel_trips_must_all_be_cancelled():
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 100), stop("D", 200, 200)]},
            {"id": "T2", "stops": [stop("A", 0, 110), stop("D", 210, 210)]},
        ],
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 2,
        "cancelled_ids": ["T1", "T2"],
    }


def test_redundant_routes_need_two_cancellations():
    # Journeys {T1, T2} (A->B->D) and {T3} (A->D direct).  Minimum hitting
    # sets: {T1, T3} and {T2, T3}; ["T1", "T3"] is lexicographically smaller.
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("B", 100, 100)]},
            {"id": "T2", "stops": [stop("B", 110, 110), stop("D", 200, 200)]},
            {"id": "T3", "stops": [stop("A", 0, 70), stop("D", 250, 250)]},
        ],
        transfer_buffer=5,
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 2,
        "cancelled_ids": ["T1", "T3"],
    }


def test_shared_trip_is_the_unique_singleton_solution():
    # Journeys {T1, T3} and {T2, T3}: only cancelling T3 breaks both at once.
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("B", 100, 100)]},
            {"id": "T2", "stops": [stop("A", 0, 65), stop("B", 105, 105)]},
            {"id": "T3", "stops": [stop("B", 110, 110), stop("D", 200, 200)]},
        ],
        transfer_buffer=5,
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T3"],
    }


def test_deadline_prunes_late_journeys():
    # T9 arrives 500 (past the 400 deadline): only T2's journey is feasible,
    # so cancelling T2 alone disconnects on-time travel.
    body = payload(
        trips=[
            {"id": "T2", "stops": [stop("A", 0, 100), stop("D", 300, 300)]},
            {"id": "T9", "stops": [stop("A", 0, 200), stop("D", 500, 500)]},
        ],
        arrival_deadline=400,
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T2"],
    }


def test_staying_on_same_trip_through_intermediate_stops():
    body = payload(
        trips=[
            {
                "id": "T3",
                "stops": [stop("A", 0, 10), stop("B", 50, 60), stop("D", 90, 90)],
            }
        ],
        arrival_deadline=90,
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["T3"],
    }


# ------------------------------------------------------- baseline unreachable

def test_baseline_unreachable_returns_zero_and_empty_list():
    r = post(payload(origin="D", destination="A"))
    assert r.status_code == 200
    assert r.json() == {
        "baseline_reachable": False,
        "minimum_cancellations": 0,
        "cancelled_ids": [],
    }


def test_transfer_buffer_can_make_baseline_unreachable():
    # Alight at C at 200, buffer 16 -> need departure >= 216, T2 leaves 215.
    assert post(payload(transfer_buffer=16)).json()["baseline_reachable"] is False
    # Boundary: buffer 15 -> need >= 215, exactly met.
    assert post(payload(transfer_buffer=15)).json()["baseline_reachable"] is True


def test_earliest_departure_gates_first_boarding():
    # T1 leaves A at 60; earliest_departure=61 makes boarding impossible.
    assert post(payload(earliest_departure=61)).json()["baseline_reachable"] is False
    assert post(payload(earliest_departure=60)).json()["baseline_reachable"] is True


def test_deadline_may_equal_earliest_departure():
    # Not a 422 (deadline is not *earlier* than earliest departure); with no
    # zero-length trip the demand is simply unreachable.
    r = post(payload(earliest_departure=0, arrival_deadline=0))
    assert r.status_code == 200
    assert r.json()["baseline_reachable"] is False


# ------------------------------------------------------------------ tie-break

def test_lexicographic_tie_break_uses_trip_ids_not_departure_order():
    # The only journey chains B2 (departs first) into A1.  Cancelling either
    # trip breaks it; ["A1"] wins although A1 is the later trip.
    body = payload(
        trips=[
            {"id": "B2", "stops": [stop("A", 0, 100), stop("C", 150, 150)]},
            {"id": "A1", "stops": [stop("C", 160, 160), stop("D", 250, 250)]},
        ],
    )
    r = post(body)
    assert r.json() == {
        "baseline_reachable": True,
        "minimum_cancellations": 1,
        "cancelled_ids": ["A1"],
    }


# --------------------------------------------------------------------- stress

def test_sixteen_parallel_trips_all_cancelled():
    trips = [
        {"id": f"T{i:02d}", "stops": [stop("A", 0, 60 + i * 5), stop("D", 110 + i * 5, 110 + i * 5)]}
        for i in range(16)
    ]
    r = post(payload(stations=["A", "D"], trips=trips))
    assert r.status_code == 200
    body = r.json()
    assert body["baseline_reachable"] is True
    assert body["minimum_cancellations"] == 16
    assert body["cancelled_ids"] == [f"T{i:02d}" for i in range(16)]


def test_sixteen_trip_chain_any_single_cancellation_breaks_it():
    stations = [f"S{i}" for i in range(17)]
    trips = [
        {
            "id": f"T{i:02d}",
            "stops": [
                stop(stations[i], i * 100, i * 100),
                stop(stations[i + 1], i * 100 + 50, i * 100 + 50),
            ],
        }
        for i in range(16)
    ]
    r = post(
        payload(
            stations=stations,
            trips=trips,
            origin="S0",
            destination="S16",
            arrival_deadline=2879,
            transfer_buffer=5,
        )
    )
    assert r.status_code == 200
    body = r.json()
    assert body["baseline_reachable"] is True
    # Every singleton works; the lexicographically smallest id wins.
    assert body["minimum_cancellations"] == 1
    assert body["cancelled_ids"] == ["T00"]


# ------------------------------------------------------------ 422 validation

@pytest.mark.parametrize(
    "mutate",
    [
        # station reference errors
        lambda b: b.update(origin="ZZ"),
        lambda b: b.update(destination="ZZ"),
        lambda b: b["trips"][0]["stops"].append(stop("ZZ", 300, 300)),
        lambda b: b.update(stations=["A", "A", "B"]),
        # timing errors
        lambda b: b["trips"][0]["stops"].__setitem__(1, stop("B", 130, 125)),
        lambda b: b["trips"][0]["stops"].__setitem__(2, stop("C", 125, 200)),
        lambda b: b["trips"][0]["stops"].__setitem__(0, stop("A", 0, 3000)),
        lambda b: b.update(arrival_deadline=2880),
        lambda b: b.update(earliest_departure=-1),
        lambda b: b.update(earliest_departure=100, arrival_deadline=99),
        # structure errors
        lambda b: b.update(origin="A", destination="A"),
        lambda b: b.update(transfer_buffer=0),
        lambda b: b.update(transfer_buffer=61),
        lambda b: b.update(stations=["ONLY"]),
        lambda b: b.update(trips=[]),
        lambda b: b["trips"].append({"id": "T1", "stops": [stop("A", 1, 2), stop("B", 3, 3)]}),
        lambda b: b["trips"].__setitem__(0, {"id": "X", "stops": [stop("A", 1, 2)]}),
        lambda b: b["trips"].extend(
            {"id": f"X{i}", "stops": [stop("A", 1, 2), stop("B", 3, 3)]}
            for i in range(15)
        ),
    ],
    ids=[
        "unknown-origin",
        "unknown-destination",
        "trip-references-unknown-station",
        "duplicate-stations",
        "arrival-after-departure",
        "next-arrival-not-after-prev-departure",
        "minute-out-of-range",
        "deadline-out-of-range",
        "negative-earliest-departure",
        "deadline-before-earliest-departure",
        "same-origin-destination",
        "buffer-too-small",
        "buffer-too-large",
        "too-few-stations",
        "no-trips",
        "duplicate-trip-ids",
        "trip-with-single-stop",
        "too-many-trips",
    ],
)
def test_validation_errors_return_422(mutate):
    body = payload()
    mutate(body)
    r = post(body)
    assert r.status_code == 422, r.text


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/network/disruption",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
