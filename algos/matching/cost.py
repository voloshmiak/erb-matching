"""Cost and ETA calculations."""

from matching.scoring import cost_per_wagon_km

COST_PER_KM_EMPTY = 20  # UAH/km empty run — flat rate from ТЗ (solo wagon)
AVG_SPEED_KMPH = 40     # avg freight train speed, km/h


def empty_run_cost(distance_km: float, n_wagons: int = 1) -> float:
    """Cost of empty run in UAH. Train-aware when n_wagons > 1."""
    return distance_km * cost_per_wagon_km(n_wagons)


def estimated_hours(distance_km: float) -> float:
    """ETA in hours for empty run."""
    return distance_km / AVG_SPEED_KMPH
