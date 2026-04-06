"""Naive baseline matcher: first-fit by wagon type, no distance optimization."""
from matching.models import OrderIn, WagonIn, Assignment, UnmatchedOrder
from matching.graph import reconstruct_path
from matching.cost import empty_run_cost, estimated_hours


def naive_match(
    orders: list[OrderIn],
    wagons: list[WagonIn],
    dist_matrix: dict[str, dict[str, float]],
    prev_matrix: dict[str, dict[str, str | None]],
) -> tuple[list[Assignment], list[UnmatchedOrder]]:
    """Assign first available wagon of matching type. No distance optimization."""
    available = list(wagons)
    assignments: list[Assignment] = []
    unmatched: list[UnmatchedOrder] = []

    for order in orders:
        candidates = [w for w in available if w.wagon_type == order.wagon_type]
        assigned_count = 0

        for _ in range(order.quantity):
            if not candidates:
                break
            wagon = candidates.pop(0)
            available.remove(wagon)

            dist = dist_matrix.get(wagon.current_station_id, {}).get(
                order.station_to_id, float("inf")
            )
            if dist == float("inf"):
                continue

            prev = prev_matrix[wagon.current_station_id]
            route = reconstruct_path(prev, order.station_to_id)

            assignments.append(
                Assignment(
                    order_id=order.order_id,
                    wagon_id=wagon.wagon_id,
                    wagon_number=wagon.wagon_number,
                    route=route,
                    empty_run_km=round(dist, 1),
                    cost_empty_run=round(empty_run_cost(dist), 1),
                    estimated_hours=round(estimated_hours(dist), 1),
                )
            )
            assigned_count += 1

        remaining = order.quantity - assigned_count
        if remaining > 0:
            unmatched.append(
                UnmatchedOrder(
                    order_id=order.order_id,
                    reason="no_available_wagons_of_type",
                )
            )

    return assignments, unmatched
