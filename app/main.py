"""Offline fixed-schedule journey planning API."""

from __future__ import annotations

from fastapi import FastAPI

from .charge import plan_charge
from .design import design_timetable
from .disruption import min_disruption
from .fares import quote_fares
from .fleet import assign_fleet
from .hold import plan_hold
from .meet import plan_meet
from .models import (
    DesignRequest,
    DesignResponse,
    DisruptionRequest,
    DisruptionResponse,
    FareQuoteRequest,
    FareQuoteResponse,
    FleetAssignRequest,
    FleetAssignResponse,
    FleetChargeRequest,
    FleetChargeResponse,
    GtfsRequest,
    GtfsResponse,
    HoldRequest,
    HoldResponse,
    JourneyRequest,
    JourneyResponse,
    MeetRequest,
    MeetResponse,
    ReliabilityRequest,
    ReliabilityResponse,
    SeatAllocateRequest,
    SeatAllocateResponse,
)
from .planner import plan_journey
from .reliability import journey_reliability
from .seats import allocate_seats

app = FastAPI(
    title="Fixed-Schedule Journey Planner",
    version="1.0.0",
    description=(
        "Offline earliest-arrival journey planning over fixed-schedule "
        "trips with transfer buffers, plus GTFS-subset timetable "
        "conversion, two-traveller meeting planning, minimum-fare ticket "
        "quoting, minimum-fleet vehicle assignment, minimum-cost "
        "charging of one electric vehicle along a fixed trip chain, "
        "group-booking seat "
        "allocation, minimum-cancellation "
        "network disruption analysis, on-time reliability scoring of "
        "a fixed itinerary under random delays and demand-driven "
        "timetable design over a single line, plus transfer-station "
        "departure holding that trades onboard delay against "
        "connecting passengers."
    ),
)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "journey-planner",
        "docs": "/docs",
        "endpoints": [
            "POST /v1/journey",
            "POST /v1/journey/meet",
            "POST /v1/journey/reliability",
            "POST /v1/fares/quote",
            "POST /v1/fleet/assign",
            "POST /v1/fleet/charge",
            "POST /v1/seats/allocate",
            "POST /v1/network/disruption",
            "POST /v1/gtfs",
            "POST /v1/timetable/design",
            "POST /v1/transfers/hold",
        ],
    }


@app.post("/v1/journey", response_model=JourneyResponse)
def journey(request: JourneyRequest) -> JourneyResponse:
    result = plan_journey(
        trips=request.trips,
        origin=request.origin,
        destination=request.destination,
        earliest_departure=request.earliest_departure,
        transfer_buffer=request.transfer_buffer,
    )
    return JourneyResponse(**result)


@app.post("/v1/journey/meet", response_model=MeetResponse)
def journey_meet(request: MeetRequest) -> MeetResponse:
    result = plan_meet(
        trips=request.trips,
        travelers=[
            (traveler.origin, traveler.earliest_departure)
            for traveler in request.travelers
        ],
        transfer_buffer=request.transfer_buffer,
        max_wait=request.max_wait,
    )
    return MeetResponse(**result)


@app.post("/v1/journey/reliability", response_model=ReliabilityResponse)
def reliability(request: ReliabilityRequest) -> ReliabilityResponse:
    trips = request.trip_index
    profiles = {profile.trip_id: profile for profile in request.delay_profiles}
    legs = []
    for ride in request.rides:
        trip = trips[ride.trip_id]
        legs.append(
            (
                trip.stops[ride.board_index].departure,
                trip.stops[ride.alight_index].arrival,
                [
                    (outcome.delay_minutes, outcome.weight)
                    for outcome in profiles[ride.trip_id].delays
                ],
            )
        )
    result = journey_reliability(
        legs=legs,
        transfer_buffer=request.transfer_buffer,
        arrival_deadline=request.arrival_deadline,
    )
    return ReliabilityResponse(**result)


@app.post("/v1/fares/quote", response_model=FareQuoteResponse)
def fares_quote(request: FareQuoteRequest) -> FareQuoteResponse:
    result = quote_fares(
        rides=request.rides,
        departures=request.ride_departures(),
        tickets=request.tickets,
    )
    return FareQuoteResponse(**result)


@app.post("/v1/fleet/assign", response_model=FleetAssignResponse)
def fleet_assign(request: FleetAssignRequest) -> FleetAssignResponse:
    result = assign_fleet(
        trips=request.trips,
        turnaround_minutes=request.turnaround_minutes,
    )
    return FleetAssignResponse(**result)


@app.post("/v1/fleet/charge", response_model=FleetChargeResponse)
def fleet_charge(request: FleetChargeRequest) -> FleetChargeResponse:
    result = plan_charge(
        trips=request.trips,
        trip_ids=request.trip_ids,
        consumptions=request.consumptions,
        capacity=request.capacity,
        initial_charge=request.initial_charge,
        chargers=request.chargers,
    )
    return FleetChargeResponse(**result)


@app.post("/v1/seats/allocate", response_model=SeatAllocateResponse)
def seats_allocate(request: SeatAllocateRequest) -> SeatAllocateResponse:
    result = allocate_seats(
        trips=request.trips,
        capacity=request.capacity,
        bookings=request.bookings,
    )
    return SeatAllocateResponse(**result)


@app.post("/v1/network/disruption", response_model=DisruptionResponse)
def network_disruption(request: DisruptionRequest) -> DisruptionResponse:
    result = min_disruption(
        trips=request.trips,
        origin=request.origin,
        destination=request.destination,
        earliest_departure=request.earliest_departure,
        arrival_deadline=request.arrival_deadline,
        transfer_buffer=request.transfer_buffer,
    )
    return DisruptionResponse(**result)


@app.post("/v1/gtfs", response_model=GtfsResponse)
def gtfs_convert(request: GtfsRequest) -> GtfsResponse:
    return GtfsResponse(**request.network())


@app.post("/v1/timetable/design", response_model=DesignResponse)
def timetable_design(request: DesignRequest) -> DesignResponse:
    result = design_timetable(
        stops=request.stops,
        candidates=request.candidates,
        demand=request.demand,
        trip_count=request.trip_count,
        min_interval=request.min_interval,
        max_wait=request.max_wait,
    )
    return DesignResponse(**result)


@app.post("/v1/transfers/hold", response_model=HoldResponse)
def transfers_hold(request: HoldRequest) -> HoldResponse:
    result = plan_hold(
        trips=request.trips,
        outgoing=request.outgoing,
        groups=request.groups,
        transfer_buffer=request.transfer_buffer,
        max_hold=request.max_hold,
        min_interval=request.min_interval,
    )
    return HoldResponse(**result)
