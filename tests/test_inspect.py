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


def workshop(station, open_, close, duration_minutes, cost_cents):
    return {
        "station": station,
        "open": open_,
        "close": close,
        "duration_minutes": duration_minutes,
        "cost_cents": cost_cents,
    }


def base_trips():
    # T1 ends at B at 120, T2 leaves B at 150 (30-minute stop);
    # T2 ends at C at 220, T3 leaves C at 240 (20-minute stop).
    return [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 150, 150), ("C", 220, 220)),
        trip("T3", ("C", 240, 240), ("D", 300, 300)),
    ]


def payload(**overrides):
    base = {
        "stations": ["A", "B", "C", "D"],
        "trips": base_trips(),
        "trip_ids": ["T1", "T2", "T3"],
        "trip_km": {"T1": 120, "T2": 150, "T3": 100},
        "limit_km": 200,
        "initial_km": 50,
        "workshops": [
            workshop("B", 100, 200, 20, 300),
            workshop("C", 200, 250, 20, 100),
        ],
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/fleet/inspect", json=body)


def simulate(body, inspections):
    """Independent re-check: the returned plan is feasible and costs the
    reported total.  Returns the total cost in cents."""
    trips = {t["id"]: t for t in body["trips"]}
    workshops = {w["station"]: w for w in body["workshops"]}
    ids = body["trip_ids"]
    at = {entry["connection_index"]: entry for entry in inspections}
    assert len(at) == len(inspections)  # at most one inspection per gap
    assert all(0 <= i < len(ids) - 1 for i in at)  # only between two trips
    assert [e["connection_index"] for e in inspections] == sorted(at)
    km = body["initial_km"]
    total = 0
    for i, tid in enumerate(ids):
        km += body["trip_km"][tid]
        assert km <= body["limit_km"]
        if i == len(ids) - 1:
            break
        if i in at:
            entry = at[i]
            station = trips[tid]["stops"][-1]["station"]
            assert entry["station"] == station
            w = workshops[station]
            start = max(trips[tid]["stops"][-1]["arrival"], w["open"])
            end = min(
                trips[ids[i + 1]]["stops"][0]["departure"], w["close"]
            )
            assert start + w["duration_minutes"] <= end
            total += w["cost_cents"]
            km = 0
    return total


# ---------------------------------------------------------------- happy path

def test_two_inspections_cover_the_whole_chain():
    # After T1 the odometer reads 50 + 120 = 170; T2 would push it to 320,
    # so an inspection at B is forced (fits [120, 150) at cost 300).  T3
    # would then push 150 + 100 = 250 past 200, forcing C as well — the
    # 20-minute job fits [220, 240) exactly.  Total 300 + 100 = 400.
    r = post(payload())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out == {
        "feasible": True,
        "inspections": [
            {"connection_index": 0, "station": "B"},
            {"connection_index": 1, "station": "C"},
        ],
        "total_cents": 400,
    }
    assert simulate(payload(), out["inspections"]) == 400


def test_no_inspection_when_the_limit_never_binds():
    body = payload(
        trip_km={"T1": 50, "T2": 60, "T3": 40},
        initial_km=0,
    )
    out = post(body).json()
    assert out == {"feasible": True, "inspections": [], "total_cents": 0}


def test_single_trip_chain_needs_no_inspection():
    body = payload(
        trip_ids=["T1"],
        trip_km={"T1": 100},
        initial_km=100,
        workshops=[],
    )
    assert post(body).json() == {
        "feasible": True,
        "inspections": [],
        "total_cents": 0,
    }
    # One kilometre more and nothing can help: no inspection is possible
    # before the first trip.
    body["initial_km"] = 101
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_inspection_resets_the_odometer():
    body = payload(
        trip_ids=["T1", "T2"],
        trip_km={"T1": 150, "T2": 150},
        initial_km=0,
        workshops=[workshop("B", 0, 2879, 10, 700)],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"connection_index": 0, "station": "B"}],
        "total_cents": 700,
    }
    assert simulate(body, out["inspections"]) == 700


