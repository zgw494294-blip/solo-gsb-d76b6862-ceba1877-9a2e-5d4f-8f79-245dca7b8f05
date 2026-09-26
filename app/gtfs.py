"""GTFS-subset to fixed-schedule network conversion.

Pure standard-library module so it can be unit-tested without FastAPI.

Reads a small GTFS subset supplied as in-memory CSV text and produces the
``stations`` / ``trips`` network accepted by the journey-planning endpoints
for one concrete service date:

- ``calendar.txt`` activates a service through its date range and weekday
  flags; ``calendar_dates.txt`` exceptions take priority for the requested
  date (``1`` = added, ``2`` = removed).
- ``stop_times.txt`` rows of a trip are ordered by numeric
  ``stop_sequence``.
- Every time must lie within 00:00:00..47:59:00 with zero seconds and is
  returned as minutes after midnight (0..2879).
- ``frequencies.txt`` (only ``exact_times=1`` with a positive whole-minute
  headway) expands its template trip into one trip per first-departure
  time in ``[start_time, end_time)`` stepped by ``headway_secs``; the whole
  trip is shifted relative to the template's first departure and *replaces*
  the template.  Generated ids are ``<trip_id>@<first departure minute>``;
  plain trips keep their id.

Any reference error, duplicate key, time-sequence error or breach of the
network limits (2-40 stations, at most 100 resulting trips, 2-20 stops per
resulting trip, times within 0..2879 minutes) raises ``ValueError``, which
the API layer turns into HTTP 422 for the whole batch.  A date with no
running service yields an empty ``trips`` list.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date as date_class

MAX_MINUTE = 2879  # 48 hours in minutes, matches the network limit
MIN_STATIONS, MAX_STATIONS = 2, 40
MAX_TRIPS = 100
MIN_STOPS, MAX_STOPS = 2, 20

REQUIRED_FILES = ("stops.txt", "trips.txt", "stop_times.txt", "calendar.txt")
OPTIONAL_FILES = ("calendar_dates.txt", "frequencies.txt")

WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

REQUIRED_COLUMNS = {
    "stops.txt": ("stop_id",),
    "trips.txt": ("trip_id", "service_id"),
    "stop_times.txt": (
        "trip_id",
        "arrival_time",
        "departure_time",
        "stop_id",
        "stop_sequence",
    ),
    "calendar.txt": ("service_id",) + WEEKDAY_COLUMNS + ("start_date", "end_date"),
    "calendar_dates.txt": ("service_id", "date", "exception_type"),
    "frequencies.txt": (
        "trip_id",
        "start_time",
        "end_time",
        "headway_secs",
        "exact_times",
    ),
}

_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{8}$")
_INT_RE = re.compile(r"^\d+$")


def convert_gtfs(*, date_text: str, files: dict[str, str]) -> dict:
    """Convert a GTFS subset into one day's ``stations`` / ``trips`` network.

    ``date_text`` is the service date in ``YYYYMMDD`` form; ``files`` maps
    GTFS file names to their CSV text.  Returns
    ``{"stations": [...], "trips": [{"id", "stops"}]}`` with all times as
    minutes after midnight.  Raises ``ValueError`` on the first violation.
    """
    service_date = _parse_date("request", "date", date_text)

    missing = [name for name in REQUIRED_FILES if name not in files]
    if missing:
        raise ValueError(f"missing required GTFS files: {missing}")
    tables = {
        name: _read_table(name, files[name])
        for name in REQUIRED_FILES + OPTIONAL_FILES
        if name in files
    }

    stations = _read_stations(tables["stops.txt"])
    calendar = _read_calendar(tables["calendar.txt"])
    exceptions = _read_calendar_dates(tables.get("calendar_dates.txt", []))
    trip_service, trip_order = _read_trips(
        tables["trips.txt"], calendar, exceptions
    )
    templates = _read_stop_times(tables["stop_times.txt"], trip_service, stations)
    frequencies = _read_frequencies(tables.get("frequencies.txt", []), trip_service)

    def is_active(service_id: str) -> bool:
        exception = exceptions.get((service_id, service_date))
        if exception == "1":
            return True
        if exception == "2":
            return False
        entry = calendar.get(service_id)
        if entry is None:
            return False
        start, end, weekdays = entry
        return start <= service_date <= end and weekdays[service_date.weekday()]

    trips: list[dict] = []
    used_ids: set[str] = set()
    for trip_id in trip_order:
        if not is_active(trip_service[trip_id]):
            continue
        template = templates.get(trip_id)
        if template is None:
            raise ValueError(
                f"trip {trip_id!r} runs on {date_text} but has no stop_times"
            )
        frequency = frequencies.get(trip_id)
        if frequency is None:
            instances = [(trip_id, 0)]
        else:
            start, end, headway = frequency
            first_departure = template[0][2]
            instances = [
                (f"{trip_id}@{start_minute}", start_minute - first_departure)
                for start_minute in range(start, end, headway)
            ]
        for instance_id, shift in instances:
            if instance_id in used_ids:
                raise ValueError(
                    f"duplicate trip id in the converted network: {instance_id!r}"
                )
            used_ids.add(instance_id)
            stops = []
            for station, arrival, departure in template:
                arr, dep = arrival + shift, departure + shift
                if not (0 <= arr <= MAX_MINUTE and 0 <= dep <= MAX_MINUTE):
                    raise ValueError(
                        f"trip {instance_id!r}: shifted time outside "
                        f"0..{MAX_MINUTE} minutes"
                    )
                stops.append(
                    {"station": station, "arrival": arr, "departure": dep}
                )
            if not MIN_STOPS <= len(stops) <= MAX_STOPS:
                raise ValueError(
                    f"trip {instance_id!r}: {len(stops)} stops, "
                    f"need {MIN_STOPS}..{MAX_STOPS}"
                )
            trips.append({"id": instance_id, "stops": stops})
    if len(trips) > MAX_TRIPS:
        raise ValueError(
            f"converted network has {len(trips)} trips, at most {MAX_TRIPS} allowed"
        )
    return {"stations": stations, "trips": trips}


# --------------------------------------------------------------------- files


def _read_table(name: str, text: str) -> list[dict[str, str]]:
    """Parse CSV text into a list of row dicts keyed by the header columns."""
    try:
        records = [
            [cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text))
        ]
    except csv.Error as exc:
        raise ValueError(f"{name}: invalid CSV: {exc}") from exc
    records = [row for row in records if any(row)]
    if not records:
        raise ValueError(f"{name}: missing header row")
    header, data = records[0], records[1:]
    if len(set(header)) != len(header):
        raise ValueError(f"{name}: duplicate column names in the header")
    missing = [c for c in REQUIRED_COLUMNS[name] if c not in header]
    if missing:
        raise ValueError(f"{name}: missing required columns {missing}")
    rows = []
    for line, record in enumerate(data, start=2):
        if len(record) != len(header):
            raise ValueError(
                f"{name}: row {line} has {len(record)} fields, "
                f"expected {len(header)}"
            )
        rows.append(dict(zip(header, record)))
    return rows


def _required(row: dict[str, str], column: str, name: str) -> str:
    value = row[column]
    if not value:
        raise ValueError(f"{name}: empty {column}")
    return value


# ------------------------------------------------------------------- parsers


def _parse_date(name: str, context: str, value: str) -> date_class:
    if not _DATE_RE.match(value):
        raise ValueError(
            f"{name}: {context}: invalid date {value!r} (expected YYYYMMDD)"
        )
    try:
        return date_class(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        raise ValueError(
            f"{name}: {context}: invalid date {value!r} (expected YYYYMMDD)"
        ) from None


def _parse_time(name: str, context: str, value: str) -> int:
    """Parse HH:MM:SS into minutes after midnight (0..2879, zero seconds)."""
    match = _TIME_RE.match(value)
    if match is None:
        raise ValueError(
            f"{name}: {context}: invalid time {value!r} (expected HH:MM:SS)"
        )
    hours, minutes, seconds = (int(group) for group in match.groups())
    if seconds != 0:
        raise ValueError(
            f"{name}: {context}: time {value!r} must have zero seconds"
        )
    if hours > 47:
        raise ValueError(
            f"{name}: {context}: time {value!r} is outside 00:00:00..47:59:00"
        )
    return hours * 60 + minutes


def _parse_int(name: str, context: str, value: str) -> int:
    if not _INT_RE.match(value):
        raise ValueError(
            f"{name}: {context}: invalid integer {value!r}"
        )
    return int(value)


# -------------------------------------------------------------------- tables


def _read_stations(rows: list[dict[str, str]]) -> list[str]:
    stations: list[str] = []
    seen: set[str] = set()
    for row in rows:
        stop_id = _required(row, "stop_id", "stops.txt")
        if stop_id in seen:
            raise ValueError(f"stops.txt: duplicate stop_id {stop_id!r}")
        seen.add(stop_id)
        stations.append(stop_id)
    if not MIN_STATIONS <= len(stations) <= MAX_STATIONS:
        raise ValueError(
            f"stops.txt: {len(stations)} stations, "
            f"need {MIN_STATIONS}..{MAX_STATIONS}"
        )
    return stations


def _read_calendar(
    rows: list[dict[str, str]],
) -> dict[str, tuple[date_class, date_class, tuple[bool, ...]]]:
    calendar: dict[str, tuple[date_class, date_class, tuple[bool, ...]]] = {}
    for row in rows:
        service_id = _required(row, "service_id", "calendar.txt")
        if service_id in calendar:
            raise ValueError(f"calendar.txt: duplicate service_id {service_id!r}")
        weekdays = []
        for column in WEEKDAY_COLUMNS:
            flag = row[column]
            if flag not in ("0", "1"):
                raise ValueError(
                    f"calendar.txt: service {service_id!r}: {column} must be "
                    f"0 or 1, got {flag!r}"
                )
            weekdays.append(flag == "1")
        start = _parse_date("calendar.txt", f"service {service_id!r} start_date",
                            _required(row, "start_date", "calendar.txt"))
        end = _parse_date("calendar.txt", f"service {service_id!r} end_date",
                          _required(row, "end_date", "calendar.txt"))
        if start > end:
            raise ValueError(
                f"calendar.txt: service {service_id!r}: start_date {start} is "
                f"after end_date {end}"
            )
        calendar[service_id] = (start, end, tuple(weekdays))
    return calendar


def _read_calendar_dates(
    rows: list[dict[str, str]],
) -> dict[tuple[str, date_class], str]:
    exceptions: dict[tuple[str, date_class], str] = {}
    for row in rows:
        service_id = _required(row, "service_id", "calendar_dates.txt")
        day = _parse_date(
            "calendar_dates.txt",
            f"service {service_id!r} date",
            _required(row, "date", "calendar_dates.txt"),
        )
        exception_type = _required(row, "exception_type", "calendar_dates.txt")
        if exception_type not in ("1", "2"):
            raise ValueError(
                f"calendar_dates.txt: service {service_id!r} on {day}: "
                f"exception_type must be 1 or 2, got {exception_type!r}"
            )
        key = (service_id, day)
        if key in exceptions:
            raise ValueError(
                f"calendar_dates.txt: duplicate exception for service "
                f"{service_id!r} on {day}"
            )
        exceptions[key] = exception_type
    return exceptions


def _read_trips(
    rows: list[dict[str, str]],
    calendar: dict[str, tuple],
    exceptions: dict[tuple[str, date_class], str],
) -> tuple[dict[str, str], list[str]]:
    known_services = set(calendar) | {service for service, _ in exceptions}
    trip_service: dict[str, str] = {}
    trip_order: list[str] = []
    for row in rows:
        trip_id = _required(row, "trip_id", "trips.txt")
        service_id = _required(row, "service_id", "trips.txt")
        if trip_id in trip_service:
            raise ValueError(f"trips.txt: duplicate trip_id {trip_id!r}")
        if service_id not in known_services:
            raise ValueError(
                f"trips.txt: trip {trip_id!r} references unknown service_id "
                f"{service_id!r}"
            )
        trip_service[trip_id] = service_id
        trip_order.append(trip_id)
    return trip_service, trip_order


def _read_stop_times(
    rows: list[dict[str, str]],
    trip_service: dict[str, str],
    stations: list[str],
) -> dict[str, list[tuple[str, int, int]]]:
    """Group stop_times by trip, ordered by numeric stop_sequence.

    Returns ``trip_id -> [(station, arrival, departure)]`` with times in
    minutes; every listed trip is checked for a valid time sequence.
    """
    known_stations = set(stations)
    grouped: dict[str, list[tuple[int, str, int, int]]] = {}
    seen_keys: set[tuple[str, int]] = set()
    for row in rows:
        trip_id = _required(row, "trip_id", "stop_times.txt")
        if trip_id not in trip_service:
            raise ValueError(
                f"stop_times.txt: unknown trip_id {trip_id!r}"
            )
        station = _required(row, "stop_id", "stop_times.txt")
        if station not in known_stations:
            raise ValueError(
                f"stop_times.txt: trip {trip_id!r} references unknown "
                f"stop_id {station!r}"
            )
        sequence = _parse_int(
            "stop_times.txt", f"trip {trip_id!r} stop_sequence",
            _required(row, "stop_sequence", "stop_times.txt"),
        )
        key = (trip_id, sequence)
        if key in seen_keys:
            raise ValueError(
                f"stop_times.txt: duplicate stop_sequence {sequence} for "
                f"trip {trip_id!r}"
            )
        seen_keys.add(key)
        arrival = _parse_time(
            "stop_times.txt", f"trip {trip_id!r} arrival_time",
            _required(row, "arrival_time", "stop_times.txt"),
        )
        departure = _parse_time(
            "stop_times.txt", f"trip {trip_id!r} departure_time",
            _required(row, "departure_time", "stop_times.txt"),
        )
        grouped.setdefault(trip_id, []).append((sequence, station, arrival, departure))

    templates: dict[str, list[tuple[str, int, int]]] = {}
    for trip_id, entries in grouped.items():
        entries.sort(key=lambda entry: entry[0])
        stops = [(station, arrival, departure)
                 for _, station, arrival, departure in entries]
        _check_time_sequence(trip_id, stops)
        templates[trip_id] = stops
    return templates


def _check_time_sequence(
    trip_id: str, stops: list[tuple[str, int, int]]
) -> None:
    for station, arrival, departure in stops:
        if arrival > departure:
            raise ValueError(
                f"trip {trip_id!r}: arrival ({arrival}) must not be later "
                f"than departure ({departure}) at stop {station!r}"
            )
    for (_, _, prev_departure), (station, arrival, _) in zip(stops, stops[1:]):
        if arrival <= prev_departure:
            raise ValueError(
                f"trip {trip_id!r}: arrival at stop {station!r} ({arrival}) "
                f"must be later than the departure at the previous stop "
                f"({prev_departure})"
            )


def _read_frequencies(
    rows: list[dict[str, str]],
    trip_service: dict[str, str],
) -> dict[str, tuple[int, int, int]]:
    """Parse frequencies into ``trip_id -> (start, end, headway_minutes)``."""
    frequencies: dict[str, tuple[int, int, int]] = {}
    for row in rows:
        trip_id = _required(row, "trip_id", "frequencies.txt")
        if trip_id not in trip_service:
            raise ValueError(f"frequencies.txt: unknown trip_id {trip_id!r}")
        if trip_id in frequencies:
            raise ValueError(
                f"frequencies.txt: duplicate row for trip {trip_id!r}"
            )
        exact_times = _required(row, "exact_times", "frequencies.txt")
        if exact_times != "1":
            raise ValueError(
                f"frequencies.txt: trip {trip_id!r}: only exact_times=1 is "
                f"supported, got {exact_times!r}"
            )
        headway_secs = _parse_int(
            "frequencies.txt", f"trip {trip_id!r} headway_secs",
            _required(row, "headway_secs", "frequencies.txt"),
        )
        if headway_secs == 0 or headway_secs % 60 != 0:
            raise ValueError(
                f"frequencies.txt: trip {trip_id!r}: headway_secs "
                f"{headway_secs} is not a positive whole number of minutes"
            )
        start = _parse_time(
            "frequencies.txt", f"trip {trip_id!r} start_time",
            _required(row, "start_time", "frequencies.txt"),
        )
        end = _parse_time(
            "frequencies.txt", f"trip {trip_id!r} end_time",
            _required(row, "end_time", "frequencies.txt"),
        )
        if start >= end:
            raise ValueError(
                f"frequencies.txt: trip {trip_id!r}: start_time must be "
                f"earlier than end_time"
            )
        frequencies[trip_id] = (start, end, headway_secs // 60)
    return frequencies
