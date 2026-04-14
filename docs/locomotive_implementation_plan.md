I now have a thorough understanding of all three systems. Let me compile the comprehensive implementation plan.

---

# Locomotive Routing Implementation Plan

## Executive Summary

The current system has locomotives only in the Rust optimizer, where they are tracked as a post-filter resource (the `Locomotive` struct at `simulation.rs:69-72`) but **teleport** -- when a train needs dispatch, any available loco anywhere is grabbed and instantly appears at the source station (line 385-388: `locos.iter().enumerate().find_map(|(i, l)| { if l.available_at <= hour && !locos_used.contains(&i) { Some(i) } else { None } })`). The loco is then teleported to the destination: `locos[li].station_id = dst.clone()` (line 394). No travel cost, no travel time, no physical movement.

The Python production service (`erb-matching/algos/matching/matcher.py`) has zero locomotive awareness. The Go backend (`erb-backend`) has zero locomotive awareness.

This plan replaces teleportation with physical locomotive routing across all three systems.

---

## 1. RUST OPTIMIZER CHANGES

### 1.1 Problem Analysis

In `simulation.rs` lines 385-388, the loco selection logic is:
```rust
let li = locos.iter().enumerate().find_map(|(i, l)| {
    if l.available_at <= hour && !locos_used.contains(&i) { Some(i) } else { None }
});
```

This picks **any** available loco, regardless of location. Then at line 393-394:
```rust
locos[li].available_at = hour + (dist / AVG_SPEED_KMH) as usize + 1;
locos[li].station_id = dst.clone();
```

The loco teleports from wherever it is to `src` (no cost, no time), then moves with the train to `dst`. The time only accounts for `src->dst` travel, not `loco_station->src`.

### 1.2 Locomotive Struct Enhancement

File: `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-dev/optimizer_rust/src/simulation.rs`

Replace lines 68-72:
```rust
#[derive(Debug, Clone)]
struct Locomotive {
    station_id: String,
    available_at: usize,
}
```

With:
```rust
#[derive(Debug, Clone)]
struct Locomotive {
    id: usize,
    station_id: String,
    available_at: usize,
    total_empty_km: f64,     // loco-only repositioning km (no wagons)
    total_loaded_km: f64,    // km traveled hauling trains
    repositions: usize,      // number of empty repositioning moves
    trains_hauled: usize,    // number of trains hauled
}
```

### 1.3 init_locos Update

Replace lines 153-158:
```rust
fn init_locos(station_ids: &[String]) -> Vec<Locomotive> {
    (0..NUM_LOCOMOTIVES).map(|i| Locomotive {
        id: i,
        station_id: station_ids[i % station_ids.len()].clone(),
        available_at: 0,
        total_empty_km: 0.0,
        total_loaded_km: 0.0,
        repositions: 0,
        trains_hauled: 0,
    }).collect()
}
```

### 1.4 Nearest-Loco Dispatch with Economic Gate

Replace the locomotive selection block at lines 385-388 and surrounding logic (lines 378-414). The new logic for each `(src, dst)` group:

```rust
for ((src, dst), group) in sorted {
    let dist = /* ... same distance lookup as before ... */;
    if group.len() < min_train_size(dist) { continue; }

    // === NEW: Find nearest available loco by physical distance ===
    let mut best_loco: Option<(usize, f64)> = None; // (index, reposition_km)
    for (i, l) in locos.iter().enumerate() {
        if l.available_at > hour || locos_used.contains(&i) { continue; }

        // Calculate physical distance from loco to train source
        let loco_to_src = if l.station_id == src {
            0.0  // loco already at source -- no repositioning needed
        } else if let (Some(&li), Some(&si)) = (idx.get(l.station_id.as_str()), idx.get(src.as_str())) {
            let d = dist_matrix[li][si];
            if d.is_infinite() { continue; }
            d
        } else {
            continue;
        };

        if best_loco.is_none() || loco_to_src < best_loco.as_ref().unwrap().1 {
            best_loco = Some((i, loco_to_src));
        }
    }
    let (li, reposition_km) = match best_loco { Some(v) => v, None => continue };

    // === NEW: Economic gate ===
    // Loco repositioning costs LOCO_BASE UAH/km (light engine, no wagons)
    let reposition_cost = reposition_km * LOCO_BASE;  // 20 UAH/km
    let batch_size = group.len().min(MAX_TRAIN_SIZE);
    let train_revenue_estimate = batch_size as f64 * dist * 30.0;  // loaded revenue
    let train_empty_cost = batch_size as f64 * dist * cost_per_wagon_km(batch_size);

    // Skip if repositioning cost exceeds the net margin of the train
    let net_margin = train_revenue_estimate - train_empty_cost;
    if reposition_cost > net_margin * 0.5 {
        // Repositioning eats more than 50% of expected margin -- skip
        continue;
    }

    locos_used.insert(li);

    let batch = &group[..batch_size];

    // === NEW: Account for loco travel time ===
    let reposition_hours = reposition_km / AVG_SPEED_KMH;
    let train_travel_hours = dist / AVG_SPEED_KMH;
    let total_loco_hours = reposition_hours + train_travel_hours;

    // Loco arrives at destination after reposition + train travel
    locos[li].available_at = hour + total_loco_hours as usize + 1;
    locos[li].station_id = dst.clone();

    // === NEW: Track loco metrics ===
    if reposition_km > 0.0 {
        locos[li].total_empty_km += reposition_km;
        locos[li].repositions += 1;
        total_loco_empty_km += reposition_km;
        total_repositions += 1;
    }
    locos[li].total_loaded_km += dist;
    locos[li].trains_hauled += 1;

    trains_formed += 1;
    total_wagons_in_trains += batch.len();

    for a in batch {
        let order = order_map.get(a.order_id.as_str()).unwrap();
        let loaded_dest = generator.get_loaded_destination(&order.cargo);
        let loaded_km = /* ... same as before ... */;

        // === NEW: Wagon ETA includes loco repositioning delay ===
        let total_hours = reposition_hours  // wait for loco to arrive
            + dist / AVG_SPEED_KMH          // empty travel (train)
            + LOADING_H                      // loading at destination
            + loaded_km / AVG_SPEED_KMH     // loaded travel
            + UNLOADING_H;                  // unloading

        wagons[a.wagon_idx].busy_until = hour + total_hours as usize + 1;
        wagons[a.wagon_idx].destination_station_id = loaded_dest;

        total_assigned += 1;
        total_empty_km += dist;
        total_loaded_km += loaded_km;
        *assigned_order_ids.entry(a.order_id.clone()).or_insert(0) += 1;
    }
}
```