def test_job_must_fit_the_intersection_of_stop_and_open_interval():
    # The stop at B is [120, 150]; a 30-minute job fits exactly.
    body = payload(
        trip_ids=["T1", "T2"],
        trip_km={"T1": 150, "T2": 150},
        initial_km=0,
        workshops=[workshop("B", 0, 2879, 30, 10)],
    )
    assert post(body).json()["feasible"] is True
    # 31 minutes no longer fit the 30-minute stop.
    body["workshops"][0]["duration_minutes"] = 31
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_open_interval_can_clip_the_usable_window():
    # The workshop opens at 130, so the job occupies [130, 150): 20 minutes
    # fit exactly, 21 do not.
    body = payload(
        trip_ids=["T1", "T2"],
        trip_km={"T1": 150, "T2": 150},
        initial_km=0,
        workshops=[workshop("B", 130, 2879, 20, 10)],
    )
    assert post(body).json()["feasible"] is True
    body["workshops"][0]["duration_minutes"] = 21
    assert post(body).json()["feasible"] is False
    # Closing at 140 also clips: [120, 140) hosts exactly 20 minutes.
    body["workshops"][0] = workshop("B", 0, 140, 20, 10)
    assert post(body).json()["feasible"] is True
    body["workshops"][0] = workshop("B", 0, 139, 20, 10)
    assert post(body).json()["feasible"] is False


def test_zero_minute_stop_hosts_no_inspection():
    trips = [
        trip("T1", ("A", 0, 60), ("B", 120, 120)),
        trip("T2", ("B", 120, 120), ("C", 200, 200)),
    ]
    body = payload(
        trips=trips,
        trip_ids=["T1", "T2"],
        trip_km={"T1": 50, "T2": 50},
        initial_km=0,
        workshops=[workshop("B", 0, 2879, 1, 10)],
    )
    assert post(body).json() == {
        "feasible": True,
        "inspections": [],
        "total_cents": 0,
    }
    # Needing a reset at the zero-minute stop makes the chain infeasible.
    body["trip_km"] = {"T1": 150, "T2": 150}
    assert post(body).json()["feasible"] is False


def test_cheapest_connection_wins():
    # One inspection is forced.  Resetting at B (conn 0) leaves 50 + 100 =
    # 150 after T3; resetting at C (conn 1) leaves 150 after T2 and 100
    # after T3 — both work exactly once, so the cheaper workshop wins.
    body = payload(
        trip_km={"T1": 100, "T2": 50, "T3": 100},
        limit_km=150,
        initial_km=0,
        workshops=[
            workshop("B", 0, 2879, 10, 500),
            workshop("C", 0, 2879, 10, 200),
        ],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"connection_index": 1, "station": "C"}],
        "total_cents": 200,
    }
    assert simulate(body, out["inspections"]) == 200


def test_equal_costs_take_the_lexicographically_smallest_indices():
    # Same situation, but both workshops cost 200: [0] beats [1].
    body = payload(
        trip_km={"T1": 100, "T2": 50, "T3": 100},
        limit_km=150,
        initial_km=0,
        workshops=[
            workshop("B", 0, 2879, 10, 200),
            workshop("C", 0, 2879, 10, 200),
        ],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"connection_index": 0, "station": "B"}],
        "total_cents": 200,
    }


def test_count_is_minimised_before_cost():
    # Four trips, km 100/50/50/100 against a 150 limit.  The only
    # one-inspection plan resets at C (conn 1) for 1000 cents; resetting
    # at B and D (conns 0 and 2) would cost only 20 — but two inspections
    # lose to one no matter how cheap they are.
    trips = base_trips() + [trip("T4", ("D", 350, 350), ("E", 420, 420))]
    body = payload(
        stations=["A", "B", "C", "D", "E"],
        trips=trips,
        trip_ids=["T1", "T2", "T3", "T4"],
        trip_km={"T1": 100, "T2": 50, "T3": 50, "T4": 100},
        limit_km=150,
        initial_km=0,
        workshops=[
            workshop("B", 0, 2879, 10, 10),
            workshop("C", 0, 2879, 10, 1000),
            workshop("D", 0, 2879, 10, 10),
        ],
    )
    out = post(body).json()
    assert out == {
        "feasible": True,
        "inspections": [{"connection_index": 1, "station": "C"}],
        "total_cents": 1000,
    }
    assert simulate(body, out["inspections"]) == 1000


