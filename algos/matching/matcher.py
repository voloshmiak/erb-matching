"""Matching with train economics + locomotive-aware dispatch.

Flow:
1. MIP (or scored greedy fallback) assigns wagon→order
2. Group assignments by (from_station, to_station) into train groups
3. Filter by min_train_size(distance)
4. If locos provided: assign nearest loco to each group, skip if too expensive
5. Return assignments + train_groups for backend to dispatch

Backward compatible: if no locomotives in request, skips loco filter.
"""
from collections import Counter, defaultdict

from matching.models import (
    MatchRequest,
    MatchResponse,
    Assignment,
    TrainGroup,
    UnmatchedOrder,
    Metrics,
)
from matching.graph import build_graph, all_pairs_shortest, reconstruct_path
from matching.cost import empty_run_cost, estimated_hours
from matching.scoring import compute_score, cost_per_wagon_km
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

AVG_SPEED = 40.0


def _min_train_size(dist_km: float) -> int:
    if dist_km < 30: return 1
    if dist_km < 300: return 3
    return 5


def _scored_greedy(orders, wagons, dist_matrix, prev_matrix,
                   stations_meta, weights):
    """Score all (wagon, order) pairs, assign greedily by best score."""
    w = weights or {}

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

    pairs.sort(key=lambda x: x[0])

    assigned_wagons = set()
    assigned_per_order = Counter()
    assignments = []

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


def _group_into_trains(assignments, wagons, orders, dist_matrix, locomotives):
    """Group assignments into train groups with locomotive assignment.

    Returns: (dispatched_assignments, train_groups, rejected_assignments)
    """
    # Build lookups
    wagon_map = {w.wagon_id: w for w in wagons}
    order_map = {o.order_id: o for o in orders}

    # Group assignments by (from_station, to_station)
    groups: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    for a in assignments:
        wg = wagon_map.get(a.wagon_id)
        order = order_map.get(a.order_id)
        if wg and order:
            groups[(wg.current_station_id, order.station_to_id)].append(a)

    # Sort groups by size descending — big trains first
    sorted_groups = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

    train_groups: list[TrainGroup] = []
    dispatched: list[Assignment] = []
    rejected_wagon_ids: set[str] = set()
    locos_used: set[str] = set()
    train_counter = 0

    for (src, dst), group in sorted_groups:
        dist = dist_matrix.get(src, {}).get(dst, 300.0)
        if len(group) < _min_train_size(dist):
            for a in group:
                rejected_wagon_ids.add(a.wagon_id)
            continue

        # Find nearest available locomotive
        loco_id = ""
        reposition_km = 0.0

        if locomotives:
            best_loco = None
            best_dist = float("inf")
            for loco in locomotives:
                if loco.loco_id in locos_used:
                    continue
                loco_to_src = dist_matrix.get(loco.current_station_id, {}).get(src, float("inf"))
                if loco_to_src < best_dist:
                    best_dist = loco_to_src
                    best_loco = loco

            if best_loco is None:
                # No loco available — reject this group
                for a in group:
                    rejected_wagon_ids.add(a.wagon_id)
                continue

            # Economic gate: skip if loco repositioning too expensive
            train_revenue = len(group) * dist * 30 * 0.3
            reposition_cost = best_dist * 20
            train_cost = len(group) * dist * cost_per_wagon_km(len(group))
            if reposition_cost > train_revenue - train_cost and best_dist > 50:
                for a in group:
                    rejected_wagon_ids.add(a.wagon_id)
                continue

            loco_id = best_loco.loco_id
            reposition_km = best_dist if best_dist < float("inf") else 0.0
            locos_used.add(loco_id)

        train_counter += 1
        batch = group[:50]  # max train size

        train_groups.append(TrainGroup(
            train_id=f"TRN-{train_counter:04d}",
            source_station_id=src,
            dest_station_id=dst,
            wagon_ids=[a.wagon_id for a in batch],
            loco_id=loco_id,
            loco_reposition_km=round(reposition_km, 1),
            distance_km=round(dist, 1),
            estimated_hours=round((reposition_km + dist) / AVG_SPEED, 1),
        ))
        dispatched.extend(batch)

    return dispatched, train_groups, rejected_wagon_ids


def match(request: MatchRequest) -> MatchResponse:
    """Main entry point: MIP/greedy → train grouping → loco dispatch."""
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

    # Step 1: MIP or scored greedy assignment
    try:
        from matching.mip_matcher import mip_match
        all_assignments, unmatched = mip_match(
            request.orders, request.wagons, dist_matrix, prev_matrix,
            stations_meta=stations_meta, current_hour=0, weights=TUNED_WEIGHTS,
        )
    except Exception:
        all_assignments, unmatched = _scored_greedy(
            request.orders, request.wagons, dist_matrix, prev_matrix,
            stations_meta, TUNED_WEIGHTS,
        )

    # Step 2: Group into trains + assign locos
    dispatched, train_groups, rejected = _group_into_trains(
        all_assignments, request.wagons, request.orders,
        dist_matrix, request.locomotives,
    )

    # Assignments that didn't make it into a train → unmatched
    for a in all_assignments:
        if a.wagon_id in rejected:
            # Check if order already in unmatched
            if not any(u.order_id == a.order_id for u in unmatched):
                unmatched.append(UnmatchedOrder(
                    order_id=a.order_id,
                    reason="below_train_minimum",
                ))

    # Use dispatched assignments (those that formed trains)
    assignments = dispatched if train_groups else all_assignments

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
        train_groups=train_groups,
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