### 1.5 New Metric Fields in SimResult

At `simulation.rs` line 108, add to `SimResult`:
```rust
pub struct SimResult {
    // ... existing fields ...
    pub loco_empty_km: f64,         // total light-engine repositioning km
    pub loco_repositions: usize,    // number of repositioning moves
    pub loco_utilization_pct: f64,  // % of hours locos are busy
    pub avg_reposition_km: f64,     // average repositioning distance
    pub reposition_cost: f64,       // total repositioning cost in UAH
}
```

Add new counters at lines 316-321:
```rust
let mut total_loco_empty_km: f64 = 0.0;
let mut total_repositions: usize = 0;
```

At the end of `run()` (before returning `SimResult`), compute loco utilization:
```rust
let total_loco_busy_hours: usize = locos.iter()
    .map(|l| l.total_empty_km as usize / AVG_SPEED_KMH as usize + l.total_loaded_km as usize / AVG_SPEED_KMH as usize)
    .sum();
let loco_utilization_pct = if hours > 0 {
    total_loco_busy_hours as f64 / (NUM_LOCOMOTIVES * hours) as f64 * 100.0
} else { 0.0 };
```

### 1.6 Impact on cost_per_wagon_km in scoring.rs

No changes needed in `scoring.rs`. The `LOCO_BASE` constant (20 UAH/km, line 4) is already used for the light-engine cost. The repositioning cost in the economic gate uses this same constant directly, which is correct: a light engine running without wagons costs the locomotive base rate.

### 1.7 Summary of Rust File Changes

| File | Lines | Change |
|------|-------|--------|
| `simulation.rs` | 68-72 | Expand `Locomotive` struct with metrics fields |
| `simulation.rs` | 108-124 | Add 5 new fields to `SimResult` |
| `simulation.rs` | 153-158 | Update `init_locos` for new struct fields |
| `simulation.rs` | 316-321 | Add loco metric counters |
| `simulation.rs` | 378-414 | Replace teleport with nearest-loco + economic gate + reposition timing |
| `simulation.rs` | 428-454 | Compute loco utilization, fill new SimResult fields |

---

## 2. PYTHON MATCHING SERVICE CHANGES (erb-matching)

### 2.1 API Contract Extension

The Python service currently accepts `MatchRequest` with fields `{orders, wagons, stations, edges}` and returns `MatchResponse` with `{assignments, unmatched_orders, metrics}`.

The extension adds **optional** fields for backward compatibility.

### 2.2 models.py Changes

File: `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-matching/algos/matching/models.py`

Add new input model after `EdgeIn` (line 37):
```python
class LocomotiveIn(BaseModel):
    locomotive_id: str
    current_station_id: str
    available_at_hour: int = 0  # 0 = available now
```

Extend `MatchRequest` (lines 39-43):
```python
class MatchRequest(BaseModel):
    orders: list[OrderIn]
    wagons: list[WagonIn]
    stations: list[StationIn]
    edges: list[EdgeIn]
    # NEW: optional locomotive positions for train-aware matching
    locomotives: list[LocomotiveIn] = []  # empty = no loco constraint
    current_hour: int = 0  # simulation hour for loco availability check
```

Add new response model for train groups after `Assignment` (line 54):
```python
class TrainGroup(BaseModel):
    """A group of assignments that should travel together as one train."""
    source_station_id: str
    destination_station_id: str
    locomotive_id: str | None = None  # which loco to use (None = no loco assigned)
    wagon_ids: list[str]
    order_ids: list[str]
    empty_run_km: float  # distance source -> destination
    loco_reposition_km: float = 0.0  # distance loco -> source (0 if already there)
    estimated_hours: float  # includes loco repositioning time
```

Extend `Metrics` (lines 61-70):
```python
class Metrics(BaseModel):
    # ... existing fields ...
    # NEW: locomotive-aware metrics
    trains_formed: int = 0
    avg_train_size: float = 0.0
    loco_reposition_km: float = 0.0
    locos_used: int = 0
```

Extend `MatchResponse` (lines 73-76):
```python
class MatchResponse(BaseModel):
    assignments: list[Assignment]
    unmatched_orders: list[UnmatchedOrder]
    metrics: Metrics
    # NEW: optional train grouping with loco assignments
    train_groups: list[TrainGroup] = []
```

### 2.3 matcher.py Changes

File: `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-matching/algos/matching/matcher.py`

The `match()` function (line 147) needs a new post-processing step after scoring/MIP assignment: group assignments into trains and assign locomotives.

Add new import at top:
```python
from matching.models import LocomotiveIn, TrainGroup
```

Add new function after `_scored_greedy`:
```python
def _group_into_trains(
    assignments: list[Assignment],
    locomotives: list[LocomotiveIn],
    dist_matrix: dict[str, dict[str, float]],
    current_hour: int,
) -> list[TrainGroup]:
    """Group assignments by (source, destination), assign nearest available loco.

    Distance-based min train size:
      <30km: 1, 30-300km: 3, >300km: 5
    """
    if not assignments:
        return []

    # Group by (wagon_source, order_destination) using the assignment route
    from collections import defaultdict
    groups: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    for a in assignments:
        src = a.route[0] if a.route else ""
        dst = a.route[-1] if a.route else ""
        if src and dst:
            groups[(src, dst)].append(a)

    # Sort groups by size descending (largest trains first, greedy loco allocation)
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    # Track used locos
    used_loco_ids: set[str] = set()
    train_groups: list[TrainGroup] = []

    for (src, dst), group_assignments in sorted_groups:
        dist = dist_matrix.get(src, {}).get(dst, float("inf"))
        if dist == float("inf"):
            continue

        # Min train size check
        if dist < 30:
            min_size = 1
        elif dist < 300:
            min_size = 3
        else:
            min_size = 5

        if len(group_assignments) < min_size:
            continue  # too few wagons for this route

        # Find nearest available locomotive
        best_loco = None
        best_reposition_km = float("inf")

        if locomotives:
            for loco in locomotives:
                if loco.locomotive_id in used_loco_ids:
                    continue
                if loco.available_at_hour > current_hour:
                    continue

                if loco.current_station_id == src:
                    reposition_km = 0.0
                else:
                    reposition_km = dist_matrix.get(loco.current_station_id, {}).get(
                        src, float("inf"))

                if reposition_km < best_reposition_km:
                    best_loco = loco
                    best_reposition_km = reposition_km

            if best_loco is None:
                continue  # no loco available

            # Economic gate: repositioning cost vs train margin
            LOCO_BASE = 20.0
            reposition_cost = best_reposition_km * LOCO_BASE
            n = len(group_assignments)
            per_wagon_cost = (LOCO_BASE - 2.0) / max(n, 1) + 2.0
            train_net_margin = n * dist * 30.0 - n * dist * per_wagon_cost
            if reposition_cost > train_net_margin * 0.5:
                continue

            used_loco_ids.add(best_loco.locomotive_id)
            loco_id = best_loco.locomotive_id
        else:
            # No locos provided -- form train without loco assignment
            loco_id = None
            best_reposition_km = 0.0

        reposition_hours = best_reposition_km / 40.0
        train_travel_hours = dist / 40.0

        train_groups.append(TrainGroup(
            source_station_id=src,
            destination_station_id=dst,
            locomotive_id=loco_id,
            wagon_ids=[a.wagon_id for a in group_assignments[:50]],  # max 50
            order_ids=list({a.order_id for a in group_assignments[:50]}),
            empty_run_km=round(dist, 1),
            loco_reposition_km=round(best_reposition_km, 1),
            estimated_hours=round(reposition_hours + train_travel_hours, 1),
        ))

    return train_groups
```

