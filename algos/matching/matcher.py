"""Greedy matching algorithm: assign nearest wagon per order, sorted by urgency."""
from matching.models import (
    MatchRequest,
    MatchResponse,
    Assignment,
    UnmatchedOrder,
    Metrics,
)
from matching.graph import build_graph, all_pairs_shortest, reconstruct_path
from matching.cost import empty_run_cost, estimated_hours
from matching.naive import naive_match


def _greedy_match(
    orders, wagons, dist_matrix, prev_matrix
) -> tuple[list[Assignment], list[UnmatchedOrder]]:
    """Core greedy: sort orders by urgency, pick nearest wagons."""
    sorted_orders = sorted(orders, key=lambda o: o.desired_date)
    available = list(wagons)
    assignments: list[Assignment] = []
    unmatched: list[UnmatchedOrder] = []

    for order in sorted_orders:
        candidates = [w for w in available if w.wagon_type == order.wagon_type]

        if not candidates:
            unmatched.append(
                UnmatchedOrder(
                    order_id=order.order_id,
                    reason="no_available_wagons_of_type",
                )
            )
            continue

        # Rank candidates by distance to order station
        ranked = []
        for w in candidates:
            dist = dist_matrix.get(w.current_station_id, {}).get(
                order.station_to_id, float("inf")
            )
            ranked.append((dist, w))
        ranked.sort(key=lambda x: x[0])

        assigned_count = 0
        for dist, wagon in ranked:
            if assigned_count >= order.quantity:
                break
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
            available.remove(wagon)
            assigned_count += 1

        remaining = order.quantity - assigned_count
        if remaining > 0:
            unmatched.append(
                UnmatchedOrder(
                    order_id=order.order_id,
                    reason="insufficient_wagons_of_type",
                )
            )

    return assignments, unmatched


def match(request: MatchRequest) -> MatchResponse:
    """Main entry point. Returns optimized assignments + naive comparison."""
    # Build graph and compute all-pairs shortest paths
    adj = build_graph(request.edges)
    station_ids = [s.station_id for s in request.stations]
    dist_matrix, prev_matrix = all_pairs_shortest(adj, station_ids)

    # Optimized greedy matching
    assignments, unmatched = _greedy_match(
        request.orders, request.wagons, dist_matrix, prev_matrix
    )

    # Naive baseline (first-fit) for cost comparison
    naive_assignments, _ = naive_match(
        request.orders, request.wagons, dist_matrix, prev_matrix
    )
    naive_total_cost = sum(a.cost_empty_run for a in naive_assignments)

    # Metrics
    total_empty_km = sum(a.empty_run_km for a in assignments)
    total_cost = sum(a.cost_empty_run for a in assignments)
    wagons_matched = len(assignments)
    orders_matched = len({a.order_id for a in assignments})
    orders_unmatched = len(unmatched)
    total_orders = orders_matched + orders_unmatched

    return MatchResponse(
        assignments=assignments,
        unmatched_orders=unmatched,
        metrics=Metrics(
            total_empty_km=round(total_empty_km, 1),
            avg_empty_run_km=round(total_empty_km / max(wagons_matched, 1), 1),
            total_cost=round(total_cost, 1),
            naive_total_cost=round(naive_total_cost, 1),
            cost_saved=round(naive_total_cost - total_cost, 1),
            match_rate=round(orders_matched / max(total_orders, 1), 2),
            wagons_matched=wagons_matched,
            orders_matched=orders_matched,
            orders_unmatched=orders_unmatched,
        ),
    )
