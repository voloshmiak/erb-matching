"""Scored greedy matching with train economics.

Uses 8-component scoring function (w1-w8) to rank wagon-order pairs,
then assigns greedily by best score. No OR-Tools dependency — pure Python.
Train cost model: 18/n + 2 UAH/km per wagon (sub-linear).
DMMSY-SSSP routing when C library available.

The heavy optimization (MIP, Optuna, locomotives) runs in Rust.
This Python service handles real-time API calls from the Go backend.
"""
from collections import Counter

from matching.models import (
    MatchRequest,
    MatchResponse,
    Assignment,
    UnmatchedOrder,
    Metrics,
)
from matching.graph import build_graph, all_pairs_shortest, reconstruct_path
from matching.cost import empty_run_cost, estimated_hours
from matching.scoring import compute_score
from matching.naive import naive_match

# Optuna-tuned weights for train cost model (w1-w8)
TUNED_WEIGHTS = {
    "w1": 4.435,
    "w2": 38.72,
    "w2_mode": "threshold",
    "w3": 0.400,
    "w4": 96.58,
    "w4_mode": "quadratic",
    "w5": 0.086,
    "w6": 0.783,
    "w7": 0.3,
    "w8": 0.3,
}


def _scored_greedy(orders, wagons, dist_matrix, prev_matrix,
                   stations_meta, weights):
    """Score all (wagon, order) pairs, assign greedily by best score."""
    w = weights or {}

    # Pre-compute train sizes and station surplus for w7/w8
    idle_counts: Counter = Counter()
    for wg in wagons:
        idle_counts[(wg.current_station_id, wg.wagon_type)] += 1

    dest_demand: Counter = Counter()
    for o in orders:
        dest_demand[(o.station_to_id, o.wagon_type)] += o.quantity

    station_surplus = {}
    for wg in wagons:
        key = (wg.current_station_id, wg.wagon_type)
        if key not in station_surplus:
            info = stations_meta.get(wg.current_station_id, {})
            daily_need = info.get("avg_orders_per_day", 0) * 10
            station_surplus[key] = idle_counts[key] - daily_need

    # Score all pairs
    pairs = []
    for wagon in wagons:
        meta = stations_meta.get(wagon.current_station_id, {})
        wagon_role = meta.get("role", "")
        n_idle = idle_counts[(wagon.current_station_id, wagon.wagon_type)]
        w_surplus = station_surplus.get(
            (wagon.current_station_id, wagon.wagon_type), 0.0)

        for order in orders:
            if wagon.wagon_type != order.wagon_type:
                continue
            dist = dist_matrix.get(wagon.current_station_id, {}).get(
                order.station_to_id, float("inf"))
            if dist == float("inf"):
                continue

            n_demand = dest_demand[(order.station_to_id, order.wagon_type)]
            n_wagons = min(n_idle, n_demand)
            order_meta = stations_meta.get(order.station_to_id, {})
            order_demand = order_meta.get("avg_orders_per_day", 0.0)
            eta = dist / 40.0

            score = compute_score(
                empty_km=dist,
                idle_hours=wagon.idle_days * 24,
                loaded_km=0.0,
                eta_hours=eta,
                deadline_hour=getattr(order, "desired_date_hour", 9999),
                current_hour=0,
                wagon_station_role=wagon_role,
                order_station_role=order_meta.get("role", ""),
                wagon_type=wagon.wagon_type,
                cargo=getattr(order, "cargo", ""),
                weights=w,
                n_wagons=n_wagons,
                order_station_demand=order_demand,
                wagon_station_surplus=w_surplus,
            )
            if score == float("inf"):
                continue
            pairs.append((score, wagon, order, dist))

    # Sort by score (lower = better)
    pairs.sort(key=lambda x: x[0])

    # Greedy assign
    assigned_wagons = set()
    assigned_per_order = Counter()
    assignments = []
    unmatched_ids = set()

    for score, wagon, order, dist in pairs:
        if wagon.wagon_id in assigned_wagons:
            continue
        if assigned_per_order[order.order_id] >= order.quantity:
            continue

        prev = prev_matrix.get(wagon.current_station_id, {})
        route = reconstruct_path(prev, order.station_to_id)

        assignments.append(Assignment(
            order_id=order.order_id,
            wagon_id=wagon.wagon_id,
            wagon_number=wagon.wagon_number,
            route=route,
            empty_run_km=round(dist, 1),
            cost_empty_run=round(empty_run_cost(dist), 1),
            estimated_hours=round(estimated_hours(dist), 1),
        ))
        assigned_wagons.add(wagon.wagon_id)
        assigned_per_order[order.order_id] += 1

    unmatched = []
    for order in orders:
        if assigned_per_order[order.order_id] < order.quantity:
            unmatched.append(UnmatchedOrder(
                order_id=order.order_id,
                reason="insufficient_wagons" if assigned_per_order[order.order_id] > 0
                       else "no_available_wagons",
            ))

    return assignments, unmatched


def match(request: MatchRequest) -> MatchResponse:
    """Main entry point: scored greedy with train economics."""
    adj = build_graph(request.edges)
    station_ids = [s.station_id for s in request.stations]
    dist_matrix, prev_matrix = all_pairs_shortest(adj, station_ids)

    stations_meta = {}
    for s in request.stations:
        stations_meta[s.station_id] = {
            "role": getattr(s, "role", ""),
            "cargo": getattr(s, "cargo", []),
            "avg_orders_per_day": getattr(s, "avg_orders_per_day", 0),
        }

    # Try MIP first, fall back to scored greedy if OR-Tools unavailable
    try:
        from matching.mip_matcher import mip_match
        assignments, unmatched = mip_match(
            request.orders, request.wagons, dist_matrix, prev_matrix,
            stations_meta=stations_meta, current_hour=0, weights=TUNED_WEIGHTS,
        )
    except Exception:
        assignments, unmatched = _scored_greedy(
            request.orders, request.wagons, dist_matrix, prev_matrix,
            stations_meta, TUNED_WEIGHTS,
        )

    # Naive baseline
    naive_assignments, _ = naive_match(
        request.orders, request.wagons, dist_matrix, prev_matrix)
    naive_total_cost = sum(a.cost_empty_run for a in naive_assignments)

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