def test_free_inspections_still_minimise_the_count():
    body = payload(
        workshops=[
            workshop("B", 100, 200, 20, 0),
            workshop("C", 200, 250, 20, 0),
        ],
    )
    out = post(body).json()
    # The base chain needs exactly two resets; free or not, both appear.
    assert out["inspections"] == [
        {"connection_index": 0, "station": "B"},
        {"connection_index": 1, "station": "C"},
    ]
    assert out["total_cents"] == 0


def test_workshop_at_non_gap_station_does_not_count():
    # Workshops sit at A (chain start) and D (chain end): neither lies
    # *between* two trips, so the chain must run within the limit.
    body = payload(
        trip_km={"T1": 50, "T2": 60, "T3": 40},
        initial_km=0,
        workshops=[workshop("A", 0, 2879, 1, 0), workshop("D", 0, 2879, 1, 0)],
    )
    assert post(body).json() == {
        "feasible": True,
        "inspections": [],
        "total_cents": 0,
    }
    body["trip_km"] = {"T1": 150, "T2": 150, "T3": 100}
    assert post(body).json()["feasible"] is False


# --------------------------------------------------------------- infeasible

def test_trip_longer_than_the_limit_is_infeasible():
    body = payload(trip_km={"T1": 250, "T2": 10, "T3": 10}, initial_km=0)
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_first_trip_beyond_initial_plus_limit_is_infeasible():
    # No inspection is possible before the first trip.
    body = payload(trip_km={"T1": 100, "T2": 10, "T3": 10}, initial_km=150)
    assert post(body).json()["feasible"] is False


def test_no_workshop_where_the_reset_is_needed():
    body = payload(workshops=[workshop("C", 200, 250, 20, 100)])
    assert post(body).json() == {
        "feasible": False,
        "inspections": [],
        "total_cents": None,
    }


def test_no_workshop_configured_anywhere():
    body = payload(
        trip_km={"T1": 50, "T2": 60, "T3": 40},
        initial_km=0,
        workshops=[],
    )
    assert post(body).json() == {
        "feasible": True,
        "inspections": [],
        "total_cents": 0,
    }
    body["trip_km"] = {"T1": 150, "T2": 150, "T3": 100}
    assert post(body).json()["feasible"] is False


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
    assert post(payload(trip_km={"T1": 10, "T2": 20})).status_code == 422
    # A trip outside the chain.
    extra = {"T1": 10, "T2": 20, "T3": 5, "T9": 1}
    assert post(payload(trip_km=extra)).status_code == 422
    # Values outside 1..300.
    bad_low = {"T1": 0, "T2": 20, "T3": 5}
    assert post(payload(trip_km=bad_low)).status_code == 422
    bad_high = {"T1": 10, "T2": 301, "T3": 5}
    assert post(payload(trip_km=bad_high)).status_code == 422
    body = payload()
    del body["trip_km"]
    assert post(body).status_code == 422


def test_limit_and_initial_km_validation():
    assert post(payload(limit_km=0)).status_code == 422
    assert post(payload(limit_km=301)).status_code == 422
    assert post(payload(initial_km=-1)).status_code == 422
    # initial_km must not exceed limit_km.
    assert post(payload(initial_km=201)).status_code == 422
    assert post(payload(initial_km=200)).status_code == 200
    for field in ("limit_km", "initial_km"):
        body = payload()
        del body[field]
        assert post(body).status_code == 422


