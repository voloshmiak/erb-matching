"""Cost and ETA calculations."""

COST_PER_KM_EMPTY = 20  # UAH/km empty run (from task spec)
AVG_SPEED_KMPH = 40     # avg freight train speed, km/h


def empty_run_cost(distance_km: float) -> float:
    """Cost of empty run in UAH."""
    return distance_km * COST_PER_KM_EMPTY


def estimated_hours(distance_km: float) -> float:
    """ETA in hours for empty run."""
    return distance_km / AVG_SPEED_KMPH
