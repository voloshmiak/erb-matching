"""Multi-factor scoring for wagon-order pairs.

Each component is a separate function with parametrized mode and weight.
The optimizer sweeps weights to find the best combination. We don't guess —
data decides.

Score = lower is better (minimized by MIP).
"""
from __future__ import annotations

REVENUE_PER_KM_LOADED = 30
AVG_SPEED_KMH = 40

# Train cost model (sub-linear):
#   train_cost(d, n) = d * (LOCO_BASE + MARGINAL * (n-1))
#   per_wagon(d, n)  = d * (LOCO_BASE/n + MARGINAL)
#
# Backed by real UZ data (see docs/realistic_cost_model.md):
#   - Locomotive: ~20 UAH/km (fuel + crew + track access)
#   - Marginal per wagon: ~2 UAH/km (incremental fuel + coupling wear)
#   - At n=1: 20 UAH/km (matches ТЗ flat rate)
#   - At n=30: 2.6 UAH/km (87% reduction — real train economics)
#
# Sources: UZ собівартість 0.50 UAH/tkm, wagon rental 1750 UAH/day,
#          FRA empty wagon resistance data, mentor operational params.
LOCO_BASE = 20.0   # UAH/km — locomotive + track (fixed per train)
MARGINAL = 2.0      # UAH/km — per additional wagon


def cost_per_wagon_km(n_wagons: int = 1) -> float:
    """Per-wagon cost rate (UAH/km) given expected train size.

    Train cost: d × LOCO_BASE + d × MARGINAL × (n-1)
    Per wagon:  d × ((LOCO_BASE - MARGINAL)/n + MARGINAL)

    Sub-linear: more wagons sharing one locomotive = cheaper per wagon.
    n=1 → 20.0,  n=5 → 5.6,  n=10 → 3.8,  n=30 → 2.6,  n=50 → 2.36

    The n_wagons estimate comes from pre-counting idle wagons of same type
    at the same station before MIP runs.

    Estimation bias (documented):
      This is a CONSERVATIVE UPPER BOUND on train size, giving a LOWER BOUND
      on per-wagon cost. The actual train may be smaller than predicted.

      Why this is acceptable:
      1. Consistent bias — all wagons at a station get the same rate,
         so MIP's relative ranking between them is preserved.
      2. Self-reinforcing — MIP sees "wagons here are cheap to send together"
         → assigns them together → train forms close to predicted size.
      3. Small error — overestimating train size by 30% changes cost from
         2.6 to 2.9 UAH/km (12% error). Using flat 20 when reality is 2.6
         is a 670% error.
      4. Directionally correct — the bias nudges MIP toward grouping,
         which is what we want (trains are always cheaper than solo).
    """
    if n_wagons < 1:
        n_wagons = 1
    return (LOCO_BASE - MARGINAL) / n_wagons + MARGINAL


# Solo wagon rate = 20 UAH/km (matches ТЗ flat rate)
COST_PER_KM_EMPTY = LOCO_BASE  # cost_per_wagon_km(1) = 20


# ---------------------------------------------------------------------------
# Component 1: Distance cost
# ---------------------------------------------------------------------------

def distance_cost(empty_km: float, w1: float = 1.0, n_wagons: int = 1) -> float:
    """Cost of empty run in UAH. Train-aware: cost depends on group size."""
    return w1 * empty_km * cost_per_wagon_km(n_wagons)


# ---------------------------------------------------------------------------
# Component 2: Idle penalty (negative = prioritize idle wagons)
# ---------------------------------------------------------------------------

def idle_penalty(idle_hours: float, w2: float = 0.0, mode: str = "linear") -> float:
    """Penalize long-idle wagons. Negative value = lower score = more attractive.

    Modes:
      linear:    each hour equally weighted
      exp:       flat at first, then sharp growth after 24h
      threshold: zero until 24h, then linear
    """
    if w2 == 0:
        return 0.0

    if mode == "linear":
        return -idle_hours * w2
    elif mode == "exp":
        return -w2 * (2 ** (idle_hours / 24))
    elif mode == "threshold":
        return 0.0 if idle_hours < 24 else -w2 * (idle_hours - 24)
    return 0.0


# ---------------------------------------------------------------------------
# Component 3: Expected revenue (negative = more attractive)
# ---------------------------------------------------------------------------

def expected_revenue(loaded_km: float, w3: float = 0.0) -> float:
    """Revenue from loaded trip after delivery. Negative = lowers score.

    loaded_km = median distance from order station to its typical unloading
    destinations (from station primary_destinations).
    """
    if w3 == 0 or loaded_km <= 0:
        return 0.0
    return -w3 * loaded_km * REVENUE_PER_KM_LOADED


# ---------------------------------------------------------------------------
# Component 4: Late penalty (positive = less attractive)
# ---------------------------------------------------------------------------

def late_penalty(
    eta_hours: float,
    deadline_hour: int,
    current_hour: int,
    w4: float = 0.0,
    mode: str = "linear",
    buffer_hours: int = 72,
) -> float:
    """Penalty for arriving after deadline. Soft deadline with hard ceiling.

    eta_hours = empty_km / 40 (travel time)
    arrival_hour = current_hour + eta_hours
    If arrival > deadline: penalty grows. After buffer: infinity (expired).

    Modes:
      linear:    k * hours_late
      quadratic: k * hours_late² (punishes severe lateness harder)
    """
    if w4 == 0:
        return 0.0

    arrival_hour = current_hour + eta_hours
    hours_late = arrival_hour - deadline_hour

    if hours_late <= 0:
        return 0.0  # on time

    if hours_late > buffer_hours:
        return float("inf")  # truly expired, hard ceiling

    if mode == "linear":
        return w4 * hours_late
    elif mode == "quadratic":
        return w4 * hours_late * hours_late
    return 0.0