Modify the `match()` function (line 147) to add train grouping after assignment:

```python
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

    # Try MIP first, fall back to scored greedy
    try:
        from matching.mip_matcher import mip_match
        assignments, unmatched = mip_match(
            request.orders, request.wagons, dist_matrix, prev_matrix,
            stations_meta=stations_meta, current_hour=request.current_hour,
            weights=TUNED_WEIGHTS,
        )
    except Exception:
        assignments, unmatched = _scored_greedy(
            request.orders, request.wagons, dist_matrix, prev_matrix,
            stations_meta, TUNED_WEIGHTS,
        )

    # === NEW: Train grouping with locomotive assignment ===
    train_groups = _group_into_trains(
        assignments, request.locomotives, dist_matrix, request.current_hour
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

    loco_reposition_km = sum(tg.loco_reposition_km for tg in train_groups)
    locos_used = sum(1 for tg in train_groups if tg.locomotive_id is not None)

    return MatchResponse(
        assignments=assignments,
        unmatched_orders=unmatched,
        train_groups=train_groups,  # NEW
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
            trains_formed=len(train_groups),
            avg_train_size=round(sum(len(tg.wagon_ids) for tg in train_groups) / max(len(train_groups), 1), 1),
            loco_reposition_km=round(loco_reposition_km, 1),
            locos_used=locos_used,
        ),
    )
```

### 2.4 Backward Compatibility

The new fields `locomotives` and `current_hour` in `MatchRequest` have defaults (`[]` and `0`). The Go backend will continue to work without changes until Misha adds locomotive support -- it will simply send no locomotives, the `_group_into_trains` function will form groups without loco IDs (`locomotive_id=None`), and the old `assignments` list remains unchanged.

The new `train_groups` field in `MatchResponse` defaults to `[]`. The Go backend's existing response parsing will ignore this field since Go's `json.Decoder` silently discards unknown JSON fields.

### 2.5 Summary of Python File Changes

| File | Change |
|------|--------|
| `models.py` | Add `LocomotiveIn`, `TrainGroup`; extend `MatchRequest`, `Metrics`, `MatchResponse` |
| `matcher.py` | Add `_group_into_trains()` function; modify `match()` to call it and populate new response fields |

---

## 3. GO BACKEND INTEGRATION

### 3.1 New Entity: entity/locomotive.go

```go
package entity

import (
    "time"
    "github.com/google/uuid"
)

type LocomotiveStatus string

const (
    LocoIdle      LocomotiveStatus = "idle"
    LocoInTransit LocomotiveStatus = "in_transit"      // hauling a train
    LocoReposition LocomotiveStatus = "repositioning"   // moving light to pick up wagons
)

type Locomotive struct {
    ID               uuid.UUID        `json:"id"`
    Name             string           `json:"name"`           // e.g., "LOCO-01"
    CurrentStationID uuid.UUID        `json:"currentStationId"`
    Status           LocomotiveStatus `json:"status"`
    AvailableAtHour  int64            `json:"availableAtHour"` // sim hour when loco becomes idle
    TrainID          *uuid.UUID       `json:"trainId,omitempty"`
    CreatedAt        time.Time        `json:"createdAt"`
}
```

### 3.2 DB Migration

PostgreSQL migration (add to whatever migration system the Go backend uses):

```sql
CREATE TABLE locomotives (
    id UUID PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE,
    current_station_id UUID NOT NULL REFERENCES stations(id),
    status VARCHAR(20) NOT NULL DEFAULT 'idle',
    available_at_hour BIGINT NOT NULL DEFAULT 0,
    train_id UUID REFERENCES trains(id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Seed 25 locomotives, one per station
INSERT INTO locomotives (id, name, current_station_id, status)
SELECT
    gen_random_uuid(),
    'LOCO-' || LPAD(ROW_NUMBER() OVER ()::TEXT, 2, '0'),
    id,
    'idle'
FROM stations
ORDER BY name
LIMIT 25;

-- Add locomotive_id to trains table
ALTER TABLE trains ADD COLUMN locomotive_id UUID REFERENCES locomotives(id);
```

### 3.3 Repository: repository/locomotive.go