def test_workshop_validation():
    # Station must be unique and declared.
    dup = [workshop("B", 0, 100, 10, 1), workshop("B", 0, 100, 10, 2)]
    assert post(payload(workshops=dup)).status_code == 422
    assert post(payload(workshops=[workshop("Z", 0, 100, 10, 1)])).status_code == 422
    # The open interval must be non-empty and inside 0..2879.
    assert post(payload(workshops=[workshop("B", 100, 100, 10, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 200, 100, 10, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", -1, 100, 10, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 0, 2880, 10, 1)])).status_code == 422
    # Duration 1..120, cost non-negative.
    assert post(payload(workshops=[workshop("B", 0, 100, 0, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 0, 100, 121, 1)])).status_code == 422
    assert post(payload(workshops=[workshop("B", 0, 100, 10, -1)])).status_code == 422
    # Free inspections are legal.
    assert post(payload(workshops=[workshop("B", 100, 200, 20, 0),
                                   workshop("C", 200, 250, 20, 0)])).status_code == 200
    # More than 40 workshops is rejected even before station checks.
    many = [workshop("B", 0, 100, 10, 1)] * 41
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
    n = len(ids)
    hostable = []
    for i in range(n - 1):
        station = trips[ids[i]]["stops"][-1]["station"]
        w = workshops.get(station)
        if w is None:
            hostable.append(None)
            continue
        start = max(trips[ids[i]]["stops"][-1]["arrival"], w["open"])
        end = min(trips[ids[i + 1]]["stops"][0]["departure"], w["close"])
        fits = start + w["duration_minutes"] <= end
        hostable.append(w["cost_cents"] if fits else None)
    best = None
    for mask in range(1 << (n - 1)):
        km = body["initial_km"]
        cost = 0
        indices = []
        ok = True
        for i in range(n):
            km += kms[i]
            if km > limit:
                ok = False
                break
            if i < n - 1 and (mask >> i) & 1:
                if hostable[i] is None:
                    ok = False
                    break
                km = 0
                cost += hostable[i]
                indices.append(i)
        if ok:
            label = (len(indices), cost, indices)
            if best is None or label < best:
                best = label
    if best is None:
        return False, [], None
    return True, best[2], best[1]


def random_case(rng):
    """Build a small random valid request plus its brute-force reference."""
    n = rng.randint(1, 5)
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
    limit = rng.randint(1, 12)
    workshops = []
    for station in stations:
        if rng.random() < 0.6:
            open_ = rng.randint(0, 20)
            workshops.append(workshop(
                station,
                open_,
                open_ + rng.randint(1, 30),
                rng.randint(1, 8),
                rng.randint(0, 9),
            ))
    body = {
        "stations": stations,
        "trips": trips,
        "trip_ids": ids,
        "trip_km": {tid: rng.randint(1, 12) for tid in ids},
        "limit_km": limit,
        "initial_km": rng.randint(0, limit),
        "workshops": workshops,
    }
    return body, brute_force(body)


def test_matches_brute_force_on_random_scenarios():
    rng = random.Random(20260926)
    for _ in range(150):
        body, (feasible, indices, total) = random_case(rng)
        r = post(body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["feasible"] is feasible
        assert out["total_cents"] == total
        assert [e["connection_index"] for e in out["inspections"]] == indices
        if feasible:
            assert simulate(body, out["inspections"]) == total


def test_maximum_size_chain_stays_fast_and_valid():
    # 40 trips in one chain, hourly, 30-minute stops, workshops everywhere;
    # the last trip returns to S0 so the chain stays within 40 stations.
    stations = [f"S{i}" for i in range(40)]
    trips = []
    for i in range(40):
        start = i * 60
        trips.append(trip(f"T{i}", (stations[i], max(0, start - 5), start),
                          (stations[(i + 1) % 40], start + 30, start + 30)))
    ids = [f"T{i}" for i in range(40)]
    body = {
        "stations": stations,
        "trips": trips,
        "trip_ids": ids,
        # 100 km per trip against a 250 km limit forces a reset after
        # every second trip: connections 1, 3, ..., 37 — 19 inspections.
        "trip_km": {tid: 100 for tid in ids},
        "limit_km": 250,
        "initial_km": 0,
        "workshops": [
            workshop(station, 0, 2879, 10, (i * 7) % 11 + 1)
            for i, station in enumerate(stations[1:], start=1)
        ],
    }
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["feasible"] is True
    assert [e["connection_index"] for e in out["inspections"]] == list(
        range(1, 39, 2)
    )
    assert simulate(body, out["inspections"]) == out["total_cents"]
