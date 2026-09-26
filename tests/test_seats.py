"""End-to-end tests for POST /v1/seats/allocate."""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def trip(tid, *stops):
    return {"id": tid, "stops": [stop(*s) for s in stops]}


def ride(trip_id, board, alight):
    return {"trip_id": trip_id, "board_index": board, "alight_index": alight}


def booking(bid, passengers, *rides):
    return {"id": bid, "passengers": passengers, "rides": list(rides)}


def network():
    # T1: A -> B -> C (intervals A-B, B-C); T2: C -> D; T3: A -> B.
    return {
        "stations": ["A", "B", "C", "D"],
        "trips": [
            trip("T1", ("A", 0, 60), ("B", 120, 125), ("C", 200, 200)),
            trip("T2", ("C", 210, 215), ("D", 300, 300)),
            trip("T3", ("A", 0, 100), ("B", 160, 160)),
        ],
    }


def payload(**overrides):
    base = {
        **network(),
        "capacity": {"T1": 5, "T2": 2, "T3": 5},
        "transfer_buffer": 10,
        "bookings": [
            booking("B1", 4, ride("T1", 0, 2)),
            booking("B2", 3, ride("T1", 0, 1)),
            booking("B3", 5, ride("T3", 0, 1)),
        ],
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/seats/allocate", json=body)


# ---------------------------------------------------------------- happy path

def test_accepts_the_maximum_passenger_set():
    # T1 capacity 5: B1 (4, both intervals) and B2 (3, interval A-B) collide
    # on A-B (4 + 3 > 5); B3 rides T3 and always fits.
    r = post(payload())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "accepted_ids": ["B1", "B3"],
        "rejected_ids": ["B2"],
        "accepted_passengers": 9,
    }


def test_everything_fits_accepts_all_bookings():
    body = payload(capacity={"T1": 10, "T2": 2, "T3": 5})
    assert post(body).json() == {
        "accepted_ids": ["B1", "B2", "B3"],
        "rejected_ids": [],
        "accepted_passengers": 12,
    }


def test_nothing_fits_rejects_everything():
    body = payload(capacity={"T1": 1, "T2": 1, "T3": 1})
    assert post(body).json() == {
        "accepted_ids": [],
        "rejected_ids": ["B1", "B2", "B3"],
        "accepted_passengers": 0,
    }


def test_response_id_lists_are_sorted_ascending():
    body = payload(
        bookings=[
            booking("G9", 1, ride("T3", 0, 1)),
            booking("G1", 1, ride("T3", 0, 1)),
            booking("G5", 1, ride("T3", 0, 1)),
        ]
    )
    out = post(body).json()
    assert out["accepted_ids"] == ["G1", "G5", "G9"]
    assert out["accepted_passengers"] == 3


# ------------------------------------------------------------------ tie-break

def test_tie_broken_by_lexicographically_smallest_accepted_id_list():
    # B1 and B2 each fill T1 alone; both optima tie at 3 passengers and the
    # answer must be ["B1"], independent of the input order.
    body = payload(
        capacity={"T1": 3, "T2": 2, "T3": 5},
        bookings=[
            booking("B2", 3, ride("T1", 0, 1)),
            booking("B1", 3, ride("T1", 0, 1)),
        ],
    )
    assert post(body).json() == {
        "accepted_ids": ["B1"],
        "rejected_ids": ["B2"],
        "accepted_passengers": 3,
    }


def test_tie_break_compares_sorted_id_lists_elementwise():
    # {A} and {AB, AC} both give 2 passengers; ["A"] < ["AB", "AC"].
    body = payload(
        capacity={"T1": 2, "T2": 2, "T3": 2},
        bookings=[
            booking("AB", 1, ride("T1", 0, 1)),
            booking("AC", 1, ride("T1", 0, 1)),
            booking("A", 2, ride("T1", 0, 1)),
        ],
    )
    assert post(body).json() == {
        "accepted_ids": ["A"],
        "rejected_ids": ["AB", "AC"],
        "accepted_passengers": 2,
    }


# ------------------------------------------------------- interval accounting

def test_non_overlapping_intervals_share_the_trip():
    # B1 rides A-B, B2 rides B-C: different intervals of T1, both fit.
    body = payload(
        capacity={"T1": 4, "T2": 2, "T3": 5},
        bookings=[
            booking("B1", 4, ride("T1", 0, 1)),
            booking("B2", 4, ride("T1", 1, 2)),
        ],
    )
    assert post(body).json() == {
        "accepted_ids": ["B1", "B2"],
        "rejected_ids": [],
        "accepted_passengers": 8,
    }


