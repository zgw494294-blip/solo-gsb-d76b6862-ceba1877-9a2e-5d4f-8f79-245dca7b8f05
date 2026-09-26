"""End-to-end tests for POST /v1/timetable/design."""

from __future__ import annotations

import random
from itertools import combinations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def batch(arrival, passengers):
    return {"arrival": arrival, "passengers": passengers}


def pattern():
    # Offsets relative to the first-station departure: A -(20)-> B -(20)-> C.
    return [stop("A", 0, 0), stop("B", 20, 25), stop("C", 45, 45)]


def payload(**overrides):
    base = {
        "stops": pattern(),
        "candidates": [100, 120, 140, 160, 180, 200],
        "demand": [batch(95, 3), batch(130, 2), batch(170, 4)],
        "trip_count": 2,
        "min_interval": 30,
        "max_wait": 45,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/timetable/design", json=body)


def brute_force(candidates, demand, trip_count, min_interval, max_wait):
    """Exhaustive reference: (total_wait, departures) of the optimum."""
    best = None
    for combo in combinations(sorted(candidates), trip_count):
        if any(
            combo[i + 1] - combo[i] < min_interval
            for i in range(len(combo) - 1)
        ):
            continue
        total, ok = 0, True
        for arrival, passengers in demand:
            departure = next((d for d in combo if d >= arrival), None)
            if departure is None or departure - arrival > max_wait:
                ok = False
                break
            total += passengers * (departure - arrival)
        if not ok:
            continue
        label = (total, list(combo))
        if best is None or label < best:
            best = label
    return best


# ---------------------------------------------------------------- happy path

def test_designs_the_weighted_optimal_timetable():
    # Batch 95 must ride d1 in [95, 140]; batch 130 forces d1 >= 130 (a later
    # d2 in {180, 200} would make it wait 50+); batch 170 forces d2 in
    # [170, 215].  d1 = 140, d2 = 180: 3*45 + 2*10 + 4*10 = 195.
    r = post(payload())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "feasible": True,
        "departures": [140, 180],
        "total_wait": 195,
        "stations": ["A", "B", "C"],
        "trips": [
            {"id": "design@140", "stops": [
                stop("A", 140, 140), stop("B", 160, 165), stop("C", 185, 185)]},
            {"id": "design@180", "stops": [
                stop("A", 180, 180), stop("B", 200, 205), stop("C", 225, 225)]},
        ],
    }


def test_single_departure_serves_every_batch():
    body = payload(trip_count=1, candidates=[170, 200], max_wait=120)
    # One departure at 170: 3*75 + 2*40 + 4*0 = 305 beats 200 (3*105+2*70+4*30).
    out = post(body).json()
    assert out["feasible"] is True
    assert out["departures"] == [170]
    assert out["total_wait"] == 305


def test_every_candidate_can_be_chosen():
    body = payload(
        candidates=[100, 150, 200],
        demand=[batch(100, 1), batch(150, 1), batch(200, 1)],
        trip_count=3,
        min_interval=0,
        max_wait=0,
    )
    out = post(body).json()
    assert out["feasible"] is True
    assert out["departures"] == [100, 150, 200]
    assert out["total_wait"] == 0


def test_waiting_exactly_max_wait_is_allowed():
    body = payload(candidates=[160], demand=[batch(100, 2)], trip_count=1,
                   min_interval=0, max_wait=60)
    out = post(body).json()
    assert out["feasible"] is True
    assert out["total_wait"] == 120


def test_boarding_at_the_arrival_minute_waits_zero():
    body = payload(candidates=[100], demand=[batch(100, 5)], trip_count=1,
                   min_interval=0, max_wait=0)
    out = post(body).json()
    assert out["feasible"] is True
    assert out["total_wait"] == 0


def test_batches_sharing_an_arrival_minute_accumulate():
    body = payload(
        candidates=[110],
        demand=[batch(100, 2), batch(100, 3)],
        trip_count=1,
        min_interval=0,
        max_wait=60,
    )
    out = post(body).json()
    assert out["feasible"] is True
    assert out["total_wait"] == 50


# ------------------------------------------------------------------ tie-break