```go
package repository

import (
    "context"
    "database/sql"
    "erb-backend/src/entity"
    "github.com/google/uuid"
)

type LocomotiveRepository struct {
    conn *sql.DB
}

func NewLocomotiveRepository(conn *sql.DB) *LocomotiveRepository {
    return &LocomotiveRepository{conn: conn}
}

func (r *LocomotiveRepository) List(ctx context.Context) ([]*entity.Locomotive, error) {
    rows, err := r.conn.QueryContext(ctx, `
        SELECT id, name, current_station_id, status, available_at_hour, train_id, created_at
        FROM locomotives
    `)
    if err != nil {
        return nil, err
    }
    defer rows.Close()

    var locos []*entity.Locomotive
    for rows.Next() {
        l := &entity.Locomotive{}
        var trainID sql.NullString
        if err := rows.Scan(&l.ID, &l.Name, &l.CurrentStationID, &l.Status,
            &l.AvailableAtHour, &trainID, &l.CreatedAt); err != nil {
            return nil, err
        }
        if trainID.Valid {
            id, _ := uuid.Parse(trainID.String)
            l.TrainID = &id
        }
        locos = append(locos, l)
    }
    return locos, rows.Err()
}

func (r *LocomotiveRepository) ListIdle(ctx context.Context, simHour int64) ([]*entity.Locomotive, error) {
    rows, err := r.conn.QueryContext(ctx, `
        SELECT id, name, current_station_id, status, available_at_hour, train_id, created_at
        FROM locomotives
        WHERE status = 'idle' AND available_at_hour <= $1
    `, simHour)
    if err != nil {
        return nil, err
    }
    defer rows.Close()

    var locos []*entity.Locomotive
    for rows.Next() {
        l := &entity.Locomotive{}
        if err := rows.Scan(&l.ID, &l.Name, &l.CurrentStationID, &l.Status,
            &l.AvailableAtHour, nil, &l.CreatedAt); err != nil {
            return nil, err
        }
        locos = append(locos, l)
    }
    return locos, rows.Err()
}

func (r *LocomotiveRepository) UpdateStatus(ctx context.Context, id uuid.UUID,
    status entity.LocomotiveStatus, stationID uuid.UUID, availableAt int64,
    trainID *uuid.UUID) error {
    _, err := r.conn.ExecContext(ctx, `
        UPDATE locomotives
        SET status = $1, current_station_id = $2, available_at_hour = $3, train_id = $4
        WHERE id = $5
    `, status, stationID, availableAt, trainID, id)
    return err
}

func (r *LocomotiveRepository) FreeExpired(ctx context.Context, simHour int64) (int, error) {
    result, err := r.conn.ExecContext(ctx, `
        UPDATE locomotives
        SET status = 'idle', train_id = NULL
        WHERE status IN ('in_transit', 'repositioning') AND available_at_hour <= $1
    `, simHour)
    if err != nil {
        return 0, err
    }
    n, _ := result.RowsAffected()
    return int(n), nil
}
```

### 3.4 Interface Addition: usecase/interfaces.go

Add after `TrainRepository` (line 57):

```go
type LocomotiveRepository interface {
    List(ctx context.Context) ([]*entity.Locomotive, error)
    ListIdle(ctx context.Context, simHour int64) ([]*entity.Locomotive, error)
    UpdateStatus(ctx context.Context, id uuid.UUID, status entity.LocomotiveStatus,
        stationID uuid.UUID, availableAt int64, trainID *uuid.UUID) error
    FreeExpired(ctx context.Context, simHour int64) (int, error)
}
```

### 3.5 Matching Gateway Changes: gateway/matching.go

Add new DTOs after `edgeDTO` (line 62):

```go
type locomotiveDTO struct {
    LocomotiveID     string `json:"locomotive_id"`
    CurrentStationID string `json:"current_station_id"`
    AvailableAtHour  int    `json:"available_at_hour"`
}
```

Extend `matchRequest` (lines 64-69):

```go
type matchRequest struct {
    Orders      []orderDTO      `json:"orders"`
    Wagons      []wagonDTO      `json:"wagons"`
    Stations    []stationDTO    `json:"stations"`
    Edges       []edgeDTO       `json:"edges"`
    Locomotives []locomotiveDTO `json:"locomotives,omitempty"`
    CurrentHour int             `json:"current_hour,omitempty"`
}
```

Add new response DTOs after `matchMetrics`:

```go
type trainGroupDTO struct {
    SourceStationID      string   `json:"source_station_id"`
    DestinationStationID string   `json:"destination_station_id"`
    LocomotiveID         *string  `json:"locomotive_id"`
    WagonIDs             []string `json:"wagon_ids"`
    OrderIDs             []string `json:"order_ids"`
    EmptyRunKM           float64  `json:"empty_run_km"`
    LocoRepositionKM     float64  `json:"loco_reposition_km"`
    EstimatedHours       float64  `json:"estimated_hours"`
}
```

Extend `matchResponse`:

```go
type matchResponse struct {
    Assignments     []matchedAssignment `json:"assignments"`
    UnmatchedOrders []unmatchedOrder    `json:"unmatched_orders"`
    Metrics         matchMetrics        `json:"metrics"`
    TrainGroups     []trainGroupDTO     `json:"train_groups,omitempty"`
}
```

Add conversion helper:

```go
func toLocomotiveDTOs(locos []*entity.Locomotive) []locomotiveDTO {
    out := make([]locomotiveDTO, 0, len(locos))
    for _, l := range locos {
        out = append(out, locomotiveDTO{
            LocomotiveID:     l.ID.String(),
            CurrentStationID: l.CurrentStationID.String(),
            AvailableAtHour:  int(l.AvailableAtHour),
        })
    }
    return out
}
```

Modify the `Match` method signature to accept locomotives:

```go
func (g *MatchingGateway) Match(ctx context.Context, orders []*entity.Order,
    wagons []*entity.Wagon, stations []*entity.Station, edges []*entity.Edge,
    locos []*entity.Locomotive, simHour int64) ([]*entity.AssignmentResult,
    []entity.TrainGroupResult, error) {

    reqBody := matchRequest{
        Orders:      toOrderDTOs(orders),
        Wagons:      toWagonDTOs(wagons),
        Stations:    toStationDTOs(stations),
        Edges:       toEdgeDTOs(edges),
        Locomotives: toLocomotiveDTOs(locos),
        CurrentHour: int(simHour),
    }

    // ... existing HTTP call logic ...

    // Parse train groups from response
    var trainGroupResults []entity.TrainGroupResult
    for _, tg := range result.TrainGroups {
        // Parse UUIDs
        var locoID *uuid.UUID
        if tg.LocomotiveID != nil {
            id, err := uuid.Parse(*tg.LocomotiveID)
            if err == nil {
                locoID = &id
            }
        }
        srcID, _ := uuid.Parse(tg.SourceStationID)
        dstID, _ := uuid.Parse(tg.DestinationStationID)

        wagonIDs := make([]uuid.UUID, 0, len(tg.WagonIDs))
        for _, w := range tg.WagonIDs {
            id, _ := uuid.Parse(w)
            wagonIDs = append(wagonIDs, id)
        }

        trainGroupResults = append(trainGroupResults, entity.TrainGroupResult{
            SourceStationID:      srcID,
            DestinationStationID: dstID,
            LocomotiveID:         locoID,
            WagonIDs:             wagonIDs,
            EmptyRunKM:           tg.EmptyRunKM,
            LocoRepositionKM:     tg.LocoRepositionKM,
            EstimatedHours:       tg.EstimatedHours,
        })
    }

    return out, trainGroupResults, nil
}
```

### 3.6 New Entity Type: entity/assignment.go addition

Add to `entity` package:

