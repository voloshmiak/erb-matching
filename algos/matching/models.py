"""Pydantic models for /api/match request and response."""
from pydantic import BaseModel


class OrderIn(BaseModel):
    order_id: str
    station_to_id: str
    wagon_type: str
    quantity: int
    desired_date: str
    desired_date_hour: int = 0
    cargo: str = ""


class WagonIn(BaseModel):
    wagon_id: str
    wagon_number: str
    wagon_type: str
    current_station_id: str
    idle_days: float


class StationIn(BaseModel):
    station_id: str
    name: str
    type: str
    lat: float
    lng: float
    role: str = ""
    cargo: list[str] = []


class EdgeIn(BaseModel):
    from_station_id: str
    to_station_id: str
    distance_km: float


class MatchRequest(BaseModel):
    orders: list[OrderIn]
    wagons: list[WagonIn]
    stations: list[StationIn]
    edges: list[EdgeIn]


class Assignment(BaseModel):
    order_id: str
    wagon_id: str
    wagon_number: str
    route: list[str]
    empty_run_km: float
    cost_empty_run: float
    estimated_hours: float


class UnmatchedOrder(BaseModel):
    order_id: str
    reason: str


class Metrics(BaseModel):
    total_empty_km: float
    avg_empty_run_km: float
    total_cost: float
    naive_total_cost: float
    cost_saved: float
    match_rate: float
    wagons_matched: int
    orders_matched: int
    orders_unmatched: int


class MatchResponse(BaseModel):
    assignments: list[Assignment]
    unmatched_orders: list[UnmatchedOrder]
    metrics: Metrics
