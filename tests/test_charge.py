"""End-to-end tests for POST /v1/fleet/charge."""

from __future__ import annotations

import random
from itertools import product

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def trip(tid, *stops):
    return {"id": tid, "stops": [stop(*s) for s in stops]}


def charger(station, rate, price_cents):
    return {"station": station, "rate": rate, "price_cents": price_cents}


def base_trips():
    # T1 ends at B at 120, T2 leaves B at 150 (30-minute stop);
    # T2 ends at C at 220, T3 leaves C at 230 (10-minute stop).
    return [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 150, 150), ("C", 220, 220)),
        trip("T3", ("C", 230, 230), ("D", 300, 300)),
    ]


def payload(**overrides):
    base = {
        "stations": ["A", "B", "C", "D"],
        "trips": base_trips(),
        "trip_ids": ["T1", "T2", "T3"],
        "consumptions": {"T1": 10, "T2": 20, "T3": 5},
        "capacity": 30,
        "initial_charge": 15,
        "chargers": [charger("B", 2, 5), charger("C", 3, 1)],
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/fleet/charge", json=body)


def simulate(body, charges):
    """Independent re-check: the returned plan is feasible and costs the
    reported total.  Returns the final battery level."""
    trips = {t["id"]: t for t in body["trips"]}
    chargers = {c["station"]: c for c in body["chargers"]}
    ids = body["trip_ids"]
    assert len(charges) == len(ids) - 1
    battery = body["initial_charge"]
    total = 0
    for i, tid in enumerate(ids):
        assert battery >= body["consumptions"][tid]
        battery -= body["consumptions"][tid]
        if i == len(ids) - 1:
            break
        station = trips[tid]["stops"][-1]["station"]
        stop_minutes = (
            trips[ids[i + 1]]["stops"][0]["departure"]
            - trips[tid]["stops"][-1]["arrival"]
        )
        amount = charges[i]
        assert isinstance(amount, int) and amount >= 0
        if amount:
            chg = chargers[station]
            assert amount <= chg["rate"] * stop_minutes
            total += amount * chg["price_cents"]
        battery += amount
        assert battery <= body["capacity"]
    return total


# ---------------------------------------------------------------- happy path

def test_cheapest_plan_splits_charging_between_stations():
    # After T1 the battery holds 5; T2 needs 20, so at least 15 must come
    # from B (5 cents/unit).  T3 then needs 5, cheapest at C (1 cent/unit):
    # charging 20 at B instead would cost 100, so [15, 5] at 80 wins.
    r = post(payload())
    assert r.status_code == 200, r.text
    assert r.json() == {"feasible": True, "total_cents": 80, "charges": [15, 5]}


def test_cheap_early_station_covers_a_later_gap():
    # B is cheap (1), C is expensive (9): fill 15 at B so that C is skipped.
    body = payload(
        consumptions={"T1": 10, "T2": 10, "T3": 10},
        chargers=[charger("B", 2, 1), charger("C", 3, 9)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 15, "charges": [15, 0]}
    assert simulate(body, out["charges"]) == out["total_cents"]


def test_free_electricity_charges_as_late_as_possible():
    # Every plan costs 0; the lexicographically smallest charge list wins,
    # which pushes the charging towards the later gap.
    body = payload(
        consumptions={"T1": 10, "T2": 10, "T3": 10},
        chargers=[charger("B", 2, 0), charger("C", 3, 0)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 0, "charges": [5, 10]}


def test_equal_prices_still_prefer_the_lexicographically_smallest_list():
    # 15 at B or 5+10 split both cost 30; [5, 10] < [15, 0].
    body = payload(
        consumptions={"T1": 10, "T2": 10, "T3": 10},
        chargers=[charger("B", 2, 2), charger("C", 3, 2)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 30, "charges": [5, 10]}


def test_free_station_is_used_exactly_as_much_as_useful():
    # B is free: any c0 in [15, 25] completes the chain at cost 0 with
    # c1 = 0; the lexicographically smallest is c0 = 15.
    body = payload(
        consumptions={"T1": 10, "T2": 10, "T3": 10},
        chargers=[charger("B", 2, 0), charger("C", 3, 7)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 0, "charges": [15, 0]}


def test_rate_times_stop_minutes_caps_the_charge():
    # C allows only 1 * 10 = 10 units; T3 needs 15, so 5 must be
    # pre-charged at B beyond T2's own consumption.
    body = payload(
        consumptions={"T1": 10, "T2": 5, "T3": 15},
        capacity=20,
        initial_charge=10,
        chargers=[charger("B", 1, 3), charger("C", 1, 1)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 10 * 3 + 10, "charges": [10, 10]}
    assert simulate(body, out["charges"]) == out["total_cents"]


def test_battery_capacity_caps_the_charge():
    # B could deliver 2 * 30 = 60 units, but the battery only has room for
    # 25 after T1; that exactly covers T2 and T3 when T3 needs 10.
    body = payload(
        consumptions={"T1": 10, "T2": 20, "T3": 10},
        chargers=[charger("B", 2, 1)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 25, "charges": [25, 0]}
    # One unit more and the capacity cap (not the rate) makes it infeasible.
    body["consumptions"]["T3"] = 11
    out = post(body).json()
    assert out == {"feasible": False, "total_cents": None, "charges": []}


def test_zero_stop_minutes_allow_no_charging():
    # T2 departs B exactly when T1 arrives: the stop is 0 minutes.
    body = payload(
        trips=[
            trip("T1", ("A", 0, 60), ("B", 120, 120)),
            trip("T2", ("B", 120, 120), ("C", 200, 200)),
        ],
        trip_ids=["T1", "T2"],
        consumptions={"T1": 10, "T2": 5},
        initial_charge=15,
        chargers=[charger("B", 60, 1)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 0, "charges": [0]}
    # One unit short and even a zero-priced fast charger cannot help.
    body["initial_charge"] = 14
    out = post(body).json()
    assert out == {"feasible": False, "total_cents": None, "charges": []}


def test_single_trip_chain_needs_no_charge():
    body = payload(
        trip_ids=["T1"],
        consumptions={"T1": 10},
        initial_charge=10,
        chargers=[],
    )
    assert post(body).json() == {
        "feasible": True,
        "total_cents": 0,
        "charges": [],
    }


def test_charger_at_non_gap_station_does_not_count():
    # Chargers sit at A (chain start) and D (chain end): neither is a stop
    # *between* two trips, so the chain must run on the initial charge.
    body = payload(
        consumptions={"T1": 10, "T2": 20, "T3": 5},
        capacity=40,
        initial_charge=35,
        chargers=[charger("A", 60, 0), charger("D", 60, 0)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 0, "charges": [0, 0]}
    body["initial_charge"] = 34
    out = post(body).json()
    assert out == {"feasible": False, "total_cents": None, "charges": []}


def test_no_charger_configured_anywhere():
    body = payload(
        consumptions={"T1": 10, "T2": 20, "T3": 5},
        capacity=40,
        initial_charge=35,
        chargers=[],
    )
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 0, "charges": [0, 0]}


def test_initial_charge_exactly_capacity_blocks_early_charging():
    # Battery starts full at 30; after T1 (10) there is room for 20 at B.
    body = payload(
        consumptions={"T1": 10, "T2": 20, "T3": 10},
        initial_charge=30,
        chargers=[charger("B", 2, 4), charger("C", 3, 2)],
    )
    out = post(body).json()
    # After T1: 20. T2 needs 20 -> c0 = 0 suffices; after T2: 0, so C must
    # deliver 10.  Cost 20.  Charging at B cannot reduce that (C is
    # cheaper), and the lex-smallest plan charges nothing at B.
    assert out == {"feasible": True, "total_cents": 20, "charges": [0, 10]}


# --------------------------------------------------------------- infeasible

def test_first_trip_beyond_initial_charge_is_infeasible():
    body = payload(consumptions={"T1": 16, "T2": 1, "T3": 1}, initial_charge=15)
    assert post(body).json() == {"feasible": False, "total_cents": None, "charges": []}


def test_mid_chain_wall_is_infeasible():
    # T2 needs 20 but the battery after T1 is 5 and B is not configured.
    body = payload(chargers=[charger("C", 3, 1)])
    out = post(body).json()
    assert out == {"feasible": False, "total_cents": None, "charges": []}


def test_rate_cap_can_make_the_chain_infeasible():
    # C delivers at most 10 but T3 needs 15 and B cannot pre-charge enough
    # (capacity 20, T2 burns 5: at most 15 reaches C, plus 10 there = 25
    # would do — but capacity 20 caps the arrival at C + charge to 20).
    body = payload(
        consumptions={"T1": 10, "T2": 5, "T3": 21},
        capacity=20,
        initial_charge=10,
        chargers=[charger("B", 1, 1), charger("C", 1, 1)],
    )
    out = post(body).json()
    assert out == {"feasible": False, "total_cents": None, "charges": []}
    # One unit less fits exactly: any c0 + c1 = 25 with c0 <= 20 and
    # c1 <= 10 works at cost 25; the lexicographically smallest is c0 = 15.
    body["consumptions"]["T3"] = 20
    out = post(body).json()
    assert out == {"feasible": True, "total_cents": 25, "charges": [15, 10]}


# ------------------------------------------------------------ 422 validation

def test_trip_ids_validation():
    assert post(payload(trip_ids=[])).status_code == 422
    many = [f"TX{i}" for i in range(13)]
    assert post(payload(trip_ids=many)).status_code == 422
    assert post(payload(trip_ids=["T1", "T2", "T1"])).status_code == 422
    assert post(payload(trip_ids=["T1", "TX", "T3"])).status_code == 422
    body = payload()
    del body["trip_ids"]
    assert post(body).status_code == 422


def test_consumptions_must_cover_exactly_the_chain():
    # Missing a chained trip.
    assert post(payload(consumptions={"T1": 10, "T2": 20})).status_code == 422
    # A trip outside the chain.
    extra = {"T1": 10, "T2": 20, "T3": 5, "T9": 1}
    assert post(payload(consumptions=extra)).status_code == 422
    # Values outside 1..60.
    bad_low = {"T1": 0, "T2": 20, "T3": 5}
    assert post(payload(consumptions=bad_low)).status_code == 422
    bad_high = {"T1": 10, "T2": 61, "T3": 5}
    assert post(payload(consumptions=bad_high)).status_code == 422
    body = payload()
    del body["consumptions"]
    assert post(body).status_code == 422


def test_battery_fields_validation():
    assert post(payload(capacity=0)).status_code == 422
    assert post(payload(capacity=61)).status_code == 422
    assert post(payload(initial_charge=-1)).status_code == 422
    # initial_charge must not exceed the capacity.
    assert post(payload(initial_charge=31)).status_code == 422
    assert post(payload(initial_charge=30)).status_code == 200
    for field in ("capacity", "initial_charge"):
        body = payload()
        del body[field]
        assert post(body).status_code == 422


def test_charger_validation():
    # Station must be unique and declared.
    dup = [charger("B", 1, 1), charger("B", 2, 2)]
    assert post(payload(chargers=dup)).status_code == 422
    assert post(payload(chargers=[charger("Z", 1, 1)])).status_code == 422
    # Rate 1..60, price non-negative.
    assert post(payload(chargers=[charger("B", 0, 1)])).status_code == 422
    assert post(payload(chargers=[charger("B", 61, 1)])).status_code == 422
    assert post(payload(chargers=[charger("B", 1, -1)])).status_code == 422
    # Free charging is legal.
    assert post(payload(chargers=[charger("B", 1, 0)])).status_code == 200
    # More than 40 chargers is rejected even before station checks.
    many = [charger("B", 1, 1)] * 41
    assert post(payload(chargers=many)).status_code == 422
    # Extra fields inside a charger are rejected.
    body = payload()
    body["chargers"][0]["nope"] = 1
    assert post(body).status_code == 422
    body = payload()
    del body["chargers"]
    assert post(body).status_code == 422


def test_chain_must_connect_at_one_station():
    # T2 starts at B but TX ends at C.
    trips = base_trips() + [trip("TX", ("A", 0, 10), ("C", 20, 20))]
    body = payload(trips=trips, trip_ids=["TX", "T2"],
                   consumptions={"TX": 1, "T2": 1})
    assert post(body).status_code == 422


def test_next_departure_must_not_precede_previous_arrival():
    # T2 leaves B at 110, before T1 arrives there at 120.
    trips = [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 110, 110), ("C", 200, 200)),
    ]
    body = payload(trips=trips, trip_ids=["T1", "T2"],
                   consumptions={"T1": 1, "T2": 1})
    assert post(body).status_code == 422
    # Boundary: departing exactly at the arrival minute is legal.
    trips[1]["stops"][0]["arrival"] = 120
    trips[1]["stops"][0]["departure"] = 120
    assert post(payload(trips=trips, trip_ids=["T1", "T2"],
                        consumptions={"T1": 1, "T2": 1})).status_code == 200


def test_network_validation_is_reused():
    body = payload()
    body["trips"][0]["stops"].append(stop("ZZ", 300, 300))  # unknown station
    assert post(body).status_code == 422

    body = payload()
    body["trips"].append(trip("T1", ("A", 1, 2), ("B", 3, 3)))  # duplicate id
    assert post(body).status_code == 422

    assert post(payload(trips=[])).status_code == 422
    assert post(payload(stations=["ONLY"])).status_code == 422


def test_extra_field_returns_422():
    body = payload()
    body["nope"] = 1
    assert post(body).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/fleet/charge",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def brute_force(body):
    """Exhaustive reference: (feasible, total_cents, charges)."""
    trips = {t["id"]: t for t in body["trips"]}
    chargers = {c["station"]: c for c in body["chargers"]}
    ids = body["trip_ids"]
    cons = [body["consumptions"][tid] for tid in ids]
    capacity = body["capacity"]
    gaps = []
    for i in range(len(ids) - 1):
        station = trips[ids[i]]["stops"][-1]["station"]
        minutes = (
            trips[ids[i + 1]]["stops"][0]["departure"]
            - trips[ids[i]]["stops"][-1]["arrival"]
        )
        chg = chargers.get(station)
        limit = chg["rate"] * minutes if chg else 0
        price = chg["price_cents"] if chg else 0
        gaps.append((min(limit, capacity), price))
    best = None
    for amounts in product(*[range(limit + 1) for limit, _ in gaps]):
        battery = body["initial_charge"]
        cost = 0
        ok = True
        for i in range(len(ids)):
            if battery < cons[i]:
                ok = False
                break
            battery -= cons[i]
            if i < len(amounts):
                if battery + amounts[i] > capacity:
                    ok = False
                    break
                battery += amounts[i]
                cost += amounts[i] * gaps[i][1]
        if ok:
            label = (cost, amounts)
            if best is None or label < best:
                best = label
    if best is None:
        return False, None, []
    return True, best[0], list(best[1])


def random_case(rng):
    """Build a small random valid request plus its brute-force reference."""
    n = rng.randint(1, 4)
    stations = [f"S{i}" for i in range(n + 1)]
    trips = []
    now = 0
    for i in range(n):
        start = now + rng.randint(0, 4)
        end = start + rng.randint(1, 8)
        trips.append(trip(f"T{i}", (stations[i], max(0, start - 1), start),
                          (stations[i + 1], end, end)))
        now = end + rng.randint(0, 6)
    ids = [f"T{i}" for i in range(n)]
    capacity = rng.randint(1, 8)
    body = {
        "stations": stations,
        "trips": trips,
        "trip_ids": ids,
        "consumptions": {tid: rng.randint(1, 8) for tid in ids},
        "capacity": capacity,
        "initial_charge": rng.randint(0, capacity),
        "chargers": [
            charger(station, rng.randint(1, 3), rng.randint(0, 5))
            for station in stations
            if rng.random() < 0.6
        ],
    }
    return body, brute_force(body)


def test_matches_brute_force_on_random_scenarios():
    rng = random.Random(20260926)
    for _ in range(150):
        body, (feasible, total, charges) = random_case(rng)
        r = post(body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["feasible"] is feasible
        assert out["total_cents"] == total
        assert out["charges"] == charges
        if feasible:
            assert simulate(body, out["charges"]) == total


def test_maximum_size_chain_stays_fast_and_valid():
    # 12 trips in one chain, hourly, 30-minute stops, chargers everywhere.
    stations = [f"S{i}" for i in range(13)]
    trips = []
    for i in range(12):
        start = i * 60
        trips.append(trip(f"T{i}", (stations[i], max(0, start - 5), start),
                          (stations[i + 1], start + 30, start + 30)))
    ids = [f"T{i}" for i in range(12)]
    body = {
        "stations": stations,
        "trips": trips,
        "trip_ids": ids,
        # Every trip burns 20..27 units while even the slowest charger
        # delivers 1 * 30 = 30 units per stop: certainly feasible.
        "consumptions": {tid: 20 + (i % 8) for i, tid in enumerate(ids)},
        "capacity": 60,
        "initial_charge": 60,
        "chargers": [
            charger(station, 1 + (i % 3), (i * 7) % 11)
            for i, station in enumerate(stations[1:-1], start=1)
        ],
    }
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["feasible"] is True
    assert len(out["charges"]) == 11
    assert simulate(body, out["charges"]) == out["total_cents"]