```go
type TrainGroupResult struct {
    SourceStationID      uuid.UUID   `json:"sourceStationId"`
    DestinationStationID uuid.UUID   `json:"destinationStationId"`
    LocomotiveID         *uuid.UUID  `json:"locomotiveId,omitempty"`
    WagonIDs             []uuid.UUID `json:"wagonIds"`
    EmptyRunKM           float64     `json:"emptyRunKm"`
    LocoRepositionKM     float64     `json:"locoRepositionKm"`
    EstimatedHours       float64     `json:"estimatedHours"`
}
```

### 3.7 Update MatchingGateway Interface

In `usecase/interfaces.go`, update line 64-66:

```go
type MatchingGateway interface {
    Match(ctx context.Context, orders []*entity.Order, wagons []*entity.Wagon,
        stations []*entity.Station, edges []*entity.Edge,
        locos []*entity.Locomotive, simHour int64) ([]*entity.AssignmentResult,
        []entity.TrainGroupResult, error)
}
```

### 3.8 CreateOrderUseCase Changes

File: `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-backend/src/usecase/create_order.go`

The `CreateOrderUseCase` struct (line 22) needs `locomotiveRepository` and `trainRepository`:

```go
type CreateOrderUseCase struct {
    orderRepository      OrderRepository
    stationRepository    StationRepository
    assignmentRepository AssignmentRepository
    routeStepRepository  RouteStepRepository
    wagonRepository      WagonRepository
    locomotiveRepository LocomotiveRepository  // NEW
    trainRepository      TrainRepository       // NEW
    broadcaster          Broadcaster
    matchingGateway      MatchingGateway
    simClock             *simclock.SimClock
}
```

In the `match()` method (line 129), add locomotive fetching and pass to gateway:

```go
func (u *CreateOrderUseCase) match(ctx context.Context) error {
    orders, err := u.orderRepository.FindPending(ctx)
    if err != nil {
        return errors.Wrap(err, "failed to get pending orders")
    }
    if len(orders) == 0 {
        return nil
    }

    wagons, err := u.wagonRepository.ListByStatus(ctx, entity.Idle)
    if err != nil {
        return errors.Wrap(err, "failed to get idle wagons")
    }

    stations, edges, err := u.stationRepository.List(ctx)
    if err != nil {
        return errors.Wrap(err, "failed to get stations and edges")
    }

    // === NEW: Fetch available locomotives ===
    simHour := u.simClock.Now()
    locos, err := u.locomotiveRepository.ListIdle(ctx, simHour)
    if err != nil {
        log.Printf("match: failed to get locomotives, proceeding without: %v", err)
        locos = nil // graceful degradation
    }

    results, trainGroups, err := u.matchingGateway.Match(
        ctx, orders, wagons, stations, edges, locos, simHour)
    if err != nil {
        return errors.Wrap(err, "matching service failed")
    }

    // === Process individual assignments (same as before) ===
    edgeDistMap := buildEdgeDistanceMap(edges)
    stationTypeMap := buildStationTypeMap(stations)

    for _, r := range results {
        // ... existing assignment creation logic (lines 157-191) ...
    }

    // === NEW: Auto-create trains from train groups ===
    for _, tg := range trainGroups {
        if len(tg.WagonIDs) == 0 {
            continue
        }

        // Build route: [source, destination] (simplified; could use full route)
        route := []uuid.UUID{tg.SourceStationID, tg.DestinationStationID}

        train := &entity.Train{
            ID:              uuid.New(),
            WagonIDs:        tg.WagonIDs,
            Route:           route,
            StepIndex:       0,
            SourceStationID: tg.SourceStationID,
            NextStationID:   tg.DestinationStationID,
            Status:          entity.TrainForming,
            LocomotiveID:    tg.LocomotiveID,  // NEW field on Train entity
            CreatedAt:       u.simClock.ToDisplayTime(simHour),
        }

        if err := u.trainRepository.Create(ctx, train); err != nil {
            log.Printf("match: failed to create train: %v", err)
            continue
        }

        // Update wagon statuses to InTrain
        for _, wid := range tg.WagonIDs {
            if err := u.wagonRepository.UpdateStatus(ctx, wid, entity.InTrain, nil); err != nil {
                log.Printf("match: failed to update wagon %s to in_train: %v", wid, err)
            }
        }

        // === Reserve locomotive ===
        if tg.LocomotiveID != nil {
            // If loco needs repositioning, set to "repositioning" first
            if tg.LocoRepositionKM > 0 {
                repositionHours := tg.LocoRepositionKM / 40.0
                repositionUntil := simHour + int64(math.Ceil(repositionHours))
                if err := u.locomotiveRepository.UpdateStatus(ctx, *tg.LocomotiveID,
                    entity.LocoReposition, tg.SourceStationID, repositionUntil,
                    &train.ID); err != nil {
                    log.Printf("match: failed to reserve loco %s: %v", tg.LocomotiveID, err)
                }
            } else {
                // Loco already at source, set to in_transit
                trainArrival := simHour + int64(math.Ceil(tg.EstimatedHours))
                if err := u.locomotiveRepository.UpdateStatus(ctx, *tg.LocomotiveID,
                    entity.LocoInTransit, tg.DestinationStationID, trainArrival,
                    &train.ID); err != nil {
                    log.Printf("match: failed to dispatch loco %s: %v", tg.LocomotiveID, err)
                }
            }
        }

        u.broadcaster.Publish(broadcaster.NewEvent(broadcaster.TrainCreated, train))
    }

    return nil
}
```

### 3.9 Ticker: Locomotive State Transitions

File: `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-backend/src/ticker/ticker.go`

Add `freeLocomotives` step to the `Ticker` struct and `tick()` method. The `LocomotiveRepository.FreeExpired` call handles the state transition from `repositioning`/`in_transit` to `idle` when `available_at_hour <= simHour`.

Add to `Ticker` struct:
```go
type Ticker struct {
    // ... existing fields ...
    locoRepo LocomotiveRepository  // NEW
}
```

Add to `tick()` after unloadWagons (line 79):
```go
func (t *Ticker) tick(ctx context.Context) {
    // ... existing code ...

    // === NEW: Free locomotives whose travel is complete ===
    freed, err := t.locoRepo.FreeExpired(ctx, currentHour)
    if err != nil {
        log.Println("ticker: failed to free locomotives:", err)
    } else if freed > 0 {
        log.Printf("ticker: freed %d locomotives at hour %d", freed, currentHour)
    }

    err = t.unloadWagons.Execute(ctx, currentHour)
    // ... rest of tick ...
}
```

### 3.10 Entity Modification: Train with LocomotiveID

File: `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-backend/src/entity/train.go`

