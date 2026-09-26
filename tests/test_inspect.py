"""End-to-end tests for POST /v1/fleet/inspect."""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def trip(tid, *stops):
    return {"id": tid, "stops": [stop(*s) for s in stops]}


def workshop(station, open_, close, minutes, price_cents):
    return {
        "station": station,
        "open": open_,
        "close": close,
        "minutes": minutes,
        "price_cents": price_cents,
    }


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
        "trip_km": {"T1": 100, "T2": 100, "T3": 100},
        "limit_km": 150,
        "initial_km": 0,
        "workshops": [
            workshop("B", 0, 2879, 30, 500),
            workshop("C", 0, 2879, 10, 100),
        ],
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/fleet/inspect", json=body)


def simulate(body, inspections):
    """Independent re-check: the returned plan is legal and costs the
    reported total.  Returns the total price."""
    trips = {t["id"]: t for t in body["trips"]}
    workshops = {w["station"]: w for w in body["workshops"]}
    ids = body["trip_ids"]
    indices = [entry["index"] for entry in inspections]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    assert all(0 <= i < len(ids) - 1 for i in indices)
    total = 0
    mileage = body["initial_km"]
    for i, tid in enumerate(ids):
        mileage += body["trip_km"][tid]
        assert mileage <= body["limit_km"]
        if i == len(ids) - 1:
            break
        if i in indices:
            entry = inspections[indices.index(i)]
            station = trips[tid]["stops"][-1]["station"]
            assert entry["station"] == station
            ws = workshops[station]
            arrival = trips[tid]["stops"][-1]["arrival"]
            departure = trips[ids[i + 1]]["stops"][0]["departure"]
            assert (
                min(departure, ws["close"]) - max(arrival, ws["open"])
                >= ws["minutes"]
            )
            total += ws["price_cents"]
            mileage = 0
    return total


# ---------------------------------------------------------------- happy path

def test_forced_inspections_at_both_connections():
    # 100 km per trip against a 150 km limit: the mileage after T1 is 100,
    # so T2 can only start after an inspection at B; the same again at C.
    r = post(payload())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "feasible": True,
        "inspections": [
            {"index": 0, "station": "B"},
            {"index": 1, "station": "C"},
        ],
        "total_cents": 600,
    }


def test_one_inspection_at_the_cheaper_connection():
    # Limit 250: a single inspection suffices, either at B (500) or at C
    # (100); the cheaper one wins.
    body = payload(limit_km=250)
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"index": 1, "station": "C"}],
        "total_cents": 100,
    }
    assert simulate(body, out["inspections"]) == out["total_cents"]


def test_count_beats_price():
    # Six trips, 100 km each, limit 250: segments of at most two trips,
    # so exactly two inspections are forced — at connections 1 and 3
    # (stations C and E), costing 1000 each.  Inspecting three times at
    # the free stations B, D, F would cost 0, but more inspections lose.
    trips = [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 150, 150), ("C", 220, 220)),
        trip("T3", ("C", 250, 250), ("D", 320, 320)),
        trip("T4", ("D", 350, 350), ("E", 420, 420)),
        trip("T5", ("E", 450, 450), ("F", 520, 520)),
        trip("T6", ("F", 550, 550), ("G", 620, 620)),
    ]
    body = payload(
        stations=["A", "B", "C", "D", "E", "F", "G"],
        trips=trips,
        trip_ids=[f"T{i}" for i in range(1, 7)],
        trip_km={f"T{i}": 100 for i in range(1, 7)},
        limit_km=250,
        workshops=[
            workshop("B", 0, 2879, 30, 0),
            workshop("C", 0, 2879, 30, 1000),
            workshop("D", 0, 2879, 30, 0),
            workshop("E", 0, 2879, 30, 1000),
            workshop("F", 0, 2879, 30, 0),
        ],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [
            {"index": 1, "station": "C"},
            {"index": 3, "station": "E"},
        ],
        "total_cents": 2000,
    }
    assert simulate(body, out["inspections"]) == out["total_cents"]


