"""End-to-end tests for POST /v1/fares/quote."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def network():
    # T1: A 10 -> B 20 -> C 30 ; T2: C 40 -> D 50 -> E 60 ; T3: E 70 -> A 80
    return {
        "stations": ["A", "B", "C", "D", "E"],
        "trips": [
            {
                "id": "T1",
                "stops": [stop("A", 0, 10), stop("B", 20, 20), stop("C", 30, 30)],
            },
            {
                "id": "T2",
                "stops": [stop("C", 40, 40), stop("D", 50, 50), stop("E", 60, 60)],
            },
            {
                "id": "T3",
                "stops": [stop("E", 70, 70), stop("A", 80, 80)],
            },
        ],
    }


def rides3():
    # Three rides, each on a different trip, all meeting buffer 10.
    return [
        {"trip_id": "T1", "board_index": 0, "alight_index": 2},  # dep 10
        {"trip_id": "T2", "board_index": 0, "alight_index": 2},  # dep 40
        {"trip_id": "T3", "board_index": 0, "alight_index": 1},  # dep 70
    ]


def ticket(tid, price, valid, rides):
    return {"id": tid, "price_cents": price, "valid_minutes": valid, "max_rides": rides}


def payload(**overrides):
    base = {
        **network(),
        "rides": rides3(),
        "transfer_buffer": 10,
        "tickets": [
            ticket("SINGLE", 100, 60, 1),
            ticket("DAY", 250, 2879, 20),
        ],
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/fares/quote", json=body)


# ---------------------------------------------------------------- happy path

def test_single_ticket_covering_all_is_cheapest():
    r = post(payload())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "total_cents": 250,
        "tickets": [{"ticket_id": "DAY", "covers_from": 0, "covers_to": 2}],
    }


def test_individual_tickets_when_cover_is_pricier():
    r = post(payload(tickets=[ticket("SINGLE", 100, 60, 1), ticket("DAY", 400, 2879, 20)]))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "total_cents": 300,
        "tickets": [
            {"ticket_id": "SINGLE", "covers_from": 0, "covers_to": 0},
            {"ticket_id": "SINGLE", "covers_from": 1, "covers_to": 1},
            {"ticket_id": "SINGLE", "covers_from": 2, "covers_to": 2},
        ],
    }


def test_mixed_partition():
    # SINGLE 100; DAY (20 rides, all day) 220; HOUR (2 rides, 60 min) 180.
    # DAY covers all for 220 -> best (vs HOUR+SINGLE 280).
    r = post(
        payload(
            tickets=[
                ticket("SINGLE", 100, 60, 1),
                ticket("HOUR", 180, 60, 2),
                ticket("DAY", 220, 2879, 20),
            ]
        )
    )
    assert r.json()["total_cents"] == 220

    # DAY at 300: HOUR(0-1)=180 + SINGLE(2)=100 = 280; ride 1 dep 40 and
    # ride 2 dep 70 are 30 min apart so HOUR(1-2)+SINGLE(0) is also 280.
    # SINGLE*3 = 300; the two-ticket solutions tie and ("HOUR","SINGLE")
    # sorts lexicographically before ("SINGLE","HOUR").
    r = post(
        payload(
            tickets=[
                ticket("SINGLE", 100, 60, 1),
                ticket("HOUR", 180, 60, 2),
                ticket("DAY", 300, 2879, 20),
            ]
        )
    )
    body = r.json()
    assert body["total_cents"] == 280
    assert [(e["ticket_id"], e["covers_from"], e["covers_to"]) for e in body["tickets"]] == [
        ("HOUR", 0, 1),
        ("SINGLE", 2, 2),
    ]


def test_valid_minutes_boundary():
    # departures 10 and 40: span 30. valid 29 -> block [0,1] invalid;
    # valid 30 (boundary inclusive: dep <= purchase + valid) -> valid.
    tickets = [ticket("SINGLE", 100, 0, 1), ticket("PAIR", 150, 29, 20)]
    r = post(payload(tickets=tickets))
    assert r.json()["total_cents"] == 300  # three singles

    tickets[1]["valid_minutes"] = 30
    r = post(payload(tickets=tickets))
    assert r.json()["total_cents"] == 250  # PAIR[0,1] + SINGLE[2]

    # valid 60 covers span 60 between rides 0 and 2 (10 -> 70).
    tickets[1]["valid_minutes"] = 60
    r = post(payload(tickets=tickets))
    assert r.json() == {
        "total_cents": 150,
        "tickets": [{"ticket_id": "PAIR", "covers_from": 0, "covers_to": 2}],
    }


def test_max_rides_limit():
    # PAIR (max 2 rides, all day) + SINGLE: never all three on one ticket.
    r = post(payload(tickets=[ticket("SINGLE", 100, 0, 1), ticket("PAIR", 120, 2879, 2)]))
    body = r.json()
    assert body["total_cents"] == 220  # PAIR + SINGLE
    assert len(body["tickets"]) == 2


def test_ticket_type_can_be_reused():
    # Only one ticket type that allows 1 ride: it is bought for every ride.
    r = post(payload(tickets=[ticket("ONLY", 0, 2879, 1)]))
    assert r.json() == {
        "total_cents": 0,
        "tickets": [
            {"ticket_id": "ONLY", "covers_from": 0, "covers_to": 0},
            {"ticket_id": "ONLY", "covers_from": 1, "covers_to": 1},
            {"ticket_id": "ONLY", "covers_from": 2, "covers_to": 2},
        ],
    }


def test_zero_priced_ticket():
    r = post(payload(tickets=[ticket("FREE", 0, 0, 1)]))
    assert r.json()["total_cents"] == 0


# ------------------------------------------------------------- tie-breaking

def test_tie_prefers_fewer_tickets():
    # SINGLE 100 each -> 300/3 tickets; TRIPLE 300/1 ticket -> same price.
    r = post(payload(tickets=[ticket("SINGLE", 100, 0, 1), ticket("TRIPLE", 300, 2879, 20)]))
    body = r.json()
    assert body["total_cents"] == 300
    assert body["tickets"] == [{"ticket_id": "TRIPLE", "covers_from": 0, "covers_to": 2}]


def test_tie_same_count_prefers_lexicographic_id_sequence():
    # With 1-ride tickets only, every solution is 3 tickets for 300 cents:
    # sequences ("A",)*3 vs ("B",)*3 -> "A" wins.
    r = post(payload(tickets=[ticket("B", 100, 0, 1), ticket("A", 100, 0, 1)]))
    body = r.json()
    assert body["total_cents"] == 300
    assert [e["ticket_id"] for e in body["tickets"]] == ["A", "A", "A"]


def test_tie_lexicographic_at_first_diverging_position():
    # Two-ride partitions tie at 2 tickets / 250 cents:
    #   ("PAIR","SINGLE") = PAIR on rides [0,1] + SINGLE on ride 2
    #   ("SINGLE","PAIR") = SINGLE on ride 0 + PAIR on rides [1,2]
    # PAIR covers a 30-minute span and the second pair is preferred by price
    # only equally; lexicographic order on the ride-order id sequence picks
    # ("PAIR","SINGLE") — proving ids are compared in ride order, not sorted.
    r = post(
        payload(
            tickets=[
                ticket("SINGLE", 100, 0, 1),
                ticket("PAIR", 150, 30, 2),
            ]
        )
    )
    entries = r.json()["tickets"]
    assert [e["ticket_id"] for e in entries] == ["PAIR", "SINGLE"]
    assert [(e["covers_from"], e["covers_to"]) for e in entries] == [
        (0, 1),
        (2, 2),
    ]


# ------------------------------------------------------------ 422 validation

def test_indexes_not_increasing_returns_422():
    body = payload()
    body["rides"][0]["alight_index"] = 0
    assert post(body).status_code == 422


def test_board_index_at_last_stop_returns_422():
    body = payload()
    body["rides"][0]["board_index"] = 2  # last stop of T1, nothing to ride
    assert post(body).status_code == 422


def test_alight_index_out_of_range_returns_422():
    body = payload()
    body["rides"][0]["alight_index"] = 3
    assert post(body).status_code == 422


def test_unknown_trip_id_returns_422():
    body = payload()
    body["rides"][0]["trip_id"] = "T9"
    assert post(body).status_code == 422


def test_same_adjacent_trip_returns_422():
    body = payload()
    # Valid indices on T1 (A->B), but ride 0 also used T1: must change trips.
    body["rides"][1] = {"trip_id": "T1", "board_index": 0, "alight_index": 1}
    assert post(body).status_code == 422


def test_alight_not_equal_next_board_station_returns_422():
    body = payload()
    # Ride 1 boards T2 at D (index 1) instead of C; ride 0 alights at C.
    body["rides"][1]["board_index"] = 1
    assert post(body).status_code == 422


def test_transfer_buffer_violation_returns_422():
    # Ride 0 arrives C at 30, ride 1 departs C at 40: buffer 11 invalid, 10 ok.
    assert post(payload(transfer_buffer=10)).status_code == 200
    assert post(payload(transfer_buffer=11)).status_code == 422


def test_duplicate_ticket_ids_return_422():
    body = payload()
    body["tickets"].append(ticket("SINGLE", 50, 10, 1))
    assert post(body).status_code == 422


def test_ticket_field_bounds_return_422():
    bad_tickets = [
        {**ticket("S", 100, 60, 1), "price_cents": -1},
        {**ticket("S", 100, 60, 1), "valid_minutes": 2880},
        {**ticket("S", 100, 60, 1), "valid_minutes": -1},
        {**ticket("S", 100, 60, 1), "max_rides": 0},
        {**ticket("S", 100, 60, 1), "max_rides": 21},
        {**ticket("S", 100, 60, 1), "id": ""},
    ]
    for bad in bad_tickets:
        body = payload(tickets=[bad])
        assert post(body).status_code == 422, bad


def test_collection_size_bounds_return_422():
    assert post(payload(rides=[])).status_code == 422
    assert post(payload(tickets=[])).status_code == 422
    assert post(payload(transfer_buffer=0)).status_code == 422
    assert post(payload(transfer_buffer=61)).status_code == 422

    body = payload()
    body["rides"] += body["rides"] * 7  # 24 rides
    assert post(body).status_code == 422

    body = payload()
    body["tickets"] = [ticket(f"T{i}", 10, 60, 1) for i in range(13)]
    assert post(body).status_code == 422


def test_unknown_station_in_trip_still_422():
    body = payload()
    body["trips"][0]["stops"].append(stop("ZZ", 90, 90))
    assert post(body).status_code == 422


def test_extra_field_returns_422():
    body = payload()
    body["rides"][0]["nope"] = 1
    assert post(body).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/fares/quote", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def test_maximum_size_request():
    stations = [f"S{i}" for i in range(40)]
    trips = []
    # 20 single-leg rides chained across two-stop trips, 10 minutes apart.
    rides = []
    for k in range(20):
        a, b = f"S{k % 39}", f"S{(k + 1) % 39}"
        dep = 10 * k + 10
        trips.append(
            {
                "id": f"R{k}",
                "stops": [stop(a, dep - 1, dep), stop(b, dep + 5, dep + 5)],
            }
        )
        rides.append({"trip_id": f"R{k}", "board_index": 0, "alight_index": 1})
    tickets = [
        ticket("S", 10, 0, 1),
        ticket("W2", 15, 9, 2),
        ticket("W5", 60, 45, 5),
        ticket("DAY", 500, 2879, 20),
    ] + [ticket(f"X{i:02d}", 100 + i, 100, 3) for i in range(8)]
    body = {
        "stations": stations,
        "trips": trips,
        "rides": rides,
        "transfer_buffer": 5,
        "tickets": tickets,
    }
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    # Every adjacent pair is exactly 10 minutes apart and within 9-minute
    # window? span is 10 -> W2 invalid; singles at 10 each -> 200 total.
    assert out["total_cents"] == 200
    # Coverage must be one ticket per ride, contiguous ranges, no gaps.
    ranges = [(e["covers_from"], e["covers_to"]) for e in out["tickets"]]
    cur = 0
    for lo, hi in ranges:
        assert lo == cur
        assert hi >= lo
        cur = hi + 1
    assert cur == 20