Add field to `Train` struct (after line 21):
```go
type Train struct {
    // ... existing fields ...
    LocomotiveID *uuid.UUID `json:"locomotiveId,omitempty"` // NEW
}
```

Update `repository/train.go` to handle the new column in all queries.

### 3.11 API Endpoints

Add to `main.go`:
```go
// Controllers
listLocosController := controller.NewListLocomotivesController(listLocosUsecase)

// Routes
router.Handle("GET /api/locomotives", listLocosController)
router.Handle("GET /api/locomotives/{id}", getLocoController)
```

### 3.12 SSE Events

Add to `broadcaster/broadcaster.go`:
```go
const (
    // ... existing events ...
    LocoDispatched EventType = "locoDispatched"
    LocoArrived    EventType = "locoArrived"
    LocoReposition EventType = "locoRepositioning"
)
```

### 3.13 AdvanceTrains Modification

In `usecase/train_usecases.go`, the `advanceTrain` function (line 180) needs to update the locomotive when a train arrives at its destination. After line 238 (setting wagons to Idle when train arrives):

```go
// === NEW: Free the locomotive when train arrives ===
if t.LocomotiveID != nil {
    if err := uc.locoRepo.UpdateStatus(ctx, *t.LocomotiveID,
        entity.LocoIdle, t.NextStationID, simHour, nil); err != nil {
        log.Printf("advance_trains: failed to free loco %s: %v", t.LocomotiveID, err)
    }
    uc.broadcaster.Publish(broadcaster.NewEvent(broadcaster.LocoArrived, map[string]any{
        "locomotiveId": t.LocomotiveID,
        "stationId":    t.NextStationID,
        "trainId":      t.ID,
    }))
}
```

### 3.14 Summary of Go Backend Changes

| File | Change |
|------|--------|
| `entity/locomotive.go` | **New file**: `Locomotive` struct, status constants |
| `entity/train.go` | Add `LocomotiveID *uuid.UUID` field |
| `entity/assignment.go` | Add `TrainGroupResult` struct |
| `repository/locomotive.go` | **New file**: CRUD for locomotives |
| `repository/train.go` | Update queries for `locomotive_id` column |
| `usecase/interfaces.go` | Add `LocomotiveRepository` interface; update `MatchingGateway` |
| `usecase/create_order.go` | Add `locomotiveRepository` dep; pass locos to gateway; auto-create trains from groups |
| `usecase/train_usecases.go` | Free loco when train arrives; add `locoRepo` dependency |
| `gateway/matching.go` | Add `locomotiveDTO`, `trainGroupDTO`; extend request/response; update `Match` method |
| `ticker/ticker.go` | Add `locoRepo`; call `FreeExpired` in tick loop |
| `broadcaster/broadcaster.go` | Add `LocoDispatched`, `LocoArrived`, `LocoReposition` event types |
| `cmd/main/main.go` | Wire `LocomotiveRepository`, new usecases, new controllers, new routes |
| DB migration | Create `locomotives` table; add `locomotive_id` to `trains` |

---

## 4. API CONTRACT CHANGES

### 4.1 Request (POST /api/match)

**Before:**
```json
{
  "orders": [...],
  "wagons": [...],
  "stations": [...],
  "edges": [...]
}
```

**After (backward compatible):**
```json
{
  "orders": [...],
  "wagons": [...],
  "stations": [...],
  "edges": [...],
  "locomotives": [
    {
      "locomotive_id": "uuid-string",
      "current_station_id": "uuid-string",
      "available_at_hour": 0
    }
  ],
  "current_hour": 142
}
```

Both `locomotives` and `current_hour` are optional. If omitted, the service works identically to before.

### 4.2 Response (POST /api/match)

**Before:**
```json
{
  "assignments": [...],
  "unmatched_orders": [...],
  "metrics": { ... }
}
```

**After (backward compatible):**
```json
{
  "assignments": [...],
  "unmatched_orders": [...],
  "metrics": {
    "total_empty_km": 1234.5,
    "avg_empty_run_km": 45.2,
    "total_cost": 5678.9,
    "naive_total_cost": 8901.2,
    "cost_saved": 3222.3,
    "match_rate": 0.95,
    "wagons_matched": 27,
    "orders_matched": 5,
    "orders_unmatched": 1,
    "trains_formed": 3,
    "avg_train_size": 9.0,
    "loco_reposition_km": 450.0,
    "locos_used": 3
  },
  "train_groups": [
    {
      "source_station_id": "st-zaporizhzhia-uuid",
      "destination_station_id": "st-grekuvata-uuid",
      "locomotive_id": "loco-uuid-or-null",
      "wagon_ids": ["wagon-uuid-1", "wagon-uuid-2", ...],
      "order_ids": ["order-uuid-1"],
      "empty_run_km": 200.0,
      "loco_reposition_km": 150.0,
      "estimated_hours": 12.8
    }
  ]
}
```

The `train_groups` field defaults to `[]`. The 4 new metrics fields default to `0`. Go's JSON decoder ignores unknown fields, so the old Go backend works fine until updated.

---

## 5. DATA FLOW (Full Cycle Example)

### Hour 100: Order Arrives

An external client submits an order for 10 gondolas at Grekuvata (loading station, ore):
```
POST /api/orders
{
  "clientName": "Metinvest",
  "stationToId": "st-grekuvata",
  "wagonType": "gondola",
  "quantity": 10,
  "desiredDate": "2026-04-18"
}
```

The Go backend's `CreateOrderUseCase.Execute` creates the order, then triggers `match()` in a goroutine.

### Hour 100: Match Triggered

`match()` gathers:
- **Pending orders**: 1 order (10 gondolas to Grekuvata)
- **Idle wagons**: 450 total, say 12 idle gondolas at Zaporizhzhia, 8 at Dnipro, etc.
- **Idle locomotives**: 20 idle (5 are in transit), including:
  - LOCO-07 at Dnipro (150km from Zaporizhzhia)
  - LOCO-12 at Zaporizhzhia (0km, already at source)
  - LOCO-19 at Znamianka (200km from Zaporizhzhia)

The Go backend calls `matchingGateway.Match(ctx, orders, wagons, stations, edges, locos, 100)`.

### Hour 100: Python Service Processing

1. **MIP/Greedy assignment**: Scores all (wagon, order) pairs. The 12 gondolas at Zaporizhzhia score best for the Grekuvata order (Zaporizhzhia -> Grekuvata = 200km). MIP assigns 10 of them.

