"""End-to-end tests for POST /v1/journey."""

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
        "transfer_buffer": 10,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/journey", json=body)


# ---------------------------------------------------------------- happy path

def test_direct_journey_no_transfer():
    r = post(payload(destination="C"))
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["arrival_time"] == 200
    assert body["transfers"] == 0
    assert body["trip_ids"] == ["T1"]
    assert body["segments"] == [
        {"trip_id": "T1", "from": "A", "to": "C", "departure": 60, "arrival": 200}
    ]


def test_journey_with_one_transfer():
    r = post(payload())
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["arrival_time"] == 300
    assert body["transfers"] == 1
    assert body["trip_ids"] == ["T1", "T2"]
    assert body["segments"] == [
        {"trip_id": "T1", "from": "A", "to": "C", "departure": 60, "arrival": 200},
        {"trip_id": "T2", "from": "C", "to": "D", "departure": 215, "arrival": 300},
    ]


def test_board_and_alight_at_intermediate_stops():
    # Board T1 at B (an intermediate stop) instead of A.
    r = post(payload(origin="B", destination="C", earliest_departure=100))
    body = r.json()
    assert body["arrival_time"] == 200
    assert body["segments"][0]["from"] == "B"
    assert body["segments"][0]["departure"] == 125


def test_earliest_departure_gates_first_boarding():
    # T1 leaves A at 60; earliest_departure=61 makes it impossible to board.
    r = post(payload(destination="C", earliest_departure=61))
    assert r.json()["reachable"] is False
    # At exactly 60 it is allowed.
    r = post(payload(destination="C", earliest_departure=60))
    assert r.json()["reachable"] is True


def test_transfer_buffer_enforced():
    # Alight at C at 200, buffer 10 -> need departure >= 210. T2 leaves at 215: ok.
    assert post(payload()).json()["reachable"] is True
    # Buffer 16 -> need >= 216, T2 leaves 215: no route.
    r = post(payload(transfer_buffer=16))
    assert r.json()["reachable"] is False
    # Buffer exactly 15 -> need >= 215: ok again.
    r = post(payload(transfer_buffer=15))
    assert r.json()["reachable"] is True


def test_staying_on_same_trip_is_not_a_transfer():
    # T3 goes A -> B -> C; riding through B is one segment, zero transfers.
    body = payload(
        trips=[
            {
                "id": "T3",
                "stops": [stop("A", 0, 10), stop("B", 50, 60), stop("C", 90, 90)],
            }
        ],
        destination="C",
    )
    r = post(body)
    body = r.json()
    assert body["transfers"] == 0
    assert body["trip_ids"] == ["T3"]
    assert body["segments"] == [
        {"trip_id": "T3", "from": "A", "to": "C", "departure": 10, "arrival": 90}
    ]


def test_unreachable_returns_empty_itinerary():
    r = post(payload(origin="D", destination="A"))
    assert r.status_code == 200
    assert r.json() == {
        "reachable": False,
        "arrival_time": None,
        "transfers": 0,
        "trip_ids": [],
        "segments": [],
    }


# ------------------------------------------------------------- tie-breaking

def test_tie_same_arrival_prefers_fewer_transfers():
    # Direct T9 arrives 500 with 0 transfers; T1+T2 combo also arrives 500
    # with 1 transfer. Direct must win.
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 100), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("C", 210, 210), stop("D", 500, 500)]},
            {"id": "T9", "stops": [stop("A", 0, 300), stop("D", 500, 500)]},
        ],
    )
    r = post(body)
    body = r.json()
    assert body["arrival_time"] == 500
    assert body["transfers"] == 0
    assert body["trip_ids"] == ["T9"]


def test_tie_same_arrival_same_transfers_prefers_lexicographic_trip_ids():
    # Two one-transfer options arriving at 500: ("B2","T2") vs ("A1","T2").
    # ("A1", "T2") < ("B2", "T2") lexicographically, so A1 must be chosen
    # even though B2 departs earlier.
    body = payload(
        trips=[
            {"id": "B2", "stops": [stop("A", 0, 100), stop("C", 150, 150)]},
            {"id": "A1", "stops": [stop("A", 0, 180), stop("C", 230, 230)]},
            {"id": "T2", "stops": [stop("C", 240, 240), stop("D", 500, 500)]},
        ],
    )
    r = post(body)
    body = r.json()
    assert body["arrival_time"] == 500
    assert body["transfers"] == 1
    assert body["trip_ids"] == ["A1", "T2"]


def test_earliest_arrival_beats_fewer_transfers():
    # One-transfer route arrives 400, direct route arrives 450: earlier wins.
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 100), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("C", 210, 210), stop("D", 400, 400)]},
            {"id": "T9", "stops": [stop("A", 0, 300), stop("D", 450, 450)]},
        ],
    )
    r = post(body)
    body = r.json()
    assert body["arrival_time"] == 400
    assert body["transfers"] == 1


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
        # structure errors
        lambda b: b.update(origin="A", destination="A"),
        lambda b: b.update(earliest_departure=-1),
        lambda b: b.update(earliest_departure=2880),
        lambda b: b.update(transfer_buffer=0),
        lambda b: b.update(transfer_buffer=61),
        lambda b: b.update(stations=["ONLY"]),
        lambda b: b.update(trips=[]),
        lambda b: b["trips"].append({"id": "T1", "stops": [stop("A", 1, 2), stop("B", 3, 3)]}),
        lambda b: b["trips"].__setitem__(0, {"id": "X", "stops": [stop("A", 1, 2)]}),
    ],
    ids=[
        "unknown-origin",
        "unknown-destination",
        "trip-references-unknown-station",
        "duplicate-stations",
        "arrival-after-departure",
        "next-arrival-not-after-prev-departure",
        "minute-out-of-range",
        "same-origin-destination",
        "negative-earliest-departure",
        "earliest-departure-too-late",
        "buffer-too-small",
        "buffer-too-large",
        "too-few-stations",
        "no-trips",
        "duplicate-trip-ids",
        "trip-with-single-stop",
    ],
)
def test_validation_errors_return_422(mutate):
    body = payload()
    mutate(body)
    r = post(body)
    assert r.status_code == 422, r.text


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/journey", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ robustness

def test_many_trips_stress():
    # 100 trips chained through 41... keep to 40 stations: chain A0..A39.
    stations = [f"S{i}" for i in range(40)]
    trips = []
    for i in range(100):
        a, b = stations[i % 39], stations[i % 39 + 1]
        start = i * 20
        trips.append(
            {
                "id": f"TR{i:03d}",
                "stops": [stop(a, start, start + 5), stop(b, start + 15, start + 15)],
            }
        )
    body = payload(
        stations=stations,
        trips=trips,
        origin="S0",
        destination="S39",
        earliest_departure=0,
        transfer_buffer=5,
    )
    r = post(body)
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    # Each trip covers one hop; 39 hops needed.
    assert body["transfers"] == 38
    assert len(body["segments"]) == 39