# ---------------------------------------------------------------------------
# Component 5: Flow direction penalty
# ---------------------------------------------------------------------------

def flow_penalty(
    wagon_station_role: str,
    order_station_role: str,
    w5: float = 0.0,
) -> float:
    """Penalty for sending wagon against cargo flow.

    With the flow (good): unloading → loading (natural empty return)
    Against the flow (bad): loading → loading (takes wagon from where needed)

    Roles: "loading", "unloading", "both"
    """
    if w5 == 0:
        return 0.0

    # Against flow: wagon at loading station sent to another loading station
    if wagon_station_role == "loading" and order_station_role == "loading":
        return w5 * COST_PER_KM_EMPTY * 10  # significant penalty

    # With flow: wagon at unloading station sent to loading station
    if wagon_station_role == "unloading" and order_station_role == "loading":
        return -w5 * COST_PER_KM_EMPTY * 5  # bonus (negative = attractive)

    return 0.0  # neutral (both, or unloading→unloading)


# ---------------------------------------------------------------------------
# Component 6: Cargo compatibility multiplier
# ---------------------------------------------------------------------------

def cargo_compatibility(wagon_type: str, cargo: str, w6: float = 0.0) -> float:
    """Multiplier for wagon-cargo efficiency.

    gondola → ore: natural (beta ~1.05, return almost guaranteed)
    gondola → crushed_stone: less efficient (beta ~1.55, scatter pattern)
    grain_hopper → grain, cement_hopper → cement: always 1.0 (matched)

    Returns a multiplier >= 1.0 (1.0 = natural, >1.0 = penalized).
    """
    if w6 == 0:
        return 1.0

    # Only gondola has dual cargo — ore and crushed_stone
    if wagon_type == "gondola" and cargo == "crushed_stone":
        return 1.0 + w6  # e.g., w6=0.3 → multiplier 1.3

    return 1.0  # natural pairing or matched type


# ---------------------------------------------------------------------------
# Component 7: Demand heat (from Artem) — bonus for sending to high-demand stations
# ---------------------------------------------------------------------------

def demand_heat(
    order_station_demand: float,
    w7: float = 0.0,
) -> float:
    """Bonus for serving high-demand stations first.

    Artem's idea: stations with higher avg_orders_per_day should attract
    wagons more strongly. High-demand station = more future revenue potential.

    order_station_demand = avg_orders_per_day from station metadata.
    Negative = lower score = more attractive.
    """
    if w7 == 0 or order_station_demand <= 0:
        return 0.0
    return -w7 * order_station_demand * 100  # scale to be meaningful


# ---------------------------------------------------------------------------
# Component 8: Station pressure (from Artem) — penalty for taking from deficit
# ---------------------------------------------------------------------------

def station_pressure(
    wagon_station_surplus: float,
    w8: float = 0.0,
) -> float:
    """Penalty for taking wagons from stations already in deficit.

    Artem's idea: if wagon's current station has few idle wagons relative
    to its demand, sending this wagon away makes the deficit worse.

    wagon_station_surplus = idle_count - daily_need. Positive = surplus, negative = deficit.
    Positive surplus = cheap to take (bonus). Negative = expensive to take (penalty).
    """
    if w8 == 0:
        return 0.0
    if wagon_station_surplus >= 0:
        return -w8 * min(wagon_station_surplus, 20) * 10  # bonus for taking from surplus
    else:
        return w8 * abs(wagon_station_surplus) * 50  # penalty for taking from deficit


# ---------------------------------------------------------------------------
# Combined score
# ---------------------------------------------------------------------------

def compute_score(
    empty_km: float,
    idle_hours: float,
    loaded_km: float,
    eta_hours: float,
    deadline_hour: int,
    current_hour: int,
    wagon_station_role: str,
    order_station_role: str,
    wagon_type: str,
    cargo: str,
    weights: dict | None = None,
    n_wagons: int = 1,
    order_station_demand: float = 0.0,
    wagon_station_surplus: float = 0.0,
) -> float:
    """Compute combined score for a wagon-order pair.

    Lower score = better assignment. MIP minimizes total score.

    weights dict keys: w1-w8 + modes
    n_wagons: expected train size (for sub-linear cost)
    order_station_demand: avg_orders_per_day at order destination (for w7)
    wagon_station_surplus: idle_count - daily_need at wagon station (for w8)
    """
    w = weights or {}
    w1 = w.get("w1", 1.0)
    w2 = w.get("w2", 0.0)
    w2_mode = w.get("w2_mode", "linear")
    w3 = w.get("w3", 0.0)
    w4 = w.get("w4", 0.0)
    w4_mode = w.get("w4_mode", "linear")
    w5 = w.get("w5", 0.0)
    w6 = w.get("w6", 0.0)
    w7 = w.get("w7", 0.0)
    w8 = w.get("w8", 0.0)

    score = distance_cost(empty_km, w1, n_wagons)
    score += idle_penalty(idle_hours, w2, w2_mode)
    score += expected_revenue(loaded_km, w3)
    score += late_penalty(eta_hours, deadline_hour, current_hour, w4, w4_mode)
    score += flow_penalty(wagon_station_role, order_station_role, w5)
    score += demand_heat(order_station_demand, w7)
    score += station_pressure(wagon_station_surplus, w8)
    score *= cargo_compatibility(wagon_type, cargo, w6)

    return score