2. **`_group_into_trains()`**: Groups the 10 assignments into one train group `(Zaporizhzhia, Grekuvata)`. Finds nearest loco:
   - LOCO-12 at Zaporizhzhia: reposition_km = 0 (best)
   - LOCO-07 at Dnipro: reposition_km = 150km
   
   Selects LOCO-12 (0km repositioning).

3. **Response**:
```json
{
  "assignments": [10 assignments...],
  "train_groups": [{
    "source_station_id": "st-zaporizhzhia",
    "destination_station_id": "st-grekuvata",
    "locomotive_id": "LOCO-12-uuid",
    "wagon_ids": ["GND-0001", ..., "GND-0010"],
    "order_ids": ["order-uuid"],
    "empty_run_km": 200.0,
    "loco_reposition_km": 0.0,
    "estimated_hours": 5.0
  }]
}
```

### Hour 100: Go Backend Processes Response

1. Creates individual assignments (existing logic)
2. Auto-creates a Train with `LocomotiveID = LOCO-12`
3. Updates LOCO-12: `status = in_transit`, `available_at_hour = 105`, `current_station_id = st-grekuvata`
4. Updates 10 wagons to `status = in_train`
5. Publishes SSE events: `trainCreated`, `assignmentCreated` x10

### Hour 100: Alternative Scenario (Loco repositioning needed)

If LOCO-12 were not at Zaporizhzhia but LOCO-07 at Dnipro were the nearest:

1. **Reposition check**: 150km * 20 UAH/km = 3,000 UAH reposition cost
2. **Train margin**: 10 wagons * 200km * 30 UAH/km revenue = 60,000 UAH revenue; minus 10 * 200km * 3.8 UAH/km cost = 7,600 UAH cost; net margin = 52,400 UAH
3. **Economic gate**: 3,000 < 52,400 * 0.5 = 26,200. Pass.
4. **Timing**: reposition = 150/40 = 3.75h, train travel = 200/40 = 5h, total = 8.75h
5. LOCO-07 status: `repositioning`, `available_at_hour = 100 + ceil(3.75) = 104` (arrives at Zaporizhzhia)
6. At hour 104: ticker frees LOCO-07, status becomes `idle` at Zaporizhzhia. Train dispatch can proceed.
7. Train dispatches. LOCO-07 status: `in_transit`, `available_at_hour = 104 + 5 = 109`

### Hour 105 (or 109): Train Arrives at Grekuvata

The `AdvanceTrainsUseCase` detects train has arrived:
1. Updates all 10 wagons to station `st-grekuvata`, status `idle`
2. Frees LOCO-12 (or LOCO-07): status `idle`, station `st-grekuvata`
3. Publishes `trainArrived`, `locoArrived`, `wagonMoved` x10

### Hour 105+: LOCO Now at Grekuvata

LOCO is available for the next dispatch. The next matching call will include this loco at Grekuvata. If ore wagons at Grekuvata need to go to Odesa port (loading -> port flow), this loco is perfectly positioned.

---

## 6. OPTUNA RETUNE

### 6.1 Current Parameter Space

The current 8 weights (`w1`-`w8`) were tuned without locomotive repositioning costs. With locos, the cost landscape changes.

### 6.2 New Parameter Candidates

No new weights are needed. The locomotive repositioning cost enters the system in the **post-filter** (economic gate), not in the scoring function. The scoring function determines which wagons match which orders. The locomotive logic determines whether the resulting train can physically be dispatched.

However, the economic gate threshold (currently `0.5` -- skip if repositioning cost exceeds 50% of net margin) should be tuned:

```python
# In Optuna study, add:
loco_gate_threshold = trial.suggest_float("loco_gate_threshold", 0.2, 0.8)
```

### 6.3 Retune Strategy

1. **Keep w1-w8 as search space** -- same ranges as current Optuna study
2. **Add `loco_gate_threshold`** as a single new float parameter [0.2, 0.8]
3. **The Rust optimizer must pass this threshold** via the weights JSON or a separate config field
4. **Objective remains the same**: maximize profit with train cost model

Implementation in Rust `simulation.rs`:

Add to `Weights` struct in `scoring.rs`:
```rust
#[serde(default = "default_loco_gate")]
pub loco_gate: f64,  // threshold for loco repositioning economic gate

fn default_loco_gate() -> f64 { 0.5 }
```

Then in the simulation loop, use `weights.loco_gate` instead of hardcoded `0.5`:
```rust
if reposition_cost > net_margin * weights.loco_gate {
    continue;
}
```

### 6.4 Expected Impact

With physical locos (not teleporting), some dispatches that were previously free become expensive. The optimizer will:
- Value train size even more (larger trains amortize loco cost)
- Penalize matching wagons far from available locos
- Potentially hold wagons idle longer, waiting for a local loco

Expected changes: profit may decrease 5-15% from current v4 numbers (548M) because loco repositioning adds real cost. Beta ratio may improve because the system now avoids some marginal dispatches where loco cost was hidden.

---

## 7. TESTING STRATEGY

### 7.1 Unit Tests: Rust Optimizer

**Test 1: Loco Already at Source**
- Place a loco at station A, create a train group A->B
- Verify: reposition_km = 0, loco_empty_km = 0, timing = dist(A,B)/40

**Test 2: Loco Repositioning Required**
- Place a loco at station C, create a train group A->B where dist(C,A) = 100km
- Verify: reposition_km = 100, loco becomes available at `hour + ceil(100/40 + dist(A,B)/40) + 1`
- Verify: loco station after dispatch = B (not A)

**Test 3: Economic Gate Rejection**
- Place a loco at station C, create a small train group (2 wagons) A->B where dist(C,A) = 500km
- Verify: reposition cost (500 * 20 = 10,000) exceeds margin threshold -> train not dispatched

**Test 4: No Available Loco**
- All locos busy until hour 200, try to dispatch at hour 100
- Verify: no trains formed, all assignments dropped

**Test 5: Regression Test**
- Run full year simulation with teleporting locos (current), save metrics
- Run with physical locos, verify:
  - `loco_empty_km > 0`
  - `profit <= old_profit` (physical costs cannot increase profit)
  - `trains_formed <= old_trains_formed` (some trains rejected by economic gate)
  - `total_assigned` may decrease slightly

### 7.2 Integration Tests: Python Service

**Test 1: Backward Compatibility**
- Send request WITHOUT `locomotives` field
- Verify: response has `train_groups: []` (or groups without loco assignments)
- Verify: `assignments` are identical to before

**Test 2: With Locomotives**
- Send request WITH 5 locomotives at known stations
- Verify: `train_groups` have valid `locomotive_id` values from the input set
- Verify: each loco is assigned to at most one train group
- Verify: `loco_reposition_km` >= 0 for each group

