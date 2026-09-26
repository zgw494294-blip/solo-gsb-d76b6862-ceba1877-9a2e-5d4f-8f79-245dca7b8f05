"""End-to-end tests for POST /v1/transfers/hold."""

from __future__ import annotations

import random
from itertools import product

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def stop(station, arrival, departure):
    return {"station": station, "arrival": arrival, "departure": departure}


def outgoing(trip_id, stop_index, onboard):
    return {"trip_id": trip_id, "stop_index": stop_index, "onboard": onboard}


def group(id, actual_arrival, trip_id, passengers):
    return {
        "id": id,
        "actual_arrival": actual_arrival,
        "trip_id": trip_id,
        "passengers": passengers,
    }


def trips():
    return [
        {"id": "T1", "stops": [
            stop("B", 150, 155), stop("C", 195, 200), stop("D", 240, 240)]},
        {"id": "T2", "stops": [
            stop("A", 180, 182), stop("C", 210, 215), stop("E", 260, 260)]},
        {"id": "T3", "stops": [stop("C", 230, 235), stop("D", 270, 270)]},
    ]


def payload(**overrides):
    base = {
        "stations": ["A", "B", "C", "D", "E"],
        "trips": trips(),
        "station": "C",
        "outgoing": [
            outgoing("T1", 1, 5),
            outgoing("T2", 1, 2),
            outgoing("T3", 0, 9),
        ],
        "groups": [
            group("G1", 190, "T1", 4),
            group("G2", 208, "T1", 6),
            group("G3", 212, "T2", 3),
        ],
        "transfer_buffer": 5,
        "max_hold": 20,
        "min_interval": 10,
    }
    base.update(overrides)
    return base


def post(body):
    return client.post("/v1/transfers/hold", json=body)


def brute_force(deps, caps, onboard, reqs, min_interval):
    """Exhaustive reference: (holds, connected_ids, pax, onboard_delay)."""
    best = None
    for holds in product(*[range(c + 1) for c in caps]):
        if any(
            deps[i + 1] + holds[i + 1] - deps[i] - holds[i] < min_interval
            for i in range(len(holds) - 1)
        ):
            continue
        connected = sorted(
            gid
            for i, rs in enumerate(reqs)
            for need, _, gid in rs
            if need <= holds[i]
        )
        pax = sum(
            p
            for i, rs in enumerate(reqs)
            for need, p, _ in rs
            if need <= holds[i]
        )
        cost = sum(o * h for o, h in zip(onboard, holds))
        label = (-pax, cost, holds)
        if best is None or label < best[0]:
            best = (label, connected, pax, cost)
    label, connected, pax, cost = best
    return list(label[2]), connected, pax, cost


# ---------------------------------------------------------------- happy path

def test_holds_cascade_to_connect_everyone():
    # G1 (req -5) always connects; G2 needs T1 held by 13, which pushes T2
    # to 8 via the 10-minute interval (215 + h2 >= 213 + 10), and that also
    # covers G3 (req 2); T3 stays at 0 (235 >= 223 + 10).
    r = post(payload())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "holds": [
            {"trip_id": "T1", "delay_minutes": 13},
            {"trip_id": "T2", "delay_minutes": 8},
            {"trip_id": "T3", "delay_minutes": 0},
        ],
        "connected_ids": ["G1", "G2", "G3"],
        "connected_passengers": 13,
        "onboard_delay_minutes": 5 * 13 + 2 * 8,
    }


def test_no_hold_when_every_group_already_connects():
    body = payload(groups=[group("G1", 100, "T1", 4), group("G2", 150, "T3", 2)])
    out = post(body).json()
    assert out["holds"] == [
        {"trip_id": "T1", "delay_minutes": 0},
        {"trip_id": "T2", "delay_minutes": 0},
        {"trip_id": "T3", "delay_minutes": 0},
    ]
    assert out["connected_ids"] == ["G1", "G2"]
    assert out["connected_passengers"] == 6
    assert out["onboard_delay_minutes"] == 0


def test_connecting_one_passenger_beats_any_onboard_delay():
    # Passenger count dominates: holding T1 by 5 costs 500 onboard-minutes
    # but still wins over leaving the group behind.
    body = payload(
        outgoing=[outgoing("T1", 1, 100)],
        groups=[group("G1", 200, "T1", 2)],
    )
    out = post(body).json()
    assert out["holds"] == [{"trip_id": "T1", "delay_minutes": 5}]
    assert out["connected_passengers"] == 2
    assert out["onboard_delay_minutes"] == 500