def test_equal_costs_take_the_lexicographically_smallest_indices():
    # Five trips, 100 km each, limit 200: exactly two inspections are
    # needed; the feasible index sets are [0, 2], [1, 2] and [1, 3], all
    # costing 200, and [0, 2] is the lexicographically smallest.
    trips = [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 150, 150), ("C", 220, 220)),
        trip("T3", ("C", 250, 250), ("D", 320, 320)),
        trip("T4", ("D", 350, 350), ("E", 420, 420)),
        trip("T5", ("E", 450, 450), ("F", 520, 520)),
    ]
    body = payload(
        stations=["A", "B", "C", "D", "E", "F"],
        trips=trips,
        trip_ids=["T1", "T2", "T3", "T4", "T5"],
        trip_km={f"T{i}": 100 for i in range(1, 6)},
        limit_km=200,
        workshops=[
            workshop("B", 0, 2879, 30, 100),
            workshop("C", 0, 2879, 30, 100),
            workshop("D", 0, 2879, 30, 100),
            workshop("E", 0, 2879, 30, 100),
        ],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [
            {"index": 0, "station": "B"},
            {"index": 2, "station": "D"},
        ],
        "total_cents": 200,
    }
    assert simulate(body, out["inspections"]) == out["total_cents"]


def test_no_inspection_when_the_limit_is_never_reached():
    body = payload(limit_km=300)
    out = post(body).json()
    assert out == {"feasible": True, "inspections": [], "total_cents": 0}


def test_free_inspections_are_still_minimized():
    # Price zero does not make extra inspections attractive: count first.
    body = payload(
        limit_km=250,
        workshops=[
            workshop("B", 0, 2879, 30, 0),
            workshop("C", 0, 2879, 10, 0),
        ],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"index": 0, "station": "B"}],
        "total_cents": 0,
    }


def test_initial_km_counts_towards_the_limit():
    # 50 + 100 = 150 after T1 and 250 after T2 (exactly the limit), so
    # the single inspection moves to C: T3 then ends at 100.
    body = payload(limit_km=250, initial_km=50)
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"index": 1, "station": "C"}],
        "total_cents": 100,
    }
    # 150 initial kilometres still fit the limit exactly after T1
    # (150 + 100 = 250), but then the inspection must happen at B.
    body["initial_km"] = 150
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"index": 0, "station": "B"}],
        "total_cents": 500,
    }
    # One more kilometre is a legal request but breaks the first trip.
    body["initial_km"] = 151
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_single_trip_chain_needs_no_inspection():
    body = payload(
        trip_ids=["T1"],
        trip_km={"T1": 100},
        limit_km=150,
        initial_km=50,
        workshops=[],
    )
    assert post(body).json() == {
        "feasible": True,
        "inspections": [],
        "total_cents": 0,
    }
    body["initial_km"] = 51
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_workshop_at_non_gap_station_does_not_count():
    # Workshops sit at A (chain start) and D (chain end): neither is a
    # stop *between* two trips, so the chain must run within the limit.
    body = payload(
        limit_km=300,
        workshops=[workshop("A", 0, 2879, 1, 0), workshop("D", 0, 2879, 1, 0)],
    )
    out = post(body).json()
    assert out == {"feasible": True, "inspections": [], "total_cents": 0}
    body["limit_km"] = 299
    out = post(body).json()
    assert out == {"feasible": False, "inspections": [], "total_cents": None}


def test_no_workshop_configured_anywhere():
    body = payload(limit_km=300, workshops=[])
    out = post(body).json()
    assert out == {"feasible": True, "inspections": [], "total_cents": 0}


# ------------------------------------------------------------- timing window

def test_duration_must_fit_the_intersection_of_stop_and_open_hours():
    # The B stop is [120, 150]; a workshop open [0, 140) leaves only
    # 20 minutes, too short for the 30-minute job: T2 cannot be reached.
    body = payload(workshops=[workshop("B", 0, 140, 30, 500),
                              workshop("C", 0, 2879, 10, 100)])
    out = post(body).json()
    assert out == {"feasible": False, "inspections": [], "total_cents": None}
    # Closing at 150 gives exactly 30 minutes: the job fits on the boundary.
    body["workshops"][0]["close"] = 150
    out = post(body).json()
    assert out["feasible"] is True
    assert simulate(body, out["inspections"]) == out["total_cents"]


def test_open_time_cuts_into_the_stop_window():
    # Opening at 121 leaves 29 minutes of the 30-minute stop: too short.
    body = payload(workshops=[workshop("B", 121, 2879, 30, 500),
                              workshop("C", 0, 2879, 10, 100)])
    assert post(body).json()["feasible"] is False
    # Opening exactly at the arrival minute fits exactly.
    body["workshops"][0]["open"] = 120
    assert post(body).json()["feasible"] is True


def test_zero_minute_stop_allows_no_inspection():
    # T2 departs B exactly when T1 arrives: the stop is 0 minutes.
    body = payload(
        trips=[
            trip("T1", ("A", 0, 60), ("B", 120, 120)),
            trip("T2", ("B", 120, 120), ("C", 200, 200)),
        ],
        trip_ids=["T1", "T2"],
        trip_km={"T1": 100, "T2": 100},
        limit_km=150,
        workshops=[workshop("B", 0, 2879, 1, 10)],
    )
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }
    body["limit_km"] = 200
    assert post(body).json() == {
        "feasible": True,
        "inspections": [],
        "total_cents": 0,
    }