**Test 3: Economic Gate**
- Send one loco very far from all wagon clusters
- Verify: it is not assigned (no group has that loco_id)

### 7.3 Integration Test: Go Backend End-to-End

**Test 1: DB Seeding**
- Verify 25 locomotives exist in DB after migration
- Verify all are `idle` status with `available_at_hour = 0`

**Test 2: Match -> Train -> Loco Flow**
- Create order
- Verify matching service receives locomotive data in request
- Verify train is auto-created with `locomotive_id` set
- Verify loco status changes to `in_transit` or `repositioning`
- Let ticker advance until train arrives
- Verify loco freed to `idle` at destination station

**Test 3: Concurrent Loco Usage**
- Create two orders simultaneously
- Verify each train gets a different loco (no double-booking)

### 7.4 Cross-System Consistency

**Metric comparison across all three systems** for the same 8760-hour simulation:

| Metric | Rust Optimizer | Python (mock 8760h) | Go + Python (live) |
|--------|---------------|--------------------|--------------------|
| trains_formed | X | ~X (within 5%) | ~X (within 10%) |
| loco_empty_km | Y | ~Y | ~Y |
| fulfillment_pct | Z | ~Z | ~Z |

The Rust optimizer is the ground truth (deterministic). Python and Go will have slight variations because:
- Python processes one batch at a time (not a 6-hour LP window like Rust)
- Go ticker has 10s real-time ticks vs hourly simulation steps

---

## 8. TIMELINE AND DEPENDENCIES

### Phase 1: Rust Optimizer (You, 1-2 days)
**No dependencies on Misha.**

| Task | Time | Details |
|------|------|---------|
| Expand `Locomotive` struct | 30 min | Add metrics fields |
| Replace teleport with nearest-loco | 2 h | New selection logic, economic gate |
| Add reposition timing to wagon ETA | 1 h | Affects `busy_until` calculation |
| Add loco metrics to `SimResult` | 1 h | 5 new fields |
| Add `loco_gate` to `Weights` | 30 min | For Optuna tuning |
| Unit tests | 2 h | 5 test scenarios |
| Run full benchmark comparison | 1 h | Before/after teleport vs physical |

### Phase 2: Python Matching Service (You, 1 day)
**No dependencies on Misha. Can be done in parallel with Phase 1.**

| Task | Time | Details |
|------|------|---------|
| Add models to `models.py` | 30 min | `LocomotiveIn`, `TrainGroup`, extend request/response |
| Implement `_group_into_trains()` | 2 h | Nearest-loco, economic gate, min train size |
| Modify `match()` function | 30 min | Call grouping, populate metrics |
| Integration tests | 1 h | Backward compat + with locos |
| Deploy to matching service | 30 min | No breaking changes, safe to deploy |

### Phase 3: Go Backend (Misha, 2-3 days)
**Depends on Phase 2 being deployed (so the service accepts loco data).**

| Task | Time | Owner | Details |
|------|------|-------|---------|
| `entity/locomotive.go` | 30 min | Misha | New entity |
| DB migration | 30 min | Misha | Create table, seed 25 rows, alter trains |
| `repository/locomotive.go` | 1 h | Misha | CRUD + `FreeExpired` |
| Update `usecase/interfaces.go` | 30 min | Misha | New interface + update `MatchingGateway` |
| Update `gateway/matching.go` | 1 h | Misha | New DTOs, extend request/response |
| Update `usecase/create_order.go` | 2 h | Misha | Pass locos, process train_groups, reserve locos |
| Update `ticker/ticker.go` | 30 min | Misha | `FreeExpired` call |
| Update `train_usecases.go` | 1 h | Misha | Free loco on train arrival |
| Update `entity/train.go` + repo | 1 h | Misha | `LocomotiveID` field |
| Add controllers + routes | 1 h | Misha | GET /api/locomotives |
| Wire in `main.go` | 30 min | Misha | Dependencies |
| SSE events | 30 min | Misha | New event types |
| End-to-end test | 2 h | Misha + You | Full cycle verification |

### Phase 4: Optuna Retune (You, 1 day)
**Depends on Phase 1.**

| Task | Time | Details |
|------|------|---------|
| Add `loco_gate` to Optuna search space | 30 min | One new float parameter |
| Run 200-trial study (8760h each) | 4-8 h | Rust optimizer, 15s per trial |
| Analyze results, update `TUNED_WEIGHTS` | 1 h | In both Python and Rust |

### Critical Path

```
Phase 1 (Rust) ──────┬──── Phase 4 (Optuna)
                     │
Phase 2 (Python) ────┴──── Phase 3 (Go Backend) ──── End-to-End Test
```

**Phases 1 and 2 are independent and can run in parallel.** Phase 3 depends on Phase 2 (service must accept loco data before backend sends it). Phase 4 depends on Phase 1 (optimizer must have physical locos before tuning weights).

### What Misha Needs From You

1. The API contract change (section 4) as a spec document
2. The Python service deployed with the new optional fields
3. The `TrainGroupResult` struct definition so he can parse it
4. Confirmation that sending `locomotives: []` is safe (backward compat)

### What You Need From Misha

1. DB migration executed on GCP PostgreSQL
2. The `MatchingGateway.Match` signature update (breaking change in Go, needs coordination)
3. Train creation logic that respects locomotive assignments

---

### Critical Files for Implementation

- `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-dev/optimizer_rust/src/simulation.rs` - Core Rust simulation loop: replace teleporting locos with nearest-loco dispatch, economic gate, reposition timing, new metrics. Lines 68-72 (struct), 153-158 (init), 378-414 (dispatch logic), 108-124 (results).
- `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-matching/algos/matching/models.py` - Python API contract: add `LocomotiveIn`, `TrainGroup`, extend `MatchRequest` and `MatchResponse`. This is the shared interface between Python and Go.
- `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-matching/algos/matching/matcher.py` - Python matching logic: add `_group_into_trains()` function with nearest-loco selection and economic gate; modify `match()` to populate train groups.
- `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-backend/src/gateway/matching.go` - Go backend gateway: extend DTOs, update `Match` method signature to accept/return locomotive data. This is the integration point between Go and Python.
- `/Users/masakra/python-some-projects/randomcode/hackaton_uz/erb-backend/src/usecase/create_order.go` - Go backend order flow: add `locomotiveRepository` dependency, pass locos to gateway, auto-create trains from train groups, reserve locomotives. Lines 129-194 (`match()` method) is the primary change target.