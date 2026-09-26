"""Request/response schemas for the journey planner API.

All constraint violations raise pydantic ValidationError, which FastAPI
turns into an HTTP 422 response automatically.
"""

from __future__ import annotations

from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    PrivateAttr,
    field_validator,
    model_validator,
)

from .gtfs import convert_gtfs

MAX_MINUTE = 2879  # 48 hours in minutes


class Stop(BaseModel):
    """One call of a trip at a station."""

    model_config = ConfigDict(extra="forbid")

    station: str = Field(min_length=1)
    arrival: int = Field(ge=0, le=MAX_MINUTE)
    departure: int = Field(ge=0, le=MAX_MINUTE)

    @model_validator(mode="after")
    def arrival_not_after_departure(self) -> "Stop":
        if self.arrival > self.departure:
            raise ValueError(
                f"arrival ({self.arrival}) must not be later than "
                f"departure ({self.departure}) at the same stop"
            )
        return self


class Trip(BaseModel):
    """A fixed-schedule trip: an ordered list of 2-20 stops."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    stops: list[Stop] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def stops_move_forward_in_time(self) -> "Trip":
        for prev, curr in zip(self.stops, self.stops[1:]):
            if curr.arrival <= prev.departure:
                raise ValueError(
                    f"trip {self.id!r}: arrival at {curr.station!r} "
                    f"({curr.arrival}) must be later than the departure at "
                    f"the previous stop {prev.station!r} ({prev.departure})"
                )
        return self


class TripNetwork(BaseModel):
    """Common fields of every request: the fixed-schedule network itself."""

    model_config = ConfigDict(extra="forbid")

    stations: list[str] = Field(min_length=2, max_length=40)
    trips: list[Trip] = Field(min_length=1, max_length=100)

    @field_validator("stations")
    @classmethod
    def stations_unique_and_non_empty(cls, value: list[str]) -> list[str]:
        if any(not s for s in value):
            raise ValueError("stations must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("stations must be unique")
        return value

    @model_validator(mode="after")
    def trip_ids_unique_and_stations_known(self) -> "TripNetwork":
        ids = [t.id for t in self.trips]
        if len(set(ids)) != len(ids):
            raise ValueError("trip ids must be unique")
        known = set(self.stations)
        for trip in self.trips:
            for stop in trip.stops:
                if stop.station not in known:
                    raise ValueError(
                        f"trip {trip.id!r} references unknown station "
                        f"{stop.station!r}"
                    )
        return self

    @property
    def trip_index(self) -> dict[str, Trip]:
        return {trip.id: trip for trip in self.trips}


class JourneyRequest(TripNetwork):
    origin: str
    destination: str
    earliest_departure: int = Field(ge=0, le=MAX_MINUTE)
    transfer_buffer: int = Field(ge=1, le=60)

    @model_validator(mode="after")
    def terminals_valid(self) -> "JourneyRequest":
        known = set(self.stations)
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        if self.origin not in known:
            raise ValueError(f"origin {self.origin!r} is not one of the stations")
        if self.destination not in known:
            raise ValueError(
                f"destination {self.destination!r} is not one of the stations"
            )
        return self


class Segment(BaseModel):
    """One continuous ride on a single trip."""

    model_config = ConfigDict(populate_by_name=True)

    trip_id: str
    from_station: str = Field(alias="from")
    to_station: str = Field(alias="to")
    departure: int
    arrival: int


class JourneyResponse(BaseModel):
    reachable: bool
    arrival_time: Optional[int] = None
    transfers: int = 0
    trip_ids: list[str] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)


class Traveler(BaseModel):
    """One of the two travellers to bring together."""

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1)
    earliest_departure: int = Field(ge=0, le=MAX_MINUTE)


class MeetRequest(TripNetwork):
    """Plan a meeting of two travellers starting at different stations."""

    travelers: list[Traveler] = Field(min_length=2, max_length=2)
    transfer_buffer: int = Field(ge=1, le=60)
    max_wait: int = Field(ge=0, le=60)

    @model_validator(mode="after")
    def travelers_valid(self) -> "MeetRequest":
        first, second = self.travelers
        if first.origin == second.origin:
            raise ValueError("travelers must start at different origins")
        known = set(self.stations)
        for i, traveler in enumerate(self.travelers):
            if traveler.origin not in known:
                raise ValueError(
                    f"travelers[{i}] origin {traveler.origin!r} is not one "
                    "of the stations"
                )
        return self


class MeetTravelerResult(BaseModel):
    """One traveller's arrival at the meeting station."""

    arrival_time: Optional[int] = None
    transfers: int = 0
    trip_ids: list[str] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)