def test_long_ride_occupies_every_interval_it_spans():
    # B3 rides A->C and therefore collides with both B1 (A-B) and B2 (B-C).
    body = payload(
        capacity={"T1": 5, "T2": 2, "T3": 5},
        bookings=[
            booking("B1", 2, ride("T1", 0, 1)),
            booking("B2", 2, ride("T1", 1, 2)),
            booking("B3", 5, ride("T1", 0, 2)),
        ],
    )
    # {B1, B2} = 4 passengers < {B3} = 5 passengers.
    assert post(body).json() == {
        "accepted_ids": ["B3"],
        "rejected_ids": ["B1", "B2"],
        "accepted_passengers": 5,
    }


def test_multi_ride_booking_occupies_every_leg():
    # B1 chains T1 (A->C, arrive 200) with T2 (C->D, depart 215 >= 200 + 10)
    # and fills T2 completely, so B2 is rejected.
    body = payload(
        bookings=[
            booking("B1", 2, ride("T1", 0, 2), ride("T2", 0, 1)),
            booking("B2", 1, ride("T2", 0, 1)),
        ]
    )
    assert post(body).json() == {
        "accepted_ids": ["B1"],
        "rejected_ids": ["B2"],
        "accepted_passengers": 2,
    }


def test_multi_ride_booking_is_rejected_as_a_whole():
    # T2 only has 2 seats: the 3-passenger chain cannot be accepted at all,
    # even though its T1 leg alone would fit.
    body = payload(
        bookings=[
            booking("B1", 3, ride("T1", 0, 2), ride("T2", 0, 1)),
            booking("B2", 2, ride("T2", 0, 1)),
        ]
    )
    assert post(body).json() == {
        "accepted_ids": ["B2"],
        "rejected_ids": ["B1"],
        "accepted_passengers": 2,
    }


def test_same_trip_can_be_reboarded_in_a_later_ride():
    # T1: A -> B -> C -> D; T2: B -> C.  B1 rides T1 A->B, T2 B->C, then
    # T1 again C->D (depart 205 >= 150 + 10): valid, and it occupies only
    # the A-B and C-D intervals of T1, leaving B-C free for B2.
    body = {
        "stations": ["A", "B", "C", "D"],
        "trips": [
            trip("T1", ("A", 0, 60), ("B", 120, 125), ("C", 200, 205), ("D", 260, 260)),
            trip("T2", ("B", 130, 135), ("C", 150, 150)),
        ],
        "capacity": {"T1": 3, "T2": 3},
        "transfer_buffer": 10,
        "bookings": [
            booking("B1", 3, ride("T1", 0, 1), ride("T2", 0, 1), ride("T1", 2, 3)),
            booking("B2", 3, ride("T1", 1, 2)),
        ],
    }
    assert post(body).json() == {
        "accepted_ids": ["B1", "B2"],
        "rejected_ids": [],
        "accepted_passengers": 6,
    }


# ------------------------------------------------------------ 422 validation

def test_capacity_must_cover_every_trip_exactly():
    body = payload()
    del body["capacity"]["T2"]  # missing trip
    assert post(body).status_code == 422

    body = payload()
    body["capacity"]["T9"] = 3  # unknown trip
    assert post(body).status_code == 422

    body = payload()
    del body["capacity"]
    assert post(body).status_code == 422


def test_capacity_values_must_be_positive_integers():
    assert post(payload(capacity={"T1": 5, "T2": 2, "T3": 0})).status_code == 422
    assert post(payload(capacity={"T1": 5, "T2": 2, "T3": -1})).status_code == 422
    assert post(payload(capacity={"T1": 5, "T2": 2, "T3": "x"})).status_code == 422


def test_booking_ids_must_be_unique():
    body = payload(
        bookings=[
            booking("B1", 1, ride("T1", 0, 1)),
            booking("B1", 1, ride("T3", 0, 1)),
        ]
    )
    assert post(body).status_code == 422


def test_passengers_must_be_positive():
    assert post(payload(bookings=[booking("B1", 0, ride("T1", 0, 1))])).status_code == 422
    assert post(payload(bookings=[booking("B1", -2, ride("T1", 0, 1))])).status_code == 422


def test_bookings_count_bounds():
    assert post(payload(bookings=[])).status_code == 422
    many = [booking(f"G{k:02d}", 1, ride("T3", 0, 1)) for k in range(19)]
    assert post(payload(bookings=many)).status_code == 422


def test_ride_index_validation_returns_422():
    # Unknown trip.
    assert post(payload(bookings=[booking("B1", 1, ride("T9", 0, 1))])).status_code == 422
    # board_index not strictly below alight_index.
    assert post(payload(bookings=[booking("B1", 1, ride("T1", 1, 1))])).status_code == 422
    assert post(payload(bookings=[booking("B1", 1, ride("T1", 2, 1))])).status_code == 422
    # alight_index beyond the last stop (T1 has stops 0..2).
    assert post(payload(bookings=[booking("B1", 1, ride("T1", 0, 3))])).status_code == 422
    # board at the last stop.
    assert post(payload(bookings=[booking("B1", 1, ride("T1", 2, 3))])).status_code == 422