# --------------------------------------------------------------- infeasible

def test_first_trip_beyond_the_limit_is_infeasible():
    body = payload(trip_km={"T1": 100, "T2": 1, "T3": 1}, initial_km=100)
    # 100 + 100 > 150 and no inspection is possible before the first trip.
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_single_trip_km_beyond_the_limit_is_infeasible():
    # T2 alone adds 200 km; even a fresh zero at B cannot keep it <= 150.
    body = payload(trip_km={"T1": 100, "T2": 200, "T3": 100})
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_mid_chain_wall_without_a_workshop_is_infeasible():
    # No workshop at B: T1 + T2 = 200 > 150 cannot be avoided.
    body = payload(workshops=[workshop("C", 0, 2879, 10, 100)])
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


# ------------------------------------------------------------ 422 validation

def test_trip_ids_validation():
    assert post(payload(trip_ids=[])).status_code == 422
    many = [f"TX{i}" for i in range(41)]
    assert post(payload(trip_ids=many)).status_code == 422
    assert post(payload(trip_ids=["T1", "T2", "T1"])).status_code == 422
    assert post(payload(trip_ids=["T1", "TX", "T3"])).status_code == 422
    body = payload()
    del body["trip_ids"]
    assert post(body).status_code == 422


def test_trip_km_must_cover_exactly_the_chain():
    # Missing a chained trip.
    assert post(payload(trip_km={"T1": 100, "T2": 100})).status_code == 422
    # A trip outside the chain.
    extra = {"T1": 100, "T2": 100, "T3": 100, "T9": 1}
    assert post(payload(trip_km=extra)).status_code == 422
    # Values outside 1..300.
    bad_low = {"T1": 0, "T2": 100, "T3": 100}
    assert post(payload(trip_km=bad_low)).status_code == 422
    bad_high = {"T1": 100, "T2": 301, "T3": 100}
    assert post(payload(trip_km=bad_high)).status_code == 422
    body = payload()
    del body["trip_km"]
    assert post(body).status_code == 422


def test_mileage_fields_validation():
    assert post(payload(limit_km=0)).status_code == 422
    assert post(payload(limit_km=301)).status_code == 422
    assert post(payload(initial_km=-1)).status_code == 422
    # initial_km must not exceed limit_km.
    assert post(payload(initial_km=151)).status_code == 422
    assert post(payload(initial_km=150)).status_code == 200
    for field in ("limit_km", "initial_km"):
        body = payload()
        del body[field]
        assert post(body).status_code == 422


