"""End-to-end tests for POST /v1/journey/reliability."""

from __future__ import annotations

import itertools
import random
from math import gcd

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def payload(**overrides):
    base = {
        "stations": ["A", "C", "D"],
        "trips": [
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("C", 210, 215), stop("D", 300, 300)]},
        ],
        "rides": [
            {"trip_id": "T1", "board_index": 0, "alight_index": 1},
            {"trip_id": "T2", "board_index": 0, "alight_index": 1},
        ],
        "transfer_buffer": 10,
        "arrival_deadline": 320,
        "delay_profiles": [
            {
                "trip_id": "T1",
                "delays": [
                    {"delay_minutes": 0, "weight": 70},
                    {"delay_minutes": 20, "weight": 30},
                ],
            },
            {
                "trip_id": "T2",
                "delays": [
                    {"delay_minutes": 0, "weight": 50},
                    {"delay_minutes": 10, "weight": 50},
                ],
            },
        ],
    }
    base.update(overrides)
    return base


def chain_payload(n, slack=60, **overrides):
    """n chained 2-stop trips; nominal transfer slack is ``slack`` minutes."""
    stations = [f"S{i}" for i in range(n + 1)]
    trips, rides, profiles = [], [], []
    for i in range(n):
        dep = i * 100 + 50
        arr = dep + 100 - slack
        trips.append({
            "id": f"T{i}",
            "stops": [stop(stations[i], dep, dep), stop(stations[i + 1], arr, arr)],
        })
        rides.append({"trip_id": f"T{i}", "board_index": 0, "alight_index": 1})
        profiles.append({
            "trip_id": f"T{i}",
            "delays": [{"delay_minutes": 0, "weight": 100}],
        })
    base = {
        "stations": stations,
        "trips": trips,
        "rides": rides,
        "transfer_buffer": 10,
        "arrival_deadline": 2879,
        "delay_profiles": profiles,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/journey/reliability", json=body)


def brute_force_fraction(body):
    """Exact success fraction by enumerating every delay combination."""
    trips = {t["id"]: t for t in body["trips"]}
    profiles = {p["trip_id"]: p["delays"] for p in body["delay_profiles"]}
    legs = []
    for ride in body["rides"]:
        trip = trips[ride["trip_id"]]
        legs.append((
            trip["stops"][ride["board_index"]]["departure"],
            trip["stops"][ride["alight_index"]]["arrival"],
            [(o["delay_minutes"], o["weight"]) for o in profiles[ride["trip_id"]]],
        ))
    success = 0
    for combo in itertools.product(*(outcomes for _, _, outcomes in legs)):
        weight = 1
        for _, w in combo:
            weight *= w
        feasible = all(
            legs[i][0] + combo[i][0]
            >= legs[i - 1][1] + combo[i - 1][0] + body["transfer_buffer"]
            for i in range(1, len(legs))
        ) and legs[-1][1] + combo[-1][0] <= body["arrival_deadline"]
        if feasible:
            success += weight
    total = 100 ** len(legs)
    common = gcd(success, total)
    return {"success_numerator": str(success // common),
            "success_denominator": str(total // common)}


# ---------------------------------------------------------------- happy path

def test_two_ride_chain_partial_success():
    # Transfer needs 215 + d2 >= 200 + d1 + 10, i.e. d2 >= d1 - 5; the
    # deadline 320 never binds (300 + 10 <= 320).  Only d1 = 20 fails.
    r = post(payload())
    assert r.status_code == 200
    assert r.json() == {"success_numerator": "7", "success_denominator": "10"}


def test_profile_order_does_not_matter():
    # Profiles are matched to rides by trip_id, not by position.
    body = payload()
    body["delay_profiles"] = list(reversed(body["delay_profiles"]))
    assert post(body).json() == {"success_numerator": "7", "success_denominator": "10"}


def test_single_ride_certain_success():
    body = payload(
        trips=[{"id": "T1", "stops": [stop("A", 0, 60), stop("D", 300, 300)]}],
        rides=[{"trip_id": "T1", "board_index": 0, "alight_index": 1}],
        delay_profiles=[{"trip_id": "T1", "delays": [{"delay_minutes": 20, "weight": 100}]}],
    )
    assert post(body).json() == {"success_numerator": "1", "success_denominator": "1"}


def test_single_ride_zero_probability_returns_0_over_1():
    body = payload(
        trips=[{"id": "T1", "stops": [stop("A", 0, 60), stop("D", 300, 300)]}],
        rides=[{"trip_id": "T1", "board_index": 0, "alight_index": 1}],
        arrival_deadline=310,
        delay_profiles=[{"trip_id": "T1", "delays": [{"delay_minutes": 20, "weight": 100}]}],
    )
    assert post(body).json() == {"success_numerator": "0", "success_denominator": "1"}


def test_delay_shifts_alight_arrival_against_deadline():
    # Arrival 300 + d; deadline 305: delay 0 arrives 300 (ok), 10 arrives 310.
    body = payload(
        trips=[{"id": "T1", "stops": [stop("A", 0, 60), stop("D", 300, 300)]}],
        rides=[{"trip_id": "T1", "board_index": 0, "alight_index": 1}],
        arrival_deadline=305,
        delay_profiles=[{"trip_id": "T1", "delays": [
            {"delay_minutes": 0, "weight": 50},
            {"delay_minutes": 10, "weight": 50},
        ]}],
    )
    assert post(body).json() == {"success_numerator": "1", "success_denominator": "2"}


def test_arrival_exactly_at_deadline_counts_as_on_time():
    body = payload(
        trips=[{"id": "T1", "stops": [stop("A", 0, 60), stop("D", 300, 300)]}],
        rides=[{"trip_id": "T1", "board_index": 0, "alight_index": 1}],
        delay_profiles=[{"trip_id": "T1", "delays": [{"delay_minutes": 0, "weight": 100}]}],
    )
    assert post(payload(**{**body, "arrival_deadline": 300})).json()[
        "success_numerator"] == "1"
    assert post(payload(**{**body, "arrival_deadline": 299})).json() == {
        "success_numerator": "0", "success_denominator": "1"}


def test_transfer_boundary_departure_equals_arrival_plus_buffer():
    # No randomness: 210 >= 200 + 10 exactly meets the buffer; 11 breaks it.
    body = payload(
        delay_profiles=[
            {"trip_id": "T1", "delays": [{"delay_minutes": 0, "weight": 100}]},
            {"trip_id": "T2", "delays": [{"delay_minutes": 0, "weight": 100}]},
        ],
    )
    assert post(payload(**{**body, "transfer_buffer": 15})).json()[
        "success_numerator"] == "1"
    assert post(payload(**{**body, "transfer_buffer": 16})).json() == {
        "success_numerator": "0", "success_denominator": "1"}


def test_delay_shifts_boarding_departure_and_can_save_a_transfer():
    # Nominally impossible (250 < 300 + 10) yet *not* a 422: T2's 60-minute
    # delay shifts its departure to 310, exactly meeting the buffer.
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 300, 300)]},
            {"id": "T2", "stops": [stop("C", 240, 250), stop("D", 400, 400)]},
        ],
        arrival_deadline=500,
        delay_profiles=[
            {"trip_id": "T1", "delays": [{"delay_minutes": 0, "weight": 100}]},
            {"trip_id": "T2", "delays": [
                {"delay_minutes": 0, "weight": 50},
                {"delay_minutes": 60, "weight": 50},
            ]},
        ],
    )
    r = post(body)
    assert r.status_code == 200
    assert r.json() == {"success_numerator": "1", "success_denominator": "2"}


