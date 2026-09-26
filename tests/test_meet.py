"""End-to-end tests for POST /v1/journey/meet."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def payload(**overrides):
    base = {
        "stations": ["A", "B", "C"],
        "trips": [
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("B", 0, 100), stop("C", 230, 230)]},
            {"id": "T3", "stops": [stop("A", 0, 150), stop("C", 225, 225)]},
        ],
        "travelers": [
            {"origin": "A", "earliest_departure": 0},
            {"origin": "B", "earliest_departure": 0},
        ],
        "transfer_buffer": 10,
        "max_wait": 30,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/journey/meet", json=body)


# ---------------------------------------------------------------- happy path

def test_later_route_is_considered_to_shorten_the_wait():
    # Traveler 1 can arrive at C at 200 (T1) or 225 (T3); traveler 2
    # arrives at 230 (T2).  Both pairings give meeting time 230, but the
    # later-arriving T3 shortens the wait from 30 to 5 and must win.
    r = post(payload())
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["meeting_station"] == "C"
    assert body["meeting_time"] == 230
    first, second = body["travelers"]
    assert first["arrival_time"] == 225
    assert first["transfers"] == 0
    assert first["trip_ids"] == ["T3"]
    assert first["segments"] == [
        {"trip_id": "T3", "from": "A", "to": "C", "departure": 150, "arrival": 225}
    ]
    assert second["arrival_time"] == 230
    assert second["transfers"] == 0
    assert second["trip_ids"] == ["T2"]
    assert second["segments"] == [
        {"trip_id": "T2", "from": "B", "to": "C", "departure": 100, "arrival": 230}
    ]


def test_max_wait_boundary_is_inclusive():
    # The best pairing arrives 225 / 230: a wait of exactly 5 minutes.
    assert post(payload(max_wait=5)).json()["reachable"] is True
    assert post(payload(max_wait=4)).json()["reachable"] is False


def test_max_wait_zero_requires_simultaneous_arrival():
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("B", 0, 80), stop("C", 200, 200)]},
        ],
        max_wait=0,
    )
    body = post(body).json()
    assert body["reachable"] is True
    assert body["meeting_time"] == 200
    assert [t["arrival_time"] for t in body["travelers"]] == [200, 200]
    # One minute apart: no plan with zero allowed wait.
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("B", 0, 80), stop("C", 201, 201)]},
        ],
        max_wait=0,
    )
    assert post(body).json()["reachable"] is False


def test_meet_with_transfer_and_buffer():
    body = payload(
        stations=["A", "B", "C", "D"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("D", 120, 125)]},
            {"id": "T2", "stops": [stop("D", 135, 135), stop("C", 200, 200)]},
            {"id": "T3", "stops": [stop("B", 0, 100), stop("C", 210, 210)]},
        ],
    )
    body = post(body).json()
    # Traveler 1: T1 A->D arriving 120, then T2 at 135 >= 120 + 10.
    assert body["meeting_station"] == "C"
    assert body["meeting_time"] == 210
    first, second = body["travelers"]
    assert first["transfers"] == 1
    assert first["trip_ids"] == ["T1", "T2"]
    assert first["segments"] == [
        {"trip_id": "T1", "from": "A", "to": "D", "departure": 60, "arrival": 120},
        {"trip_id": "T2", "from": "D", "to": "C", "departure": 135, "arrival": 200},
    ]
    assert second["trip_ids"] == ["T3"]


def test_transfer_buffer_enforced_for_travelers():
    # Alight at D at 120, buffer 16 -> need departure >= 136, T2 leaves 135.
    body = payload(
        stations=["A", "B", "C", "D"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("D", 120, 125)]},
            {"id": "T2", "stops": [stop("D", 135, 135), stop("C", 200, 200)]},
            {"id": "T3", "stops": [stop("B", 0, 100), stop("C", 210, 210)]},
        ],
        transfer_buffer=16,
    )
    assert post(body).json()["reachable"] is False
    # Buffer exactly 15 -> 135 >= 120 + 15: ok again.
    body["transfer_buffer"] = 15
    assert post(body).json()["reachable"] is True


def test_earliest_departure_gates_each_traveler():
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("B", 0, 100), stop("C", 210, 210)]},
        ],
        travelers=[
            {"origin": "A", "earliest_departure": 61},  # cannot board T1 at 60
            {"origin": "B", "earliest_departure": 0},
        ],
        max_wait=60,
    )
    assert post(body).json()["reachable"] is False
    body["travelers"][0]["earliest_departure"] = 60
    assert post(body).json()["reachable"] is True


def test_staying_on_same_trip_is_not_a_transfer():
    body = payload(
        stations=["A", "B", "C", "D"],
        trips=[
            {
                "id": "T1",
                "stops": [stop("A", 0, 10), stop("D", 50, 60), stop("C", 90, 90)],
            },
            {"id": "T2", "stops": [stop("B", 0, 20), stop("C", 95, 95)]},
        ],
    )
    body = post(body).json()
    assert body["meeting_time"] == 95
    first = body["travelers"][0]
    assert first["transfers"] == 0
    assert first["trip_ids"] == ["T1"]
    assert first["segments"] == [
        {"trip_id": "T1", "from": "A", "to": "C", "departure": 10, "arrival": 90}
    ]


def test_meeting_station_cannot_be_an_origin():
    # Traveler 1 can ride to B (traveler 2's origin) and traveler 2 can
    # ride to A (traveler 1's origin); the origins are the only shared
    # stations, so there is no valid meeting station.
    body = payload(
        stations=["A", "B"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("B", 120, 120)]},
            {"id": "T2", "stops": [stop("B", 0, 70), stop("A", 130, 130)]},
        ],
        max_wait=60,
    )
    assert post(body).json()["reachable"] is False


def test_meeting_at_an_origin_is_skipped_even_when_convenient():
    # Traveler 1 passes through B (traveler 2's origin) at 120; traveler 2
    # can ride the B->E->B loop and be back at B at 115 (wait 5, meeting
    # 120), which would beat the C meeting if B were allowed.  It is
    # forbidden, so they must meet at C instead.
    body = payload(
        stations=["A", "B", "C", "E"],
        trips=[
            {
                "id": "T1",
                "stops": [stop("A", 0, 60), stop("B", 120, 125), stop("C", 200, 200)],
            },
            {"id": "T2", "stops": [stop("B", 0, 130), stop("C", 210, 210)]},
            {"id": "T3", "stops": [stop("B", 0, 30), stop("E", 60, 65), stop("B", 115, 115)]},
        ],
    )
    body = post(body).json()
    assert body["meeting_station"] == "C"
    # Traveler 2 boards T1 at B (dep 125); both reach C at 200 with zero
    # transfers, which beats the T3+T1 loop arriving at the same time.
    assert body["meeting_time"] == 200
    assert [t["arrival_time"] for t in body["travelers"]] == [200, 200]
    assert body["travelers"][1]["trip_ids"] == ["T1"]


def test_unreachable_returns_nulls_and_empty_lists():
    body = post(payload(max_wait=4)).json()
    assert body == {
        "reachable": False,
        "meeting_station": None,
        "meeting_time": None,
        "travelers": [
            {"arrival_time": None, "transfers": 0, "trip_ids": [], "segments": []},
            {"arrival_time": None, "transfers": 0, "trip_ids": [], "segments": []},
        ],
    }


# ------------------------------------------------------------- tie-breaking

def test_earliest_meeting_time_beats_shorter_wait():
    body = payload(
        stations=["A", "B", "C", "D"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("B", 0, 60), stop("C", 205, 205)]},
            {"id": "T3", "stops": [stop("A", 0, 60), stop("D", 150, 150)]},
            {"id": "T4", "stops": [stop("B", 0, 60), stop("D", 190, 190)]},
        ],
        max_wait=60,
    )
    body = post(body).json()
    # At C: meeting 205 with wait 5; at D: meeting 190 with wait 40.
    # The earlier meeting time wins despite the longer wait.
    assert body["meeting_station"] == "D"
    assert body["meeting_time"] == 190


def test_fewer_total_transfers_wins_on_tied_meeting_and_wait():
    body = payload(
        stations=["A", "B", "C", "D", "E"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("A", 0, 60), stop("D", 200, 200)]},
            {"id": "T3", "stops": [stop("B", 0, 80), stop("D", 210, 210)]},
            {"id": "T4", "stops": [stop("B", 0, 80), stop("E", 150, 155)]},
            {"id": "T5", "stops": [stop("E", 165, 165), stop("C", 210, 210)]},
        ],
    )
    body = post(body).json()
    # At C: meeting 210, wait 10, but traveler 2 needs T4+T5 (1 transfer).
    # At D: meeting 210, wait 10, zero transfers in total.  D wins even
    # though "C" < "D".
    assert body["meeting_station"] == "D"
    assert body["meeting_time"] == 210
    assert sum(t["transfers"] for t in body["travelers"]) == 0


def test_station_id_breaks_ties():
    body = payload(
        stations=["A", "B", "C", "D"],
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "T2", "stops": [stop("A", 0, 60), stop("D", 200, 200)]},
            {"id": "T3", "stops": [stop("B", 0, 80), stop("C", 210, 210)]},
            {"id": "T4", "stops": [stop("B", 0, 80), stop("D", 210, 210)]},
        ],
    )
    body = post(body).json()
    # Identical meeting time, wait and transfers at C and D: the
    # lexicographically smaller station id wins.
    assert body["meeting_station"] == "C"
    assert body["meeting_time"] == 210


def test_first_traveler_trip_id_sequence_breaks_ties():
    body = payload(
        trips=[
            {"id": "B2", "stops": [stop("A", 0, 50), stop("C", 200, 200)]},
            {"id": "A1", "stops": [stop("A", 0, 70), stop("C", 200, 200)]},
            {"id": "T9", "stops": [stop("B", 0, 80), stop("C", 210, 210)]},
        ],
    )
    body = post(body).json()
    # Both A1 and B2 bring traveler 1 to C at 200 with 0 transfers; the
    # lexicographically smaller trip id wins even though B2 departs earlier.
    first = body["travelers"][0]
    assert first["trip_ids"] == ["A1"]
    assert first["segments"][0]["departure"] == 70


def test_second_traveler_trip_id_sequence_breaks_ties():
    body = payload(
        trips=[
            {"id": "T1", "stops": [stop("A", 0, 60), stop("C", 200, 200)]},
            {"id": "B2", "stops": [stop("B", 0, 50), stop("C", 210, 210)]},
            {"id": "A1", "stops": [stop("B", 0, 70), stop("C", 210, 210)]},
        ],
    )
    body = post(body).json()
    assert body["travelers"][1]["trip_ids"] == ["A1"]


# ------------------------------------------------------------ 422 validation

@pytest.mark.parametrize(
    "mutate",
    [
        # traveler list errors
        lambda b: b.update(travelers=[{"origin": "A", "earliest_departure": 0}]),
        lambda b: b["travelers"].append({"origin": "C", "earliest_departure": 0}),
        lambda b: b.update(travelers=[]),
        lambda b: b.pop("travelers"),
        # origin errors
        lambda b: b["travelers"][1].update(origin="A"),  # same as traveler 1
        lambda b: b["travelers"][0].update(origin="ZZ"),
        lambda b: b["travelers"][1].update(origin=""),
        # earliest_departure errors
        lambda b: b["travelers"][0].update(earliest_departure=-1),
        lambda b: b["travelers"][1].update(earliest_departure=2880),
        lambda b: b["travelers"][0].pop("earliest_departure"),
        # max_wait / transfer_buffer errors
        lambda b: b.update(max_wait=-1),
        lambda b: b.update(max_wait=61),
        lambda b: b.pop("max_wait"),
        lambda b: b.update(transfer_buffer=0),
        lambda b: b.update(transfer_buffer=61),
        # extra fields
        lambda b: b["travelers"][0].update(name="x"),
        lambda b: b.update(wait=10),
        # network errors (shared with /v1/journey)
        lambda b: b.update(stations=["A", "A", "B"]),
        lambda b: b["trips"].append(
            {"id": "T1", "stops": [stop("A", 1, 2), stop("B", 3, 3)]}
        ),
        lambda b: b["trips"][0]["stops"].append(stop("ZZ", 300, 300)),
    ],
    ids=[
        "one-traveler",
        "three-travelers",
        "no-travelers",
        "travelers-missing",
        "same-origins",
        "unknown-origin",
        "empty-origin",
        "negative-earliest-departure",
        "earliest-departure-too-late",
        "earliest-departure-missing",
        "negative-max-wait",
        "max-wait-too-large",
        "max-wait-missing",
        "buffer-too-small",
        "buffer-too-large",
        "traveler-extra-field",
        "request-extra-field",
        "duplicate-stations",
        "duplicate-trip-ids",
        "trip-references-unknown-station",
    ],
)
def test_validation_errors_return_422(mutate):
    body = payload()
    mutate(body)
    r = post(body)
    assert r.status_code == 422, r.text


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/journey/meet",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ robustness

def test_many_trips_stress():
    # 100 trips chained through 40 stations; both travelers ride the same
    # chain, one starting a station behind the other.
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
        travelers=[
            {"origin": "S0", "earliest_departure": 0},
            {"origin": "S1", "earliest_departure": 0},
        ],
        transfer_buffer=5,
        max_wait=60,
    )
    r = post(body)
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    # S1 is traveler 2's origin; the first valid meeting station is S2,
    # where TR001 delivers both travelers at minute 35.
    assert body["meeting_station"] == "S2"
    assert body["meeting_time"] == 35
    first, second = body["travelers"]
    assert first["trip_ids"] == ["TR000", "TR001"]
    assert first["transfers"] == 1
    assert second["trip_ids"] == ["TR001"]