def test_adjacent_ride_rules_return_422():
    # Adjacent rides on the same trip.
    body = payload(bookings=[booking("B1", 1, ride("T1", 0, 1), ride("T1", 1, 2))])
    assert post(body).status_code == 422
    # Alighting station (C) differs from the next boarding station (A).
    body = payload(bookings=[booking("B1", 1, ride("T1", 0, 2), ride("T3", 0, 1))])
    assert post(body).status_code == 422
    # Buffer too short: T1 arrives C at 200, T2 departs C at 215 < 200 + 16.
    body = payload(
        transfer_buffer=16,
        bookings=[booking("B1", 1, ride("T1", 0, 2), ride("T2", 0, 1))],
    )
    assert post(body).status_code == 422


def test_transfer_buffer_boundary_is_inclusive():
    # 215 == 200 + 15 exactly: the chain is valid.
    body = payload(
        transfer_buffer=15,
        bookings=[booking("B1", 2, ride("T1", 0, 2), ride("T2", 0, 1))],
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json()["accepted_ids"] == ["B1"]


def test_ride_count_bounds():
    assert post(payload(bookings=[booking("B1", 1)])).status_code == 422
    body = payload(
        bookings=[
            booking(
                "B1", 1,
                ride("T1", 0, 1), ride("T3", 0, 1), ride("T1", 0, 1), ride("T3", 0, 1),
            )
        ]
    )
    assert post(body).status_code == 422


def test_transfer_buffer_out_of_range_returns_422():
    assert post(payload(transfer_buffer=0)).status_code == 422
    assert post(payload(transfer_buffer=61)).status_code == 422
    body = payload()
    del body["transfer_buffer"]
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


def test_extra_fields_return_422():
    body = payload()
    body["nope"] = 1
    assert post(body).status_code == 422

    body = payload()
    body["bookings"][0]["nope"] = 1
    assert post(body).status_code == 422

    body = payload()
    body["bookings"][0]["rides"][0]["nope"] = 1
    assert post(body).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/seats/allocate",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def test_eighteen_bookings_all_accepted_when_capacity_allows():
    bookings = [booking(f"G{k:02d}", 3, ride("T1", 0, 1)) for k in range(18)]
    body = payload(bookings=bookings, capacity={"T1": 54, "T2": 2, "T3": 5})
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "accepted_ids": [f"G{k:02d}" for k in range(18)],
        "rejected_ids": [],
        "accepted_passengers": 54,
    }


def test_eighteen_bookings_heavy_tie_picks_first_ids():
    # Capacity 18 with 2 passengers per booking: any 9 bookings tie at 18
    # passengers; the lexicographically smallest list takes the first ids.
    bookings = [booking(f"G{k:02d}", 2, ride("T1", 0, 1)) for k in range(18)]
    body = payload(bookings=bookings, capacity={"T1": 18, "T2": 2, "T3": 5})
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "accepted_ids": [f"G{k:02d}" for k in range(9)],
        "rejected_ids": [f"G{k:02d}" for k in range(9, 18)],
        "accepted_passengers": 18,
    }


def test_matches_brute_force_on_a_dense_random_scenario():
    rng = random.Random(20260925)
    net = network()
    capacity = {"T1": 6, "T2": 4, "T3": 5}
    stop_counts = {t["id"]: len(t["stops"]) for t in net["trips"]}
    bookings = []
    for k in range(10):
        tid = rng.choice(["T1", "T2", "T3"])
        board = rng.randrange(0, stop_counts[tid] - 1)
        alight = rng.randrange(board + 1, stop_counts[tid])
        bookings.append(booking(f"G{k:02d}", rng.randrange(1, 5), ride(tid, board, alight)))
    body = {
        **net,
        "capacity": capacity,
        "transfer_buffer": 10,
        "bookings": bookings,
    }
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()

    # Exhaustive reference search over all subsets.
    def feasible(sub):
        load = {"T1": [0, 0], "T2": [0], "T3": [0]}
        for bk in sub:
            for rd in bk["rides"]:
                for i in range(rd["board_index"], rd["alight_index"]):
                    load[rd["trip_id"]][i] += bk["passengers"]
        return all(x <= capacity[tid] for tid, xs in load.items() for x in xs)

    best_p, best_ids = -1, ()
    for mask in range(1 << len(bookings)):
        sub = [bk for i, bk in enumerate(bookings) if mask >> i & 1]
        if not feasible(sub):
            continue
        p = sum(bk["passengers"] for bk in sub)
        ids = tuple(sorted(bk["id"] for bk in sub))
        if p > best_p or (p == best_p and ids < best_ids):
            best_p, best_ids = p, ids

    assert out["accepted_passengers"] == best_p
    assert out["accepted_ids"] == list(best_ids)
    assert out["rejected_ids"] == sorted(
        bk["id"] for bk in bookings if bk["id"] not in best_ids
    )