def test_fraction_is_returned_reduced():
    # (d1, d2) in {0,10} x {0,15}, uniform: only (0, 0) survives the
    # transfer (d2 >= d1 - 5) and the deadline (300 + d2 <= 310).
    body = payload(
        arrival_deadline=310,
        delay_profiles=[
            {"trip_id": "T1", "delays": [
                {"delay_minutes": 0, "weight": 50},
                {"delay_minutes": 10, "weight": 50},
            ]},
            {"trip_id": "T2", "delays": [
                {"delay_minutes": 0, "weight": 50},
                {"delay_minutes": 15, "weight": 50},
            ]},
        ],
    )
    # 2500 / 10000 reduces to 1 / 4.
    assert post(body).json() == {"success_numerator": "1", "success_denominator": "4"}


def test_three_ride_chain_multiplies_along_transfers():
    # Need d3 <= 5 (deadline), d3 >= d2 and d2 >= d1 (transfers): only the
    # all-zero assignment works out of the 2^3 equally likely ones.
    body = payload(
        stations=["A", "B", "C", "D"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 50), stop("B", 100, 100)]},
            {"id": "T2", "stops": [stop("B", 100, 110), stop("C", 200, 200)]},
            {"id": "T3", "stops": [stop("C", 200, 210), stop("D", 300, 300)]},
        ],
        rides=[
            {"trip_id": "T1", "board_index": 0, "alight_index": 1},
            {"trip_id": "T2", "board_index": 0, "alight_index": 1},
            {"trip_id": "T3", "board_index": 0, "alight_index": 1},
        ],
        arrival_deadline=305,
        delay_profiles=[
            {"trip_id": t, "delays": [
                {"delay_minutes": 0, "weight": 50},
                {"delay_minutes": 10, "weight": 50},
            ]}
            for t in ("T1", "T2", "T3")
        ],
    )
    assert post(body).json() == {"success_numerator": "1", "success_denominator": "8"}