def test_unreachable_group_is_left_behind_at_zero_cost():
    # The group would need a hold of 25 but max_hold is 20.
    body = payload(
        outgoing=[outgoing("T1", 1, 7)],
        groups=[group("G1", 220, "T1", 9)],
    )
    out = post(body).json()
    assert out["holds"] == [{"trip_id": "T1", "delay_minutes": 0}]
    assert out["connected_ids"] == []
    assert out["connected_passengers"] == 0
    assert out["onboard_delay_minutes"] == 0


def test_exact_boundary_arrival_plus_buffer_connects():
    # actual_arrival + buffer == adjusted departure connects (boundary).
    body = payload(
        outgoing=[outgoing("T1", 1, 3)],
        groups=[group("G1", 203, "T1", 4)],
        transfer_buffer=5,
    )
    out = post(body).json()
    assert out["holds"] == [{"trip_id": "T1", "delay_minutes": 8}]
    assert out["connected_passengers"] == 4


def test_hold_is_capped_by_the_2879_window():
    # T1 ends at 2870, so its hold is capped at 9 even though max_hold is
    # 30: G1 (req 5) connects, G2 (req 14) can never be reached.
    late = [{"id": "T1", "stops": [stop("A", 2860, 2865), stop("C", 2870, 2870)]}]
    body = payload(
        trips=late,
        outgoing=[outgoing("T1", 1, 1)],
        groups=[group("G1", 2870, "T1", 3), group("G2", 2879, "T1", 8)],
        max_hold=30,
    )
    out = post(body).json()
    assert out["holds"] == [{"trip_id": "T1", "delay_minutes": 5}]
    assert out["connected_ids"] == ["G1"]
    assert out["connected_passengers"] == 3
    assert out["onboard_delay_minutes"] == 5


def test_trip_visiting_the_station_twice_uses_the_indexed_stop():
    loop = [{"id": "T1", "stops": [
        stop("C", 100, 105), stop("D", 120, 120),
        stop("C", 150, 155), stop("E", 200, 200)]}]
    body = payload(
        trips=loop,
        outgoing=[outgoing("T1", 2, 0)],
        groups=[group("G1", 152, "T1", 4)],
    )
    out = post(body).json()
    # req = 152 + 5 - 155 = 2, against the second visit's departure (155).
    assert out["holds"] == [{"trip_id": "T1", "delay_minutes": 2}]
    assert out["connected_passengers"] == 4


# ------------------------------------------------------------------ tie-break

def test_zero_onboard_tie_takes_lexicographically_smallest_holds():
    # All onboard counts are 0, so every hold vector reaching G1 ties on
    # both objectives; (0, 4) is the lexicographically smallest.
    body = payload(
        outgoing=[outgoing("T1", 1, 0), outgoing("T2", 1, 0)],
        groups=[group("G1", 214, "T2", 5)],
        min_interval=0,
    )
    out = post(body).json()
    assert out["holds"] == [
        {"trip_id": "T1", "delay_minutes": 0},
        {"trip_id": "T2", "delay_minutes": 4},
    ]


def test_lexicographic_tie_minimises_every_position():
    body = payload(
        outgoing=[outgoing("T1", 1, 0), outgoing("T2", 1, 0)],
        groups=[group("G1", 197, "T1", 2), group("G2", 213, "T2", 3)],
        min_interval=0,
    )
    out = post(body).json()
    assert out["holds"] == [
        {"trip_id": "T1", "delay_minutes": 2},
        {"trip_id": "T2", "delay_minutes": 3},
    ]


def test_interval_cascade_lands_on_a_free_trip():
    # T1 departs 200, T2 departs 210, min_interval 10 (gap exactly 10).
    # G1 needs T1 held by 3, which forces T2 up to 3 as well; T2 carries
    # nobody (onboard 0), so the cascade is free and lex-min does not
    # inflate it further.
    pair = [
        {"id": "T1", "stops": [stop("C", 200, 200), stop("D", 230, 230)]},
        {"id": "T2", "stops": [stop("C", 210, 210), stop("E", 240, 240)]},
    ]
    body = payload(
        trips=pair,
        outgoing=[outgoing("T1", 0, 5), outgoing("T2", 0, 0)],
        groups=[group("G1", 198, "T1", 7)],
        transfer_buffer=5,
        min_interval=10,
    )
    out = post(body).json()
    assert out["holds"] == [
        {"trip_id": "T1", "delay_minutes": 3},
        {"trip_id": "T2", "delay_minutes": 3},
    ]
    assert out["connected_passengers"] == 7
    assert out["onboard_delay_minutes"] == 15


