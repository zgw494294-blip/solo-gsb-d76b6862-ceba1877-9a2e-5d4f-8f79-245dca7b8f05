"""End-to-end tests for POST /v1/gtfs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

STOPS = "stop_id,stop_name\nS1,Alpha\nS2,Beta\nS3,Gamma\nS4,Delta\n"

TRIPS = (
    "route_id,service_id,trip_id\n"
    "R1,WD,T1\n"
    "R1,WD,T2\n"
)

STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "T1,08:00:00,08:00:00,S1,1\n"
    "T1,08:30:00,08:35:00,S2,2\n"
    "T1,09:10:00,09:10:00,S3,3\n"
    "T2,09:20:00,09:20:00,S3,1\n"
    "T2,10:00:00,10:00:00,S4,2\n"
)

# 2026-09-25 is a Friday, 2026-09-26 a Saturday.
CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
    "start_date,end_date\n"
    "WD,1,1,1,1,1,0,0,20260101,20261231\n"
)

FREQUENCIES = (
    "trip_id,start_time,end_time,headway_secs,exact_times\n"
    "T1,07:00:00,08:00:00,1800,1\n"
)


def payload(date="20260925", **files):
    base = {
        "stops.txt": STOPS,
        "trips.txt": TRIPS,
        "stop_times.txt": STOP_TIMES,
        "calendar.txt": CALENDAR,
    }
    base.update(files)
    return {"date": date, "files": base}


def post(body):
    return client.post("/v1/gtfs", json=body)


# ---------------------------------------------------------------- happy path

def test_basic_conversion_weekday_active():
    r = post(payload())
    assert r.status_code == 200
    body = r.json()
    assert body["stations"] == ["S1", "S2", "S3", "S4"]
    assert body["trips"] == [
        {
            "id": "T1",
            "stops": [
                {"station": "S1", "arrival": 480, "departure": 480},
                {"station": "S2", "arrival": 510, "departure": 515},
                {"station": "S3", "arrival": 550, "departure": 550},
            ],
        },
        {
            "id": "T2",
            "stops": [
                {"station": "S3", "arrival": 560, "departure": 560},
                {"station": "S4", "arrival": 600, "departure": 600},
            ],
        },
    ]


def test_inactive_weekday_yields_empty_trips():
    # 2026-09-26 is a Saturday; WD runs Monday-Friday only.
    r = post(payload(date="20260926"))
    assert r.status_code == 200
    assert r.json() == {"stations": ["S1", "S2", "S3", "S4"], "trips": []}


def test_outside_date_range_yields_empty_trips():
    r = post(payload(date="20270105"))
    assert r.status_code == 200
    assert r.json()["trips"] == []


def test_calendar_dates_exception_added_service():
    # Exception 1 adds service on a Saturday that the calendar skips.
    calendar_dates = "service_id,date,exception_type\nWD,20260926,1\n"
    r = post(payload(date="20260926", **{"calendar_dates.txt": calendar_dates}))
    assert r.status_code == 200
    assert [trip["id"] for trip in r.json()["trips"]] == ["T1", "T2"]


def test_calendar_dates_exception_removed_service():
    # Exception 2 removes service on a regular Friday and wins over the
    # calendar.
    calendar_dates = "service_id,date,exception_type\nWD,20260925,2\n"
    r = post(payload(date="20260925", **{"calendar_dates.txt": calendar_dates}))
    assert r.status_code == 200
    assert r.json()["trips"] == []


def test_calendar_dates_can_define_service_without_calendar_row():
    calendar_dates = "service_id,date,exception_type\nSP,20260926,1\n"
    files = payload(
        date="20260926",
        **{
            "trips.txt": "route_id,service_id,trip_id\nR1,SP,T9\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T9,25:05:00,25:05:00,S1,1\n"
                "T9,26:00:00,26:00:00,S4,2\n"
            ),
            "calendar_dates.txt": calendar_dates,
        },
    )
    r = post(files)
    assert r.status_code == 200
    body = r.json()
    assert body["trips"] == [
        {
            "id": "T9",
            "stops": [
                {"station": "S1", "arrival": 1505, "departure": 1505},
                {"station": "S4", "arrival": 1560, "departure": 1560},
            ],
        }
    ]


def test_stop_sequence_sorted_numerically_not_textually():
    stop_times = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,09:10:00,09:10:00,S3,10\n"
        "T1,08:00:00,08:00:00,S1,2\n"
        "T1,08:30:00,08:35:00,S2,9\n"
        "T2,09:20:00,09:20:00,S3,1\n"
        "T2,10:00:00,10:00:00,S4,2\n"
    )
    r = post(payload(**{"stop_times.txt": stop_times}))
    assert r.status_code == 200
    t1 = r.json()["trips"][0]
    assert [s["station"] for s in t1["stops"]] == ["S1", "S2", "S3"]


def test_frequencies_expand_and_replace_template():
    r = post(payload(**{"frequencies.txt": FREQUENCIES}))
    assert r.status_code == 200
    trips = r.json()["trips"]
    assert [trip["id"] for trip in trips] == ["T1@420", "T1@450", "T2"]
    assert trips[0]["stops"] == [
        {"station": "S1", "arrival": 420, "departure": 420},
        {"station": "S2", "arrival": 450, "departure": 455},
        {"station": "S3", "arrival": 490, "departure": 490},
    ]
    assert trips[1]["stops"] == [
        {"station": "S1", "arrival": 450, "departure": 450},
        {"station": "S2", "arrival": 480, "departure": 485},
        {"station": "S3", "arrival": 520, "departure": 520},
    ]


def test_frequencies_of_inactive_service_generate_nothing():
    r = post(payload(date="20260926", **{"frequencies.txt": FREQUENCIES}))
    assert r.status_code == 200
    assert r.json()["trips"] == []


def test_unknown_extra_files_are_ignored():
    r = post(payload(**{"routes.txt": "route_id,route_type\nR1,3\n"}))
    assert r.status_code == 200
    assert [trip["id"] for trip in r.json()["trips"]] == ["T1", "T2"]


def test_converted_network_feeds_journey_planner():
    network = post(payload()).json()
    r = client.post(
        "/v1/journey",
        json={
            **network,
            "origin": "S1",
            "destination": "S4",
            "earliest_departure": 0,
            "transfer_buffer": 10,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["arrival_time"] == 600
    assert body["trip_ids"] == ["T1", "T2"]


# ------------------------------------------------------------------ 422 cases

def test_missing_required_file():
    body = payload()
    del body["files"]["calendar.txt"]
    r = post(body)
    assert r.status_code == 422


def test_invalid_request_date():
    assert post(payload(date="2026-09-25")).status_code == 422
    assert post(payload(date="20261301")).status_code == 422


def test_duplicate_stop_id():
    stops = "stop_id\nS1\nS2\nS1\n"
    assert post(payload(**{"stops.txt": stops})).status_code == 422


def test_duplicate_trip_id():
    trips = "route_id,service_id,trip_id\nR1,WD,T1\nR1,WD,T1\n"
    assert post(payload(**{"trips.txt": trips})).status_code == 422


def test_duplicate_calendar_service_id():
    calendar = CALENDAR + "WD,1,1,1,1,1,1,1,20260101,20261231\n"
    assert post(payload(**{"calendar.txt": calendar})).status_code == 422


def test_duplicate_calendar_dates_key():
    calendar_dates = (
        "service_id,date,exception_type\nWD,20260925,1\nWD,20260925,2\n"
    )
    assert (
        post(payload(**{"calendar_dates.txt": calendar_dates})).status_code == 422
    )


def test_duplicate_stop_sequence_within_trip():
    stop_times = STOP_TIMES + "T1,09:30:00,09:30:00,S4,3\n"
    assert post(payload(**{"stop_times.txt": stop_times})).status_code == 422


def test_duplicate_frequency_row():
    frequencies = FREQUENCIES + "T1,09:00:00,10:00:00,1800,1\n"
    assert post(payload(**{"frequencies.txt": frequencies})).status_code == 422


def test_unknown_references():
    bad_stop_times = STOP_TIMES + "T9,11:00:00,11:00:00,S1,1\n"
    assert post(payload(**{"stop_times.txt": bad_stop_times})).status_code == 422

    bad_stop = STOP_TIMES + "T1,11:00:00,11:00:00,S9,4\n"
    assert post(payload(**{"stop_times.txt": bad_stop})).status_code == 422

    bad_service = "route_id,service_id,trip_id\nR1,XX,T1\n"
    assert post(payload(**{"trips.txt": bad_service})).status_code == 422

    bad_frequency = (
        "trip_id,start_time,end_time,headway_secs,exact_times\n"
        "T9,07:00:00,08:00:00,1800,1\n"
    )
    assert (
        post(payload(**{"frequencies.txt": bad_frequency})).status_code == 422
    )


def test_time_format_violations():
    seconds = STOP_TIMES.replace("08:30:00", "08:30:15")
    assert post(payload(**{"stop_times.txt": seconds})).status_code == 422

    beyond = STOP_TIMES.replace("10:00:00", "48:00:00")
    assert post(payload(**{"stop_times.txt": beyond})).status_code == 422

    malformed = STOP_TIMES.replace("08:00:00", "8am", 1)
    assert post(payload(**{"stop_times.txt": malformed})).status_code == 422


def test_time_sequence_violations():
    inverted = STOP_TIMES.replace("08:30:00,08:35:00", "08:35:00,08:30:00")
    assert post(payload(**{"stop_times.txt": inverted})).status_code == 422

    not_strict = STOP_TIMES.replace("09:10:00", "08:35:00")
    assert post(payload(**{"stop_times.txt": not_strict})).status_code == 422


def test_calendar_field_violations():
    bad_flag = CALENDAR.replace("1,1,1,1,1,0,0", "1,1,1,1,1,0,x")
    assert post(payload(**{"calendar.txt": bad_flag})).status_code == 422

    inverted = CALENDAR.replace("20260101,20261231", "20261231,20260101")
    assert post(payload(**{"calendar.txt": inverted})).status_code == 422

    bad_exception = "service_id,date,exception_type\nWD,20260925,3\n"
    assert (
        post(payload(**{"calendar_dates.txt": bad_exception})).status_code == 422
    )


def test_frequency_field_violations():
    not_exact = FREQUENCIES.replace(",1\n", ",0\n")
    assert post(payload(**{"frequencies.txt": not_exact})).status_code == 422

    sub_minute = FREQUENCIES.replace("1800", "1799")
    assert post(payload(**{"frequencies.txt": sub_minute})).status_code == 422

    zero = FREQUENCIES.replace("1800", "0")
    assert post(payload(**{"frequencies.txt": zero})).status_code == 422

    inverted = FREQUENCIES.replace("07:00:00,08:00:00", "08:00:00,07:00:00")
    assert post(payload(**{"frequencies.txt": inverted})).status_code == 422


def test_network_limit_violations():
    # Too few stations.
    one_stop = "stop_id\nS1\n"
    assert post(payload(**{"stops.txt": one_stop})).status_code == 422

    # Too many stations.
    many = "stop_id\n" + "".join(f"ST{i}\n" for i in range(41))
    assert post(payload(**{"stops.txt": many})).status_code == 422

    # A running trip with fewer than 2 stops.
    single = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\n"
    )
    trips = "route_id,service_id,trip_id\nR1,WD,T1\n"
    assert (
        post(
            payload(**{"stop_times.txt": single, "trips.txt": trips})
        ).status_code
        == 422
    )

    # A running trip with more than 20 stops.
    seq = list(range(1, 22))
    times = "\n".join(
        f"T1,{8 + i // 6:02d}:{(i * 10) % 60:02d}:00,"
        f"{8 + i // 6:02d}:{(i * 10) % 60:02d}:00,S{1 + i % 4},{s}"
        for i, s in enumerate(seq)
    )
    many_stops = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + times + "\n"
    )
    assert (
        post(
            payload(**{"stop_times.txt": many_stops, "trips.txt": trips})
        ).status_code
        == 422
    )


def test_frequency_expansion_overflowing_trip_limit():
    frequencies = (
        "trip_id,start_time,end_time,headway_secs,exact_times\n"
        "T1,00:00:00,02:00:00,60,1\n"
    )
    early = STOP_TIMES.replace("08:", "00:").replace("09:10", "00:40").replace(
        "09:20", "00:50"
    ).replace("10:00", "01:00")
    assert (
        post(
            payload(
                **{"frequencies.txt": frequencies, "stop_times.txt": early}
            )
        ).status_code
        == 422
    )


def test_frequency_shifted_times_out_of_range():
    # The template's last stop sits at 47:50; shifting the first departure
    # to 47:55 pushes the arrival past 47:59.
    late = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,47:00:00,47:00:00,S1,1\n"
        "T1,47:50:00,47:50:00,S2,2\n"
    )
    trips = "route_id,service_id,trip_id\nR1,WD,T1\n"
    frequencies = (
        "trip_id,start_time,end_time,headway_secs,exact_times\n"
        "T1,47:55:00,47:56:00,60,1\n"
    )
    assert (
        post(
            payload(
                **{
                    "stop_times.txt": late,
                    "trips.txt": trips,
                    "frequencies.txt": frequencies,
                }
            )
        ).status_code
        == 422
    )


def test_missing_required_column():
    stops = "stop_name\nAlpha\nBeta\n"
    assert post(payload(**{"stops.txt": stops})).status_code == 422


def test_ragged_row():
    stops = "stop_id,stop_name\nS1,Alpha\nS2\n"
    assert post(payload(**{"stops.txt": stops})).status_code == 422


def test_running_trip_without_stop_times():
    trips = TRIPS + "R1,WD,T9\n"
    assert post(payload(**{"trips.txt": trips})).status_code == 422


def test_inactive_trip_without_stop_times_is_ignored():
    trips = TRIPS + "R1,WD,T9\n"
    r = post(payload(date="20260926", **{"trips.txt": trips}))
    assert r.status_code == 200
    assert r.json()["trips"] == []