class MeetResponse(BaseModel):
    reachable: bool
    meeting_station: Optional[str] = None
    meeting_time: Optional[int] = None
    travelers: list[MeetTravelerResult] = Field(default_factory=list)


class Ride(BaseModel):
    """One ride of the itinerary to price: board and alight on one trip."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(min_length=1)
    board_index: int = Field(ge=0, description="stop index the passenger boards at (0-based)")
    alight_index: int = Field(ge=0, description="stop index the passenger alights at (0-based)")


def _validate_ride_chain(
    rides: list[Ride],
    trips: dict[str, Trip],
    transfer_buffer: int,
    context: str,
    require_buffered_schedule: bool = True,
) -> None:
    """Validate one ordered chain of rides against the trip network.

    ``context`` is the error-message subject: ``"ride"`` for a bare ride
    list, ``"booking 'G1' ride"`` for the rides inside one booking.
    When ``require_buffered_schedule`` is false the *scheduled* transfer
    buffer is not enforced: the reliability endpoint scores actual
    (delayed) times instead of rejecting nominally tight chains.
    Raises ValueError (turned into HTTP 422) on the first violation.
    """
    for i, ride in enumerate(rides):
        trip = trips.get(ride.trip_id)
        if trip is None:
            raise ValueError(
                f"{context} {i}: trip_id {ride.trip_id!r} does not exist"
            )
        last = len(trip.stops) - 1
        if ride.board_index >= last:
            raise ValueError(
                f"{context} {i}: board_index {ride.board_index} is not a "
                f"boarding stop of trip {ride.trip_id!r} "
                f"(must be between 0 and {last - 1})"
            )
        if ride.alight_index > last:
            raise ValueError(
                f"{context} {i}: alight_index {ride.alight_index} exceeds the "
                f"last stop index {last} of trip {ride.trip_id!r}"
            )
        if ride.board_index >= ride.alight_index:
            raise ValueError(
                f"{context} {i}: alight_index {ride.alight_index} must be "
                f"greater than board_index {ride.board_index}"
            )
        if i > 0:
            prev = rides[i - 1]
            prev_trip = trips[prev.trip_id]
            if prev.trip_id == ride.trip_id:
                raise ValueError(
                    f"{context} {i}: adjacent rides must use different trips "
                    f"(both use {ride.trip_id!r})"
                )
            alight_station = prev_trip.stops[prev.alight_index].station
            board_station = trip.stops[ride.board_index].station
            if alight_station != board_station:
                raise ValueError(
                    f"{context} {i}: must board at the previous alighting "
                    f"station {alight_station!r}, not {board_station!r}"
                )
            if not require_buffered_schedule:
                continue
            arrival = prev_trip.stops[prev.alight_index].arrival
            departure = trip.stops[ride.board_index].departure
            if departure < arrival + transfer_buffer:
                raise ValueError(
                    f"{context} {i}: departure at minute {departure} is before "
                    f"arrival {arrival} + transfer_buffer {transfer_buffer}"
                )


class Ticket(BaseModel):
    """A purchasable ticket type."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    price_cents: int = Field(ge=0)
    valid_minutes: int = Field(ge=0, le=MAX_MINUTE)
    max_rides: int = Field(ge=1, le=20)


class FareQuoteRequest(TripNetwork):
    rides: list[Ride] = Field(min_length=1, max_length=20)
    transfer_buffer: int = Field(ge=1, le=60)
    tickets: list[Ticket] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def rides_and_tickets_valid(self) -> "FareQuoteRequest":
        ticket_ids = [ticket.id for ticket in self.tickets]
        if len(set(ticket_ids)) != len(ticket_ids):
            raise ValueError("ticket ids must be unique")
        _validate_ride_chain(
            self.rides, self.trip_index, self.transfer_buffer, "ride"
        )
        return self

    def ride_departures(self) -> list[int]:
        """Departure minute of each ride (at its boarding stop)."""
        trips = self.trip_index
        return [
            trips[ride.trip_id].stops[ride.board_index].departure
            for ride in self.rides
        ]


