"""End-to-end tests for POST /v1/fleet/assign."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def trip(tid, *stops):
    return {"id": tid, "stops": [stop(*s) for s in stops]}


def payload(**overrides):
    base = {
        "stations": ["A", "B", "C", "D"],
        "trips": [
            trip("T1", ("A", 0, 60), ("B", 120, 120)),
            trip("T2", ("B", 130, 130), ("C", 200, 200)),
            trip("T3", ("A", 70, 80), ("D", 150, 150)),
        ],
        "turnaround_minutes": 10,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/fleet/assign", json=body)


def assert_valid_assignment(body, trips, turnaround):
    """Response is a feasible assignment covering every trip exactly once."""
    vehicles = body["vehicles"]
    assert body["vehicle_count"] == len(vehicles)

    by_id = {t["id"]: t for t in trips}
    seen = [tid for v in vehicles for tid in v["trip_ids"]]
    assert sorted(seen) == sorted(by_id)  # each trip exactly once

    for v in vehicles:
        ids = v["trip_ids"]
        assert ids  # no empty vehicles
        for prev_id, next_id in zip(ids, ids[1:]):
            prev, nxt = by_id[prev_id], by_id[next_id]
            assert prev["stops"][-1]["station"] == nxt["stops"][0]["station"]
            assert (
                nxt["stops"][0]["departure"]
                >= prev["stops"][-1]["arrival"] + turnaround
            )

    # Vehicles sorted by first-trip departure, then first-trip id.
    keys = [
        (by_id[v["trip_ids"][0]]["stops"][0]["departure"], v["trip_ids"][0])
        for v in vehicles
    ]
    assert keys == sorted(keys)


# ---------------------------------------------------------------- happy path

def test_chain_and_single_trip_vehicle():
    r = post(payload())
    assert r.status_code == 200, r.text
    body = r.json()
    # T1 ends at B at 120, T2 leaves B at 130 >= 120 + 10: same vehicle.
    # T3 ends at D with nothing to continue to: its own vehicle.
    assert body == {
        "vehicle_count": 2,
        "vehicles": [{"trip_ids": ["T1", "T2"]}, {"trip_ids": ["T3"]}],
    }
    assert_valid_assignment(body, payload()["trips"], 10)


def test_single_trip_network():
    body = payload(trips=[trip("T1", ("A", 0, 60), ("B", 120, 120))])
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"vehicle_count": 1, "vehicles": [{"trip_ids": ["T1"]}]}


def test_turnaround_boundary_is_inclusive():
    # T2 departs B at 130, T1 arrives B at 120.
    assert post(payload(turnaround_minutes=10)).json()["vehicle_count"] == 2
    # One minute more breaks the chain: every trip needs its own vehicle.
    body = post(payload(turnaround_minutes=11)).json()
    assert body["vehicle_count"] == 3
    assert body["vehicles"] == [
        {"trip_ids": ["T1"]},
        {"trip_ids": ["T3"]},
        {"trip_ids": ["T2"]},
    ]


def test_zero_turnaround_allows_immediate_continuation():
    body = payload(
        trips=[
            trip("T1", ("A", 0, 0), ("B", 100, 100)),
            trip("T2", ("B", 100, 100), ("C", 200, 200)),
        ],
        turnaround_minutes=0,
    )
    assert post(body).json() == {
        "vehicle_count": 1,
        "vehicles": [{"trip_ids": ["T1", "T2"]}],
    }


def test_continuation_requires_same_station():
    # T2 departs from C, not from T1's last stop B: no chaining, ever.
    body = payload(
        trips=[
            trip("T1", ("A", 0, 0), ("B", 100, 100)),
            trip("T2", ("C", 500, 500), ("D", 600, 600)),
        ],
        turnaround_minutes=0,
    )
    assert post(body).json()["vehicle_count"] == 2


def test_loop_trip_can_chain_at_its_terminal_station():
    # T1 starts and ends at A; a vehicle may continue with T2 from A.
    body = payload(
        stations=["A", "B", "C"],
        trips=[
            trip("T1", ("A", 0, 0), ("B", 50, 50), ("A", 100, 100)),
            trip("T2", ("A", 110, 110), ("C", 200, 200)),
        ],
        turnaround_minutes=10,
    )
    assert post(body).json() == {
        "vehicle_count": 1,
        "vehicles": [{"trip_ids": ["T1", "T2"]}],
    }


# ------------------------------------------------------- global optimality

def test_global_minimum_requires_rematching():
    # Edges (turnaround 0): T1->T2, T1->T4, T3->T2, T3->T4.
    # A greedy first come first served chain could leave 3 vehicles; the
    # optimum re-matches so that 2 vehicles suffice.
    body = payload(
        stations=["A", "B", "C", "D", "E"],
        trips=[
            trip("T1", ("A", 0, 0), ("B", 100, 100)),
            trip("T2", ("B", 100, 100), ("C", 200, 200)),
            trip("T3", ("D", 10, 10), ("B", 100, 100)),
            trip("T4", ("B", 100, 100), ("E", 150, 150)),
        ],
        turnaround_minutes=0,
    )
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["vehicle_count"] == 2
    assert out["vehicles"] == [
        {"trip_ids": ["T1", "T4"]},
        {"trip_ids": ["T3", "T2"]},
    ]
    assert_valid_assignment(out, body["trips"], 0)


def test_every_vehicle_gets_a_chain_when_nothing_connects():
    body = payload(
        trips=[
            trip("T9", ("A", 0, 100), ("B", 150, 150)),
            trip("T1", ("A", 0, 100), ("C", 160, 160)),
            trip("T5", ("B", 0, 50), ("D", 90, 90)),
        ],
        turnaround_minutes=0,
    )
    out = post(body).json()
    # No continuation is possible; vehicles are ordered by first-trip
    # departure, then trip id — not by input order.
    assert out == {
        "vehicle_count": 3,
        "vehicles": [
            {"trip_ids": ["T5"]},
            {"trip_ids": ["T1"]},
            {"trip_ids": ["T9"]},
        ],
    }


# ------------------------------------------------------------ 422 validation

def test_turnaround_out_of_range_returns_422():
    assert post(payload(turnaround_minutes=-1)).status_code == 422
    assert post(payload(turnaround_minutes=61)).status_code == 422
    assert post(payload(turnaround_minutes="ten")).status_code == 422
    body = payload()
    del body["turnaround_minutes"]
    assert post(body).status_code == 422


def test_network_validation_is_reused():
    body = payload()
    body["trips"][0]["stops"].append(stop("ZZ", 300, 300))  # unknown station
    assert post(body).status_code == 422

    body = payload()
    body["trips"].append(trip("T1", ("A", 1, 2), ("B", 3, 3)))  # duplicate id
    assert post(body).status_code == 422

    body = payload()
    body["trips"][0]["stops"][1] = stop("B", 50, 50)  # time goes backwards
    assert post(body).status_code == 422

    assert post(payload(trips=[])).status_code == 422
    assert post(payload(stations=["ONLY"])).status_code == 422


def test_extra_field_returns_422():
    body = payload()
    body["nope"] = 1
    assert post(body).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/fleet/assign",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def test_hundred_trip_single_chain():
    stations = [f"S{i}" for i in range(40)]
    trips = []
    for k in range(100):
        a, b = stations[k % 40], stations[(k + 1) % 40]
        trips.append(
            trip(f"R{k:03d}", (a, k * 20, k * 20), (b, k * 20 + 10, k * 20 + 10))
        )
    body = {"stations": stations, "trips": trips, "turnaround_minutes": 5}
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    # Each trip ends where the next begins, 10 minutes before it departs:
    # one vehicle can run the whole schedule.
    assert out["vehicle_count"] == 1
    assert out["vehicles"][0]["trip_ids"] == [f"R{k:03d}" for k in range(100)]
    assert_valid_assignment(out, trips, 5)


def test_hundred_unconnectable_trips():
    stations = [f"S{i}" for i in range(40)]
    trips = []
    for k in range(100):
        # All trips run in parallel between disjoint station pairs.
        a, b = stations[(2 * k) % 40], stations[(2 * k + 1) % 40]
        trips.append(trip(f"R{k:03d}", (a, 0, 10), (b, 20, 20)))
    body = {"stations": stations, "trips": trips, "turnaround_minutes": 60}
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["vehicle_count"] == 100
    assert_valid_assignment(out, trips, 60)