# ------------------------------------------------------------ 422 validation

def test_station_must_be_known():
    assert post(payload(station="Z")).status_code == 422
    body = payload()
    del body["station"]
    assert post(body).status_code == 422


def test_outgoing_count_and_uniqueness():
    assert post(payload(outgoing=[])).status_code == 422
    many = [outgoing("T1", 1, 0)] * 11
    assert post(payload(outgoing=many)).status_code == 422
    dup = [outgoing("T1", 1, 0), outgoing("T1", 1, 0)]
    assert post(payload(outgoing=dup)).status_code == 422


def test_outgoing_trip_must_exist():
    assert post(payload(outgoing=[outgoing("TX", 1, 0)])).status_code == 422


def test_outgoing_stop_index_bounds_and_station():
    # Negative / beyond the last stop.
    assert post(payload(outgoing=[outgoing("T1", -1, 0)])).status_code == 422
    assert post(payload(outgoing=[outgoing("T1", 3, 0)])).status_code == 422
    # Index 0 of T1 is station B, not C.
    assert post(payload(outgoing=[outgoing("T1", 0, 0)])).status_code == 422
    # The last stop is a valid departure point.
    last = [{"id": "T1", "stops": [stop("A", 100, 105), stop("C", 150, 150)]}]
    body = payload(trips=last, outgoing=[outgoing("T1", 1, 0)],
                   groups=[group("G1", 100, "T1", 1)])
    assert post(body).status_code == 200


def test_outgoing_onboard_must_be_non_negative():
    assert post(payload(outgoing=[outgoing("T1", 1, -1)])).status_code == 422


def test_outgoing_must_be_sorted_by_departure():
    swapped = [outgoing("T2", 1, 0), outgoing("T1", 1, 0)]
    assert post(payload(outgoing=swapped)).status_code == 422


def test_original_departures_must_keep_min_interval():
    assert post(payload(min_interval=16)).status_code == 422
    # Gap of exactly min_interval is fine (15 >= 15).
    assert post(payload(min_interval=15)).status_code == 200


def test_groups_validation():
    assert post(payload(groups=[])).status_code == 422
    many = [group(f"G{i}", 100, "T1", 1) for i in range(31)]
    assert post(payload(groups=many)).status_code == 422
    dup = [group("G1", 100, "T1", 1), group("G1", 110, "T2", 1)]
    assert post(payload(groups=dup)).status_code == 422
    # Target must be one of the outgoing trips (T3 is, TX is not).
    assert post(payload(groups=[group("G1", 100, "TX", 1)])).status_code == 422
    assert post(payload(groups=[group("G1", 100, "T1", 0)])).status_code == 422
    assert post(payload(groups=[group("G1", 100, "T1", -2)])).status_code == 422
    assert post(payload(groups=[group("G1", -1, "T1", 1)])).status_code == 422
    assert post(payload(groups=[group("G1", 2880, "T1", 1)])).status_code == 422
    body = payload()
    del body["groups"]
    assert post(body).status_code == 422


def test_scalar_field_bounds():
    assert post(payload(transfer_buffer=0)).status_code == 422
    assert post(payload(transfer_buffer=61)).status_code == 422
    assert post(payload(max_hold=-1)).status_code == 422
    assert post(payload(max_hold=31)).status_code == 422
    assert post(payload(min_interval=-1)).status_code == 422
    assert post(payload(min_interval=61)).status_code == 422
    for field in ("transfer_buffer", "max_hold", "min_interval", "outgoing"):
        body = payload()
        del body[field]
        assert post(body).status_code == 422


def test_extra_fields_return_422():
    body = payload()
    body["nope"] = 1
    assert post(body).status_code == 422

    body = payload()
    body["outgoing"][0]["nope"] = 1
    assert post(body).status_code == 422

    body = payload()
    body["groups"][0]["nope"] = 1
    assert post(body).status_code == 422