class FareTicketEntry(BaseModel):
    """One purchased ticket and the consecutive rides it covers."""

    ticket_id: str
    covers_from: int = Field(ge=0, description="index of the first covered ride (0-based, inclusive)")
    covers_to: int = Field(ge=0, description="index of the last covered ride (0-based, inclusive)")


class FareQuoteResponse(BaseModel):
    total_cents: int
    tickets: list[FareTicketEntry]


class FleetAssignRequest(TripNetwork):
    """Minimum-fleet assignment over the whole fixed-schedule network."""

    turnaround_minutes: int = Field(
        ge=0,
        le=60,
        description=(
            "minimum minutes between a vehicle's last-stop arrival and the "
            "first-stop departure of the next trip it serves"
        ),
    )


class VehicleRun(BaseModel):
    """One vehicle's assignment: its trips in execution order."""

    trip_ids: list[str] = Field(min_length=1)


class FleetAssignResponse(BaseModel):
    vehicle_count: int
    vehicles: list[VehicleRun]


class Charger(BaseModel):
    """One station where the vehicle may charge between two trips."""

    model_config = ConfigDict(extra="forbid")

    station: str = Field(min_length=1)
    rate: int = Field(
        ge=1, le=60, description="charge units gained per stopped minute"
    )
    price_cents: int = Field(ge=0, description="cents per charged unit")


class FleetChargeRequest(TripNetwork):
    """Minimum-cost charging plan for one vehicle's fixed trip chain.

    ``trip_ids`` is the chain in execution order; ``consumptions`` maps
    every chained trip to the battery units it burns.  Charging happens
    only during the stop between two consecutive trips and only at a
    station listed in ``chargers``.
    """

    trip_ids: list[str] = Field(min_length=1, max_length=12)
    consumptions: dict[str, Annotated[int, Field(ge=1, le=60)]] = Field(
        description=(
            "per-trip battery consumption; keys must be exactly the "
            "chained trip ids"
        )
    )
    capacity: int = Field(ge=1, le=60)
    initial_charge: int = Field(ge=0)
    chargers: list[Charger] = Field(max_length=40)

    @field_validator("trip_ids")
    @classmethod
    def trip_ids_distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("trip_ids must be distinct")
        return value

    @model_validator(mode="after")
    def chain_and_chargers_valid(self) -> "FleetChargeRequest":
        trips = self.trip_index
        for i, trip_id in enumerate(self.trip_ids):
            if trip_id not in trips:
                raise ValueError(
                    f"trip_ids {i}: trip_id {trip_id!r} does not exist"
                )
        chained = set(self.trip_ids)
        missing = [t for t in self.trip_ids if t not in self.consumptions]
        if missing:
            raise ValueError(f"consumptions is missing trip ids: {missing}")
        unknown = [key for key in self.consumptions if key not in chained]
        if unknown:
            raise ValueError(
                f"consumptions references trip ids outside the chain: "
                f"{unknown}"
            )
        if self.initial_charge > self.capacity:
            raise ValueError(
                f"initial_charge ({self.initial_charge}) must not exceed "
                f"capacity ({self.capacity})"
            )
        known = set(self.stations)
        seen: set[str] = set()
        for charger in self.chargers:
            if charger.station in seen:
                raise ValueError(
                    f"charger station {charger.station!r} is duplicated"
                )
            seen.add(charger.station)
            if charger.station not in known:
                raise ValueError(
                    f"charger station {charger.station!r} is not one of "
                    "the stations"
                )
        for i in range(len(self.trip_ids) - 1):
            prev = trips[self.trip_ids[i]]
            curr = trips[self.trip_ids[i + 1]]
            end = prev.stops[-1]
            start = curr.stops[0]
            if end.station != start.station:
                raise ValueError(
                    f"trip_ids {i + 1}: trip {curr.id!r} starts at "
                    f"{start.station!r}, but the previous trip {prev.id!r} "
                    f"ends at {end.station!r}"
                )
            if start.departure < end.arrival:
                raise ValueError(
                    f"trip_ids {i + 1}: trip {curr.id!r} departs at minute "
                    f"{start.departure}, before the previous trip "
                    f"{prev.id!r} arrives at {end.arrival}"
                )
        return self


