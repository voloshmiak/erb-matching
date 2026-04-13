"""MIP-based matching: globally optimal wagon-order assignment via OR-Tools.

Replaces greedy with declarative optimization. Same input/output format.
Uses SCIP solver (included with ortools). Scoring from scoring.py.
"""
from __future__ import annotations

from ortools.linear_solver import pywraplp

from matching.models import OrderIn, WagonIn, Assignment, UnmatchedOrder
from matching.graph import reconstruct_path
from matching.cost import empty_run_cost, estimated_hours
from matching.scoring import compute_score


def mip_match(
    orders: list[OrderIn],
    wagons: list[WagonIn],
    dist_matrix: dict[str, dict[str, float]],
    prev_matrix: dict[str, dict[str, str | None]],
    stations_meta: dict[str, dict] | None = None,
    loaded_km_lookup: dict[str, float] | None = None,
    current_hour: int = 0,
    weights: dict | None = None,
) -> tuple[list[Assignment], list[UnmatchedOrder]]:
    """Optimal assignment via Mixed Integer Programming.

    Args:
        orders: pending orders with quantity, type, deadline
        wagons: idle wagons with position, type, idle_days
        dist_matrix: precomputed all-pairs shortest distances
        prev_matrix: predecessor pointers for path reconstruction
        stations_meta: {station_id: {"role": ..., "cargo": [...]}} for flow/compatibility
        loaded_km_lookup: {station_id: median_loaded_km} for revenue estimation
        current_hour: simulation hour (for deadline penalty)
        weights: scoring weights dict (w1-w6 + modes)

    Returns:
        (assignments, unmatched_orders) — same format as greedy
    """
    if not orders or not wagons:
        return [], [UnmatchedOrder(order_id=o.order_id, reason="no_wagons")
                    for o in orders]

    meta = stations_meta or {}
    loaded_lookup = loaded_km_lookup or {}
    w = weights or {}

    # SCIP for single API calls (no memory leak for one-off calls)
    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        solver = pywraplp.Solver.CreateSolver("HIGHS")

    # ----- Pre-compute expected train sizes per (station, type, destination) -----
    # n_wagons = min(idle wagons of this type at station, demand for destination).
    # This is the realistic train size — solo wagons don't exist in rail.
    # Minimum 5 wagons for any train (mentor: "5-6 ваг можемо на 30-40км").
    #
    # Weights MUST be tuned for this cost scale (not flat-20).
    from collections import Counter
    idle_counts: Counter[tuple[str, str]] = Counter()
    for wg in wagons:
        idle_counts[(wg.current_station_id, wg.wagon_type)] += 1

    dest_demand: Counter[tuple[str, str]] = Counter()
    for o in orders:
        dest_demand[(o.station_to_id, o.wagon_type)] += o.quantity

    # ----- Build variables and score matrix -----
    x: dict[tuple[str, str], pywraplp.Variable] = {}
    scores: dict[tuple[str, str], float] = {}

    # Pre-compute station surplus for w8 (station pressure)
    # surplus = idle_of_type - daily_need_of_type
    station_surplus: dict[tuple[str, str], float] = {}
    for wg in wagons:
        key = (wg.current_station_id, wg.wagon_type)
        if key not in station_surplus:
            info = meta.get(wg.current_station_id, {})
            daily_need = info.get("avg_orders_per_day", 0) * 10  # avg order ~10 wagons
            station_surplus[key] = idle_counts[key] - daily_need

    for wagon in wagons:
        wagon_meta = meta.get(wagon.current_station_id, {})
        wagon_role = wagon_meta.get("role", "")
        n_idle = idle_counts[(wagon.current_station_id, wagon.wagon_type)]
        w_surplus = station_surplus.get(
            (wagon.current_station_id, wagon.wagon_type), 0.0
        )

        for order in orders:
            # Hard constraint: type match
            if wagon.wagon_type != order.wagon_type:
                continue

            dist = dist_matrix.get(wagon.current_station_id, {}).get(
                order.station_to_id, float("inf")
            )
            if dist == float("inf"):
                continue

            n_demand = dest_demand[(order.station_to_id, order.wagon_type)]
            n_wagons = min(n_idle, n_demand)

            order_meta = meta.get(order.station_to_id, {})
            order_role = order_meta.get("role", "")
            order_demand = order_meta.get("avg_orders_per_day", 0.0)
            loaded_km = loaded_lookup.get(order.station_to_id, 0.0)
            eta = dist / 40.0

            score = compute_score(
                empty_km=dist,
                idle_hours=wagon.idle_days * 24,
                loaded_km=loaded_km,
                eta_hours=eta,
                deadline_hour=order.desired_date_hour,
                current_hour=current_hour,
                wagon_station_role=wagon_role,
                order_station_role=order_role,
                wagon_type=wagon.wagon_type,
                cargo=order.cargo,
                weights=w,
                n_wagons=n_wagons,
                order_station_demand=order_demand,
                wagon_station_surplus=w_surplus,
            )

            # Skip pairs with infinite penalty (expired beyond buffer)
            if score == float("inf"):
                continue

            key = (wagon.wagon_id, order.order_id)
            x[key] = solver.BoolVar(f"x_{wagon.wagon_id}_{order.order_id}")
            scores[key] = score

    if not x:
        return [], [UnmatchedOrder(order_id=o.order_id, reason="no_compatible_pairs")
                    for o in orders]

    # ----- Constraints -----

    # Each order gets at most quantity wagons (flexible — allows partial)
    for order in orders:
        order_vars = [x[k] for k in x if k[1] == order.order_id]
        if order_vars:
            solver.Add(sum(order_vars) <= order.quantity)

    # Each wagon assigned at most once
    for wagon in wagons:
        wagon_vars = [x[k] for k in x if k[0] == wagon.wagon_id]
        if wagon_vars:
            solver.Add(sum(wagon_vars) <= 1)

    # ----- Objective: minimize total score -----
    # Add reward for making assignments (otherwise solver could assign nobody
    # and get score=0). Reward = large negative per assignment.
    assignment_reward = -max(abs(s) for s in scores.values()) * 2 if scores else -10000

    solver.Minimize(
        sum(x[k] * scores[k] for k in x)
        + sum(x[k] * assignment_reward for k in x)
    )

    # ----- Solve -----
    status = solver.Solve()

    # ----- Extract results -----
    assignments: list[Assignment] = []
    matched_order_ids: set[str] = set()

    if status in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        for (wid, oid), var in x.items():
            if var.solution_value() > 0.5:
                wagon = next(w for w in wagons if w.wagon_id == wid)
                order = next(o for o in orders if o.order_id == oid)

                dist = dist_matrix[wagon.current_station_id][order.station_to_id]
                prev = prev_matrix[wagon.current_station_id]
                route = reconstruct_path(prev, order.station_to_id)

                assignments.append(Assignment(
                    order_id=oid,
                    wagon_id=wid,
                    wagon_number=wagon.wagon_number,
                    route=route,
                    empty_run_km=round(dist, 1),
                    cost_empty_run=round(empty_run_cost(dist), 1),
                    estimated_hours=round(estimated_hours(dist), 1),
                ))
                matched_order_ids.add(oid)

    # Unmatched orders
    unmatched: list[UnmatchedOrder] = []
    for order in orders:
        assigned_count = sum(1 for a in assignments if a.order_id == order.order_id)
        if assigned_count < order.quantity:
            unmatched.append(UnmatchedOrder(
                order_id=order.order_id,
                reason="insufficient_wagons" if assigned_count > 0 else "no_available_wagons",
            ))

    return assignments, unmatched