def test_ride_may_span_intermediate_stops():
    # Boarding at stop 0 and alighting at stop 2 of one trip: the alighting
    # arrival (200) is what the transfer and deadline see.
    body = payload(
        stations=["A", "M", "C", "D"],
        trips=[
            {"id": "T1", "stops": [
                stop("A", 0, 60), stop("M", 120, 125), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("C", 210, 215), stop("D", 300, 300)]},
        ],
        rides=[
            {"trip_id": "T1", "board_index": 0, "alight_index": 2},
            {"trip_id": "T2", "board_index": 0, "alight_index": 1},
        ],
    )
    assert post(body).json() == {"success_numerator": "7", "success_denominator": "10"}


# ------------------------------------------------------- brute-force parity

def random_chain_payload(rng, n_rides, max_outcomes):
    stations = [f"S{i}" for i in range(n_rides + 1)]
    trips, rides, profiles = [], [], []
    for i in range(n_rides):
        dep = i * 80 + rng.randint(0, 40)
        arr = dep + rng.randint(1, 50)
        trips.append({
            "id": f"T{i}",
            "stops": [stop(stations[i], dep, dep), stop(stations[i + 1], arr, arr)],
        })
        rides.append({"trip_id": f"T{i}", "board_index": 0, "alight_index": 1})
        k = rng.randint(1, max_outcomes)
        delays = rng.sample(range(0, 61), k)
        weights = [1] * k
        for _ in range(100 - k):
            weights[rng.randrange(k)] += 1
        profiles.append({"trip_id": f"T{i}", "delays": [
            {"delay_minutes": d, "weight": w} for d, w in zip(delays, weights)
        ]})
    final_arrival = trips[-1]["stops"][1]["arrival"]
    return {
        "stations": stations,
        "trips": trips,
        "rides": rides,
        "transfer_buffer": rng.randint(1, 60),
        "arrival_deadline": max(0, final_arrival + rng.randint(-30, 90)),
        "delay_profiles": profiles,
    }


def test_randomized_parity_with_brute_force():
    rng = random.Random(20260926)
    for _ in range(40):
        body = random_chain_payload(rng, n_rides=rng.randint(1, 6), max_outcomes=4)
        r = post(body)
        assert r.status_code == 200
        assert r.json() == brute_force_fraction(body)


def test_twelve_ride_chain_parity_with_brute_force():
    # 12 rides x 2 outcomes (4096 combinations): a transfer breaks only
    # when a 50-minute delay meets an on-time successor (slack 55 < 60).
    body = chain_payload(12, slack=55)
    body["delay_profiles"] = [
        {"trip_id": f"T{i}", "delays": [
            {"delay_minutes": 0, "weight": 50},
            {"delay_minutes": 50, "weight": 50},
        ]}
        for i in range(12)
    ]
    r = post(body)
    assert r.status_code == 200
    assert r.json() == brute_force_fraction(body)
    assert r.json()["success_numerator"] != "0"