class FleetChargeResponse(BaseModel):
    feasible: bool
    total_cents: Optional[int] = None
    charges: list[int] = Field(default_factory=list)


class Workshop(BaseModel):
    """One station where the vehicle may be inspected between two trips."""

    model_config = ConfigDict(extra="forbid")

    station: str = Field(min_length=1)
    open: int = Field(
        ge=0, le=MAX_MINUTE, description="first open minute of the workshop"
    )
    close: int = Field(
        ge=0,
        le=MAX_MINUTE,
        description="the workshop closes at this minute (exclusive)",
    )
    minutes: int = Field(
        ge=1, le=120, description="duration of one inspection in minutes"
    )
    price_cents: int = Field(
        ge=0, description="non-negative price of one inspection"
    )

    @model_validator(mode="after")
    def open_before_close(self) -> "Workshop":
        if self.open >= self.close:
            raise ValueError(
                f"open ({self.open}) must be earlier than close "
                f"({self.close})"
            )
        return self


class FleetInspectRequest(TripNetwork):
    """Minimum-inspection maintenance plan for one vehicle's fixed trip chain.

    ``trip_ids`` is the chain in execution order; ``trip_km`` maps every
    chained trip to the kilometres it adds to the accumulated mileage.
    After every trip the accumulated mileage must stay within
    ``limit_km``; an inspection — only possible during the stop between
    two consecutive trips, at a station with a workshop whose open
    interval fits the whole work duration — resets it to zero.
    """

    trip_ids: list[str] = Field(min_length=1, max_length=40)
    trip_km: dict[str, Annotated[int, Field(ge=1, le=300)]] = Field(
        description=(
            "per-trip mileage; keys must be exactly the chained trip ids"
        )
    )
    limit_km: int = Field(ge=1, le=300)
    initial_km: int = Field(ge=0)
    workshops: list[Workshop] = Field(max_length=40)

    @field_validator("trip_ids")
    @classmethod
    def trip_ids_distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("trip_ids must be distinct")
        return value

    @model_validator(mode="after")
    def chain_and_workshops_valid(self) -> "FleetInspectRequest":
        trips = self.trip_index
        for i, trip_id in enumerate(self.trip_ids):
            if trip_id not in trips:
                raise ValueError(
                    f"trip_ids {i}: trip_id {trip_id!r} does not exist"
                )
        chained = set(self.trip_ids)
        missing = [t for t in self.trip_ids if t not in self.trip_km]
        if missing:
            raise ValueError(f"trip_km is missing trip ids: {missing}")
        unknown = [key for key in self.trip_km if key not in chained]
        if unknown:
            raise ValueError(
                f"trip_km references trip ids outside the chain: {unknown}"
            )
        if self.initial_km > self.limit_km:
            raise ValueError(
                f"initial_km ({self.initial_km}) must not exceed "
                f"limit_km ({self.limit_km})"
            )
        known = set(self.stations)
        seen: set[str] = set()
        for workshop in self.workshops:
            if workshop.station in seen:
                raise ValueError(
                    f"workshop station {workshop.station!r} is duplicated"
                )
            seen.add(workshop.station)
            if workshop.station not in known:
                raise ValueError(
                    f"workshop station {workshop.station!r} is not one of "
                    "the stations"
                )
        for i in range(len(self.trip_ids) - 1):
            prev = trips[self.trip_ids[i]]
            curr = trips[self.trip_ids[i + 1]]
            end = prev.stops[-1]
            start = curr.stops[0]
            if end.station != start.station:
                raise ValueError(
                    f"trip_ids {i + 1}: trip {curr.id!r} starts at "
                    f"{start.station!r}, but the previous trip {prev.id!r} "
                    f"ends at {end.station!r}"
                )
            if start.departure < end.arrival:
                raise ValueError(
                    f"trip_ids {i + 1}: trip {curr.id!r} departs at minute "
                    f"{start.departure}, before the previous trip "
                    f"{prev.id!r} arrives at {end.arrival}"
                )
        return self


class InspectionEntry(BaseModel):
    """One scheduled inspection at the stop between two chained trips."""

    index: int = Field(
        ge=0,
        description=(
            "0-based connection index: the inspection happens between "
            "trip_ids[index] and trip_ids[index + 1]"
        ),
    )
    station: str