def test_tie_broken_by_lexicographically_smallest_departure_list():
    # One batch at 100; any pair with first departure 101 costs 5.  The
    # interval rule admits (101, 103), (101, 104) and (102, 104); the two
    # 101-pairs tie and (101, 103) is lexicographically smaller.
    body = payload(
        candidates=[101, 102, 103, 104],
        demand=[batch(100, 5)],
        trip_count=2,
        min_interval=2,
        max_wait=10,
    )
    out = post(body).json()
    assert out["feasible"] is True
    assert out["departures"] == [101, 103]
    assert out["total_wait"] == 5


def test_tie_break_prefers_the_smaller_first_departure():
    # The batch at 108 rides the 110 departure either way, so (102, 110)
    # and (104, 110) tie at 10; the smaller first departure wins.
    body = payload(
        candidates=[102, 104, 110],
        demand=[batch(108, 5)],
        trip_count=2,
        min_interval=0,
        max_wait=10,
    )
    out = post(body).json()
    assert out["departures"] == [102, 110]
    assert out["total_wait"] == 10


# ------------------------------------------------------------------ infeasible

def test_more_trips_than_candidates_is_infeasible():
    body = payload(candidates=[100, 200], trip_count=3)
    out = post(body).json()
    assert out == {
        "feasible": False,
        "departures": [],
        "total_wait": None,
        "stations": [],
        "trips": [],
    }


def test_batch_beyond_the_last_candidate_is_infeasible():
    body = payload(candidates=[100, 200], demand=[batch(500, 1)], trip_count=1)
    assert post(body).json()["feasible"] is False


def test_unavoidable_wait_over_max_wait_is_infeasible():
    body = payload(candidates=[161], demand=[batch(100, 1)], trip_count=1,
                   max_wait=60)
    assert post(body).json()["feasible"] is False


def test_min_interval_can_make_the_count_unreachable():
    # Three departures at least 60 apart cannot come from two clusters.
    body = payload(
        candidates=[100, 110, 200, 210],
        demand=[batch(100, 1), batch(210, 1)],
        trip_count=3,
        min_interval=60,
        max_wait=120,
    )
    assert post(body).json()["feasible"] is False


def test_zero_max_wait_requires_exact_departures():
    body = payload(candidates=[100, 200], demand=[batch(100, 1), batch(150, 1)],
                   trip_count=2, max_wait=0)
    assert post(body).json()["feasible"] is False


# ------------------------------------------------------- journey integration

def test_designed_network_feeds_v1_journey():
    out = post(payload()).json()
    assert out["feasible"] is True
    r = client.post("/v1/journey", json={
        "stations": out["stations"],
        "trips": out["trips"],
        "origin": "A",
        "destination": "C",
        "earliest_departure": 0,
        "transfer_buffer": 1,
    })
    assert r.status_code == 200, r.text
    journey = r.json()
    assert journey["reachable"] is True
    assert journey["arrival_time"] == 185
    assert journey["trip_ids"] == ["design@140"]


# ------------------------------------------------------------ 422 validation

def test_stops_count_and_uniqueness():
    assert post(payload(stops=[stop("A", 0, 0)])).status_code == 422
    many = [stop(f"S{i}", i * 10, i * 10 + 1) for i in range(21)]
    many[0] = stop("S0", 0, 0)
    assert post(payload(stops=many)).status_code == 422
    assert post(payload(stops=[stop("A", 0, 0), stop("A", 10, 10)])).status_code == 422
    assert post(payload(stops=[stop("A", 0, 0), stop("", 10, 10)])).status_code == 422


def test_first_stop_offsets_must_be_zero():
    assert post(payload(stops=[stop("A", 1, 1), stop("B", 10, 10)])).status_code == 422
    assert post(payload(stops=[stop("A", 0, 5), stop("B", 10, 10)])).status_code == 422


def test_stop_time_order_rules():
    # arrival after departure at the same stop.
    assert post(payload(stops=[stop("A", 0, 0), stop("B", 12, 10)])).status_code == 422
    # next arrival not later than the previous departure.
    assert post(payload(stops=[stop("A", 0, 0), stop("B", 0, 5)])).status_code == 422
    # negative / overflowing offsets.
    assert post(payload(stops=[stop("A", 0, 0), stop("B", -1, 10)])).status_code == 422
    assert post(payload(stops=[stop("A", 0, 0), stop("B", 10, 2880)])).status_code == 422


def test_candidate_plus_max_offset_must_stay_in_range():
    # Max offset is 45: 2834 + 45 = 2879 is fine, 2835 + 45 is not.
    assert post(payload(candidates=[2834])).status_code == 200
    assert post(payload(candidates=[2835])).status_code == 422