def test_workshop_validation():
    # Station must be unique and declared.
    dup = [workshop("B", 0, 100, 10, 1), workshop("B", 0, 200, 10, 1)]
    assert post(payload(workshops=dup)).status_code == 422
    assert post(payload(workshops=[workshop("Z", 0, 100, 10, 1)])).status_code == 422
    # The open interval must be non-empty and within 0..2879.
    assert post(payload(workshops=[workshop("B", 100, 100, 10, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 200, 100, 10, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", -1, 100, 10, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 0, 2880, 10, 1)])).status_code == 422
    # Minutes 1..120, price non-negative.
    assert post(payload(workshops=[workshop("B", 0, 2879, 0, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 0, 2879, 121, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 0, 2879, 10, -1)])).status_code == 422
    # Free inspection is legal.
    assert post(payload(workshops=[workshop("B", 0, 2879, 30, 0)])).status_code == 200
    # More than 40 workshops is rejected even before station checks.
    many = [workshop("B", 0, 2879, 10, 1)] * 41
    assert post(payload(workshops=many)).status_code == 422
    # Extra fields inside a workshop are rejected.
    body = payload()
    body["workshops"][0]["nope"] = 1
    assert post(body).status_code == 422
    body = payload()
    del body["workshops"]
    assert post(body).status_code == 422


def test_chain_must_connect_at_one_station():
    # T2 starts at B but TX ends at C.
    trips = base_trips() + [trip("TX", ("A", 0, 10), ("C", 20, 20))]
    body = payload(trips=trips, trip_ids=["TX", "T2"],
                   trip_km={"TX": 1, "T2": 1})
    assert post(body).status_code == 422


def test_next_departure_must_not_precede_previous_arrival():
    # T2 leaves B at 110, before T1 arrives there at 120.
    trips = [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 110, 110), ("C", 200, 200)),
    ]
    body = payload(trips=trips, trip_ids=["T1", "T2"],
                   trip_km={"T1": 1, "T2": 1})
    assert post(body).status_code == 422
    # Boundary: departing exactly at the arrival minute is legal.
    trips[1]["stops"][0]["arrival"] = 120
    trips[1]["stops"][0]["departure"] = 120
    assert post(payload(trips=trips, trip_ids=["T1", "T2"],
                        trip_km={"T1": 1, "T2": 1})).status_code == 200


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
        "/v1/fleet/inspect",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def brute_force(body):
    """Exhaustive reference: (feasible, indices, total_cents)."""
    trips = {t["id"]: t for t in body["trips"]}
    workshops = {w["station"]: w for w in body["workshops"]}
    ids = body["trip_ids"]
    kms = [body["trip_km"][tid] for tid in ids]
    limit = body["limit_km"]
    fits = []
    prices = []
    for i in range(len(ids) - 1):
        station = trips[ids[i]]["stops"][-1]["station"]
        arrival = trips[ids[i]]["stops"][-1]["arrival"]
        departure = trips[ids[i + 1]]["stops"][0]["departure"]
        ws = workshops.get(station)
        fits.append(
            ws is not None
            and min(departure, ws["close"]) - max(arrival, ws["open"])
            >= ws["minutes"]
        )
        prices.append(ws["price_cents"] if ws is not None else 0)
    best = None
    for mask in range(1 << (len(ids) - 1)):
        chosen = [i for i in range(len(ids) - 1) if mask >> i & 1]
        if any(not fits[i] for i in chosen):
            continue
        mileage = body["initial_km"]
        ok = True
        for i in range(len(ids)):
            mileage += kms[i]
            if mileage > limit:
                ok = False
                break
            if i < len(ids) - 1 and mask >> i & 1:
                mileage = 0
        if not ok:
            continue
        label = (len(chosen), sum(prices[i] for i in chosen), tuple(chosen))
        if best is None or label < best:
            best = label
    if best is None:
        return False, [], None
    return True, list(best[2]), best[1]


def random_case(rng):
    """Build a small random valid request plus its brute-force reference."""
    n = rng.randint(1, 6)
    stations = [f"S{i}" for i in range(n + 1)]
    trips = []
    now = 0
    for i in range(n):
        start = now + rng.randint(0, 30)
        end = start + rng.randint(1, 60)
        trips.append(trip(f"T{i}", (stations[i], max(0, start - 1), start),
                          (stations[i + 1], end, end)))
        now = end
    ids = [f"T{i}" for i in range(n)]
    limit = rng.randint(50, 300)
    body = {
        "stations": stations,
        "trips": trips,
        "trip_ids": ids,
        "trip_km": {tid: rng.randint(1, 200) for tid in ids},
        "limit_km": limit,
        "initial_km": rng.randint(0, limit),
        "workshops": [
            workshop(
                station,
                rng.randint(0, 200),
                rng.randint(201, 500),
                rng.randint(1, 120),
                rng.randint(0, 500),
            )
            for station in stations
            if rng.random() < 0.5
        ],
    }
    return body, brute_force(body)


def test_matches_brute_force_on_random_scenarios():
    rng = random.Random(20260926)
    for _ in range(300):
        body, (feasible, indices, total) = random_case(rng)
        r = post(body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["feasible"] is feasible
        assert [entry["index"] for entry in out["inspections"]] == indices
        assert out["total_cents"] == total
        if feasible:
            assert simulate(body, out["inspections"]) == total


def test_maximum_size_chain_stays_fast_and_valid():
    # 40 trips in one chain shuttling between two stations (the network
    # allows at most 40 stations), hourly, 30-minute stops.
    stations = ["S0", "S1"]
    trips = []
    for i in range(40):
        start = i * 60
        trips.append(trip(f"T{i}", (stations[i % 2], max(0, start - 5), start),
                          (stations[(i + 1) % 2], start + 30, start + 30)))
    ids = [f"T{i}" for i in range(40)]
    body = {
        "stations": stations,
        "trips": trips,
        "trip_ids": ids,
        # 100 km per trip against a 300 km limit forces an inspection
        # every three trips (13 in total); every stop fits the 30-minute
        # job, so the cheapest stations are picked.
        "trip_km": {tid: 100 for tid in ids},
        "limit_km": 300,
        "initial_km": 0,
        "workshops": [
            workshop("S0", 0, 2879, 30, 3),
            workshop("S1", 0, 2879, 30, 7),
        ],
    }
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["feasible"] is True
    assert len(out["inspections"]) == 13
    assert simulate(body, out["inspections"]) == out["total_cents"]