class FleetInspectResponse(BaseModel):
    feasible: bool
    inspections: list[InspectionEntry] = Field(default_factory=list)
    total_cents: Optional[int] = None


class Booking(BaseModel):
    """One group booking: a fixed ride chain for a party of passengers."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    passengers: int = Field(ge=1)
    rides: list[Ride] = Field(min_length=1, max_length=3)


class SeatAllocateRequest(TripNetwork):
    """Group-booking seat allocation over the fixed-schedule network."""

    capacity: dict[str, PositiveInt] = Field(
        description="per-trip seat capacity; must cover every trip id exactly"
    )
    transfer_buffer: int = Field(ge=1, le=60)
    bookings: list[Booking] = Field(min_length=1, max_length=18)

    @model_validator(mode="after")
    def capacity_and_bookings_valid(self) -> "SeatAllocateRequest":
        trips = self.trip_index
        missing = [trip_id for trip_id in trips if trip_id not in self.capacity]
        if missing:
            raise ValueError(f"capacity is missing trip ids: {missing}")
        unknown = [key for key in self.capacity if key not in trips]
        if unknown:
            raise ValueError(f"capacity references unknown trip ids: {unknown}")

        ids = [booking.id for booking in self.bookings]
        if len(set(ids)) != len(ids):
            raise ValueError("booking ids must be unique")
        for booking in self.bookings:
            _validate_ride_chain(
                booking.rides,
                trips,
                self.transfer_buffer,
                f"booking {booking.id!r} ride",
            )
        return self


class SeatAllocateResponse(BaseModel):
    accepted_ids: list[str]
    rejected_ids: list[str]
    accepted_passengers: int


class DisruptionRequest(TripNetwork):
    """Minimum trip cancellations breaking on-time origin-destination travel."""

    trips: list[Trip] = Field(min_length=1, max_length=16)
    origin: str
    destination: str
    earliest_departure: int = Field(ge=0, le=MAX_MINUTE)
    arrival_deadline: int = Field(ge=0, le=MAX_MINUTE)
    transfer_buffer: int = Field(ge=1, le=60)

    @model_validator(mode="after")
    def terminals_and_window_valid(self) -> "DisruptionRequest":
        known = set(self.stations)
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        if self.origin not in known:
            raise ValueError(f"origin {self.origin!r} is not one of the stations")
        if self.destination not in known:
            raise ValueError(
                f"destination {self.destination!r} is not one of the stations"
            )
        if self.arrival_deadline < self.earliest_departure:
            raise ValueError(
                f"arrival_deadline ({self.arrival_deadline}) must not be "
                f"earlier than earliest_departure ({self.earliest_departure})"
            )
        return self


class DisruptionResponse(BaseModel):
    baseline_reachable: bool
    minimum_cancellations: int
    cancelled_ids: list[str] = Field(default_factory=list)


class DelayOutcome(BaseModel):
    """One possible delay of a trip and its probability weight."""

    model_config = ConfigDict(extra="forbid")

    delay_minutes: int = Field(ge=0, le=60)
    weight: int = Field(ge=1, description="positive integer probability weight")


class DelayProfile(BaseModel):
    """Discrete delay distribution of one ridden trip."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(min_length=1)
    delays: list[DelayOutcome] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def delays_distinct_and_weights_sum_to_100(self) -> "DelayProfile":
        values = [outcome.delay_minutes for outcome in self.delays]
        if len(set(values)) != len(values):
            raise ValueError(
                f"delay profile for trip {self.trip_id!r}: "
                "delay_minutes values must be distinct"
            )
        total = sum(outcome.weight for outcome in self.delays)
        if total != 100:
            raise ValueError(
                f"delay profile for trip {self.trip_id!r}: "
                f"weights must sum to 100, got {total}"
            )
        return self