def test_twelve_rides_six_outcomes_each():
    body = chain_payload(12, slack=55)
    body["delay_profiles"] = [
        {"trip_id": f"T{i}", "delays": [
            {"delay_minutes": 0, "weight": 20},
            {"delay_minutes": 10, "weight": 20},
            {"delay_minutes": 20, "weight": 20},
            {"delay_minutes": 30, "weight": 20},
            {"delay_minutes": 40, "weight": 10},
            {"delay_minutes": 50, "weight": 10},
        ]}
        for i in range(12)
    ]
    r = post(body)
    assert r.status_code == 200
    result = r.json()
    num, den = int(result["success_numerator"]), int(result["success_denominator"])
    assert 0 < num < den
    assert gcd(num, den) == 1
    assert 100 ** 12 % den == 0


def test_all_certain_chain_is_one():
    r = post(chain_payload(12))
    assert r.json() == {"success_numerator": "1", "success_denominator": "1"}


# ------------------------------------------------------------ 422 validation

@pytest.mark.parametrize(
    "mutate",
    [
        # ride chain errors
        lambda b: b["rides"][1].update(trip_id="T1"),
        lambda b: b["rides"][0].update(trip_id="ZZ"),
        lambda b: b["rides"][0].update(alight_index=0),
        lambda b: b["rides"][0].update(board_index=1, alight_index=1),
        lambda b: b["rides"][0].update(alight_index=2),
        lambda b: b["rides"].reverse(),
        lambda b: b.update(rides=[]),
        # delay profile coverage errors
        lambda b: b["delay_profiles"].pop(),
        lambda b: b["delay_profiles"].append(
            {"trip_id": "T3", "delays": [{"delay_minutes": 0, "weight": 100}]}
        ),
        lambda b: b["delay_profiles"].append(
            {"trip_id": "T1", "delays": [{"delay_minutes": 5, "weight": 100}]}
        ),
        # delay distribution errors
        lambda b: b["delay_profiles"][0]["delays"][0].update(weight=69),
        lambda b: b["delay_profiles"][0]["delays"][0].update(weight=0),
        lambda b: b["delay_profiles"][0]["delays"][1].update(delay_minutes=0),
        lambda b: b["delay_profiles"][0]["delays"][1].update(delay_minutes=61),
        lambda b: b["delay_profiles"][0]["delays"][1].update(delay_minutes=-1),
        lambda b: b["delay_profiles"][0].update(delays=[]),
        lambda b: b["delay_profiles"][0].update(
            delays=[{"delay_minutes": i, "weight": 14} for i in range(7)]
        ),
        lambda b: b["delay_profiles"][0].update(unknown_field=1),
        lambda b: b.pop("delay_profiles"),
        # scalar field errors
        lambda b: b.update(transfer_buffer=0),
        lambda b: b.update(transfer_buffer=61),
        lambda b: b.update(arrival_deadline=-1),
        lambda b: b.update(arrival_deadline=2880),
        # network errors inherited from the shared schema
        lambda b: b.update(stations=["A", "A", "D"]),
        lambda b: b["trips"].append(
            {"id": "T1", "stops": [stop("A", 1, 2), stop("D", 3, 3)]}
        ),
    ],
    ids=[
        "duplicate-ride-trip-ids",
        "unknown-ride-trip",
        "alight-not-after-board",
        "board-at-last-stop",
        "alight-index-out-of-range",
        "alighting-station-mismatch",
        "no-rides",
        "profile-missing-for-ridden-trip",
        "profile-for-unridden-trip",
        "duplicate-profile-trip-ids",
        "weights-sum-not-100",
        "weight-not-positive",
        "duplicate-delay-minutes",
        "delay-above-60",
        "negative-delay",
        "empty-delay-list",
        "too-many-delay-outcomes",
        "extra-field-in-profile",
        "missing-delay-profiles",
        "buffer-too-small",
        "buffer-too-large",
        "negative-deadline",
        "deadline-out-of-range",
        "duplicate-stations",
        "duplicate-trip-ids",
    ],
)
def test_validation_errors_return_422(mutate):
    body = payload()
    mutate(body)
    r = post(body)
    assert r.status_code == 422, r.text


def test_thirteen_rides_return_422():
    assert post(chain_payload(13)).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/journey/reliability",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