def test_candidates_validation():
    assert post(payload(candidates=[])).status_code == 422
    assert post(payload(candidates=[100, 100])).status_code == 422
    assert post(payload(candidates=[-1])).status_code == 422
    assert post(payload(candidates=[2880])).status_code == 422
    assert post(payload(candidates=list(range(61)))).status_code == 422
    body = payload()
    del body["candidates"]
    assert post(body).status_code == 422


def test_demand_validation():
    assert post(payload(demand=[])).status_code == 422
    assert post(payload(demand=[batch(100, 1)] * 61)).status_code == 422
    assert post(payload(demand=[batch(100, 0)])).status_code == 422
    assert post(payload(demand=[batch(100, -3)])).status_code == 422
    assert post(payload(demand=[batch(-1, 1)])).status_code == 422
    assert post(payload(demand=[batch(2880, 1)])).status_code == 422
    assert post(payload(demand=[{"arrival": 100}])).status_code == 422
    body = payload()
    del body["demand"]
    assert post(body).status_code == 422


def test_scalar_field_bounds():
    assert post(payload(trip_count=0)).status_code == 422
    assert post(payload(trip_count=13)).status_code == 422
    assert post(payload(min_interval=-1)).status_code == 422
    assert post(payload(min_interval=121)).status_code == 422
    assert post(payload(max_wait=-1)).status_code == 422
    assert post(payload(max_wait=121)).status_code == 422
    for field in ("trip_count", "min_interval", "max_wait", "stops"):
        body = payload()
        del body[field]
        assert post(body).status_code == 422


def test_extra_fields_return_422():
    body = payload()
    body["nope"] = 1
    assert post(body).status_code == 422

    body = payload()
    body["stops"][0]["nope"] = 1
    assert post(body).status_code == 422

    body = payload()
    body["demand"][0]["nope"] = 1
    assert post(body).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/timetable/design",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def test_matches_brute_force_on_random_scenarios():
    rng = random.Random(20260926)
    for _ in range(60):
        candidates = rng.sample(range(0, 300), rng.randint(1, 14))
        demand = [
            (rng.randint(0, 300), rng.randint(1, 6))
            for _ in range(rng.randint(1, 10))
        ]
        trip_count = rng.randint(1, 12)
        min_interval = rng.randint(0, 120)
        max_wait = rng.randint(0, 120)
        body = payload(
            candidates=candidates,
            demand=[batch(a, p) for a, p in demand],
            trip_count=trip_count,
            min_interval=min_interval,
            max_wait=max_wait,
        )
        r = post(body)
        assert r.status_code == 200, r.text
        out = r.json()
        expected = brute_force(
            candidates, demand, trip_count, min_interval, max_wait
        )
        if expected is None:
            assert out["feasible"] is False
            assert out["departures"] == []
            assert out["total_wait"] is None
            assert out["stations"] == []
            assert out["trips"] == []
        else:
            total_wait, departures = expected
            assert out["feasible"] is True
            assert out["departures"] == departures
            assert out["total_wait"] == total_wait
            assert [t["id"] for t in out["trips"]] == [
                f"design@{d}" for d in departures
            ]


def test_maximum_size_request_stays_fast_and_valid():
    rng = random.Random(7)
    candidates = [i * 48 for i in range(60)]  # 0..2832, all distinct
    # Batches arrive exactly at the first 12 candidates, so the unique
    # zero-wait choice is those 12 departures.
    demand = [
        batch(candidates[i], rng.randint(1, 50))
        for i in range(12)
        for _ in range(5)
    ]
    stops = [stop("S0", 0, 0)] + [
        stop(f"S{i}", i * 2, i * 2 + 1) for i in range(1, 20)
    ]
    body = payload(
        stops=stops,
        candidates=candidates,
        demand=demand,
        trip_count=12,
        min_interval=30,
        max_wait=120,
    )
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["feasible"] is True
    assert out["departures"] == candidates[:12]
    assert out["total_wait"] == 0
    assert len(out["trips"]) == 12
    assert all(len(t["stops"]) == 20 for t in out["trips"])
    # The last trip ends at 528 + 39 = 567, within the 0..2879 window.
    assert out["trips"][-1]["stops"][-1]["departure"] == 567