class ReliabilityRequest(TripNetwork):
    """On-time completion probability of a fixed ride chain under delays."""

    rides: list[Ride] = Field(min_length=1, max_length=12)
    transfer_buffer: int = Field(ge=1, le=60)
    arrival_deadline: int = Field(ge=0, le=MAX_MINUTE)
    delay_profiles: list[DelayProfile] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def rides_and_profiles_valid(self) -> "ReliabilityRequest":
        ids = [ride.trip_id for ride in self.rides]
        if len(set(ids)) != len(ids):
            raise ValueError("ride trip ids must be distinct")
        # Structural checks only: the scheduled transfer buffer is *not*
        # enforced here — delays decide which transfers actually work.
        _validate_ride_chain(
            self.rides,
            self.trip_index,
            self.transfer_buffer,
            "ride",
            require_buffered_schedule=False,
        )
        profile_ids = [profile.trip_id for profile in self.delay_profiles]
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("delay profile trip ids must be unique")
        if set(profile_ids) != set(ids):
            raise ValueError(
                "delay profiles must cover exactly the ridden trips "
                f"{sorted(set(ids))}, got {sorted(set(profile_ids))}"
            )
        return self


class ReliabilityResponse(BaseModel):
    success_numerator: str
    success_denominator: str


class GtfsRequest(BaseModel):
    """GTFS-subset conversion request: one service date plus CSV file texts.

    The whole batch is validated and converted on construction; any
    violation raises ValueError (turned into HTTP 422), so ``network()``
    always returns a network that satisfies the shared limits.
    """

    model_config = ConfigDict(extra="forbid")

    date: str = Field(description="service date in YYYYMMDD form")
    files: dict[str, str] = Field(
        min_length=1,
        description=(
            "GTFS file name -> CSV text; requires stops.txt, trips.txt, "
            "stop_times.txt and calendar.txt, plus optional "
            "calendar_dates.txt and frequencies.txt"
        ),
    )

    _network: dict = PrivateAttr()

    @model_validator(mode="after")
    def gtfs_valid_and_converted(self) -> "GtfsRequest":
        self._network = convert_gtfs(date_text=self.date, files=self.files)
        return self

    def network(self) -> dict:
        """The converted ``{"stations", "trips"}`` network for the date."""
        return self._network


class GtfsResponse(BaseModel):
    stations: list[str]
    trips: list[Trip]


class DemandBatch(BaseModel):
    """One group of passengers arriving at the first station together."""

    model_config = ConfigDict(extra="forbid")

    arrival: int = Field(ge=0, le=MAX_MINUTE)
    passengers: int = Field(ge=1, description="positive integer group size")


class DesignRequest(BaseModel):
    """Demand-driven timetable design over a single line.

    ``stops`` is the shared stop pattern of every trip as offsets relative
    to the first-station departure; ``candidates`` are the headway minutes
    a trip may start at.  Exactly ``trip_count`` of them must be chosen so
    that adjacent departures keep ``min_interval`` and every demand batch
    boards within ``max_wait``.
    """

    model_config = ConfigDict(extra="forbid")

    stops: list[Stop] = Field(min_length=2, max_length=20)
    candidates: list[int] = Field(min_length=1, max_length=60)
    demand: list[DemandBatch] = Field(min_length=1, max_length=60)
    trip_count: int = Field(ge=1, le=12)
    min_interval: int = Field(ge=0, le=120)
    max_wait: int = Field(ge=0, le=120)

    @field_validator("candidates")
    @classmethod
    def candidates_in_range_and_distinct(cls, value: list[int]) -> list[int]:
        for minute in value:
            if not 0 <= minute <= MAX_MINUTE:
                raise ValueError(
                    f"candidate minute {minute} is outside 0..{MAX_MINUTE}"
                )
        if len(set(value)) != len(value):
            raise ValueError("candidates must be distinct")
        return value

    @model_validator(mode="after")
    def stops_and_candidates_valid(self) -> "DesignRequest":
        stations = [stop.station for stop in self.stops]
        if len(set(stations)) != len(stations):
            raise ValueError("stop stations must be unique")
        first = self.stops[0]
        if first.arrival != 0 or first.departure != 0:
            raise ValueError("the first stop's offsets must be 0")
        for prev, curr in zip(self.stops, self.stops[1:]):
            if curr.arrival <= prev.departure:
                raise ValueError(
                    f"arrival offset at {curr.station!r} ({curr.arrival}) "
                    f"must be later than the departure offset at the "
                    f"previous stop {prev.station!r} ({prev.departure})"
                )
        max_offset = self.stops[-1].departure
        for minute in self.candidates:
            if minute + max_offset > MAX_MINUTE:
                raise ValueError(
                    f"candidate minute {minute} plus the maximum offset "
                    f"{max_offset} exceeds {MAX_MINUTE}"
                )
        return self