def test_malformed_json_returns_422():
    r = client.post(
        "/v1/transfers/hold",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ stress

def random_case(rng, base_dep, max_gap):
    """Build a random valid request and its brute-force reference data."""
    n = rng.randint(1, 4)
    min_interval = rng.randint(0, 3)
    max_hold = rng.randint(0, 4)
    buffer = rng.randint(1, 5)
    deps = [base_dep]
    while len(deps) < n:
        deps.append(deps[-1] + min_interval + rng.randint(0, max_gap))
    if deps[-1] > 2878:
        return None
    stations = ["C"]
    trips_data = []
    outgoing_data = []
    caps = []
    for i, dep in enumerate(deps):
        prev_station, next_station = f"P{i}", f"Q{i}"
        stations += [prev_station, next_station]
        slack = 2879 - dep
        lead = rng.randint(1, min(10, slack))
        trips_data.append({"id": f"T{i}", "stops": [
            stop(prev_station, dep - 10, dep - 6),
            stop("C", dep - 1, dep),
            stop(next_station, dep + lead, dep + lead),
        ]})
        onboard = rng.randint(0, 5)
        outgoing_data.append(outgoing(f"T{i}", 1, onboard))
        caps.append(min(max_hold, 2879 - (dep + lead)))
    reqs = [[] for _ in range(n)]
    groups_data = []
    for g in range(rng.randint(1, 6)):
        target = rng.randrange(n)
        arrival = min(2879, max(0, deps[target] + rng.randint(-15, 15)))
        passengers = rng.randint(1, 9)
        groups_data.append(group(f"G{g}", arrival, f"T{target}", passengers))
        reqs[target].append(
            (arrival + buffer - deps[target], passengers, f"G{g}")
        )
    body = {
        "stations": stations,
        "trips": trips_data,
        "station": "C",
        "outgoing": outgoing_data,
        "groups": groups_data,
        "transfer_buffer": buffer,
        "max_hold": max_hold,
        "min_interval": min_interval,
    }
    onboard = [entry["onboard"] for entry in outgoing_data]
    reference = brute_force(deps, caps, onboard, reqs, min_interval)
    return body, reference


def check_against_reference(body, reference):
    holds, connected_ids, pax, cost = reference
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [h["delay_minutes"] for h in out["holds"]] == holds
    assert [h["trip_id"] for h in out["holds"]] == [
        entry["trip_id"] for entry in body["outgoing"]
    ]
    assert out["connected_ids"] == connected_ids
    assert out["connected_passengers"] == pax
    assert out["onboard_delay_minutes"] == cost


def test_matches_brute_force_on_random_scenarios():
    rng = random.Random(20260926)
    for _ in range(80):
        case = random_case(rng, rng.randint(100, 200), max_gap=8)
        if case is not None:
            check_against_reference(*case)


def test_matches_brute_force_near_the_2879_boundary():
    rng = random.Random(20260927)
    for _ in range(40):
        case = random_case(rng, rng.randint(2850, 2870), max_gap=2)
        if case is not None:
            check_against_reference(*case)


def test_maximum_size_request_stays_fast_and_valid():
    # 10 outgoing trips 20 minutes apart, 30 groups spread across them.
    trips_data = []
    outgoing_data = []
    groups_data = []
    for i in range(10):
        dep = 600 + 20 * i
        trips_data.append({"id": f"T{i}", "stops": [
            stop(f"P{i}", dep - 10, dep - 6),
            stop("C", dep - 1, dep),
            stop(f"Q{i}", dep + 12, dep + 12),
        ]})
        outgoing_data.append(outgoing(f"T{i}", 1, i))
    for g in range(30):
        target = g % 10
        dep = 600 + 20 * target
        groups_data.append(
            group(f"G{g:02d}", dep - 8 + (g % 5), f"T{target}", g + 1)
        )
    body = {
        "stations": ["C"] + [f"P{i}" for i in range(10)]
        + [f"Q{i}" for i in range(10)],
        "trips": trips_data,
        "station": "C",
        "outgoing": outgoing_data,
        "groups": groups_data,
        "transfer_buffer": 6,
        "max_hold": 30,
        "min_interval": 15,
    }
    r = post(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["holds"]) == 10
    # Every group can be reached within 30 minutes, so all must connect.
    assert out["connected_passengers"] == sum(range(1, 31))
    assert out["connected_ids"] == [f"G{g:02d}" for g in range(30)]
    # Interval feasibility of the returned holds.
    for i in range(1, 10):
        prev = 600 + 20 * (i - 1) + out["holds"][i - 1]["delay_minutes"]
        curr = 600 + 20 * i + out["holds"][i]["delay_minutes"]
        assert curr - prev >= 15