class DesignResponse(BaseModel):
    feasible: bool
    departures: list[int] = Field(default_factory=list)
    total_wait: Optional[int] = None
    stations: list[str] = Field(default_factory=list)
    trips: list[Trip] = Field(default_factory=list)


class OutgoingTrip(BaseModel):
    """One outgoing departure at the transfer station that may be held."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(min_length=1)
    stop_index: int = Field(
        ge=0, description="stop index of the transfer station (0-based)"
    )
    onboard: int = Field(
        ge=0, description="non-negative passengers already on board"
    )


class TransferGroup(BaseModel):
    """One group arriving at the station, hoping to board its target trip."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    actual_arrival: int = Field(ge=0, le=MAX_MINUTE)
    trip_id: str = Field(min_length=1)
    passengers: int = Field(ge=1, description="positive integer group size")


class HoldRequest(TripNetwork):
    """Departure holding at one transfer station.

    ``outgoing`` lists the departures at ``station`` that may be held, in
    ascending departure order; each hold delays the departure at the
    station by 0..``max_hold`` minutes and shifts every later stop of the
    trip by the same amount (all shifted times must stay within 0..2879).
    The original departures must already keep ``min_interval``, and the
    adjusted ones must keep it too.  A group connects to its target trip
    when ``actual_arrival + transfer_buffer`` does not exceed the adjusted
    departure.
    """

    station: str
    outgoing: list[OutgoingTrip] = Field(min_length=1, max_length=10)
    groups: list[TransferGroup] = Field(min_length=1, max_length=30)
    transfer_buffer: int = Field(ge=1, le=60)
    max_hold: int = Field(ge=0, le=30)
    min_interval: int = Field(ge=0, le=60)

    @model_validator(mode="after")
    def outgoing_and_groups_valid(self) -> "HoldRequest":
        if self.station not in set(self.stations):
            raise ValueError(
                f"station {self.station!r} is not one of the stations"
            )
        trips = self.trip_index
        ids = [entry.trip_id for entry in self.outgoing]
        if len(set(ids)) != len(ids):
            raise ValueError("outgoing trip ids must be unique")
        departures = []
        for i, entry in enumerate(self.outgoing):
            trip = trips.get(entry.trip_id)
            if trip is None:
                raise ValueError(
                    f"outgoing {i}: trip_id {entry.trip_id!r} does not exist"
                )
            last = len(trip.stops) - 1
            if entry.stop_index > last:
                raise ValueError(
                    f"outgoing {i}: stop_index {entry.stop_index} exceeds "
                    f"the last stop index {last} of trip {entry.trip_id!r}"
                )
            stop = trip.stops[entry.stop_index]
            if stop.station != self.station:
                raise ValueError(
                    f"outgoing {i}: stop {entry.stop_index} of trip "
                    f"{entry.trip_id!r} is at {stop.station!r}, not at "
                    f"{self.station!r}"
                )
            departures.append(stop.departure)
        for i in range(1, len(departures)):
            if departures[i] < departures[i - 1]:
                raise ValueError(
                    "outgoing must be sorted by departure at the station: "
                    f"entry {i} departs at {departures[i]}, before "
                    f"{departures[i - 1]}"
                )
            if departures[i] - departures[i - 1] < self.min_interval:
                raise ValueError(
                    f"original departures {departures[i - 1]} and "
                    f"{departures[i]} are closer than min_interval "
                    f"{self.min_interval}"
                )
        group_ids = [group.id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("group ids must be unique")
        targets = set(ids)
        for group in self.groups:
            if group.trip_id not in targets:
                raise ValueError(
                    f"group {group.id!r}: target trip_id {group.trip_id!r} "
                    "is not one of the outgoing trips"
                )
        return self


class HoldEntry(BaseModel):
    """The chosen hold of one outgoing trip, in outgoing order."""

    trip_id: str
    delay_minutes: int


class HoldResponse(BaseModel):
    holds: list[HoldEntry]
    connected_ids: list[str] = Field(default_factory=list)
    connected_passengers: int
    onboard_delay_minutes: int
