# ERB Matching Service

Wagon-to-order matching algorithm for Empty Run Buster. Minimizes total empty run distance using Dijkstra shortest paths + greedy assignment by urgency.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Quick Start

```bash
cd algos
uv run uvicorn main:app --port 9393
```

Service runs at `http://localhost:9393`. API docs at `http://localhost:9393/docs`.

## REST API Contract

### `POST /api/match`

Match idle wagons to pending orders. Returns optimal assignments with cost metrics.

#### Request

```json
{
    "orders": [
        {
            "order_id": "string (uuid)",
            "station_to_id": "string (uuid, destination station)",
            "wagon_type": "string (gondola | grain_hopper | cement_hopper)",
            "quantity": "integer (number of wagons needed)",
            "desired_date": "string (ISO date, e.g. 2026-04-10)"
        }
    ],
    "wagons": [
        {
            "wagon_id": "string (uuid)",
            "wagon_number": "string (human-readable, e.g. GND-0042)",
            "wagon_type": "string (gondola | grain_hopper | cement_hopper)",
            "current_station_id": "string (uuid, where the wagon is now)",
            "idle_days": "float (days since last operation)"
        }
    ],
    "stations": [
        {
            "station_id": "string (uuid)",
            "name": "string",
            "type": "string (sorting | port | border | freight)",
            "lat": "float",
            "lng": "float"
        }
    ],
    "edges": [
        {
            "from_station_id": "string (uuid)",
            "to_station_id": "string (uuid)",
            "distance_km": "float"
        }
    ]
}
```

#### Response

```json
{
    "assignments": [
        {
            "order_id": "string",
            "wagon_id": "string",
            "wagon_number": "string",
            "route": ["station_id_1", "station_id_2", "..."],
            "empty_run_km": "float",
            "cost_empty_run": "float (UAH, = empty_run_km * 20)",
            "estimated_hours": "float (= empty_run_km / 40)"
        }
    ],
    "unmatched_orders": [
        {
            "order_id": "string",
            "reason": "string (no_available_wagons_of_type | insufficient_wagons_of_type)"
        }
    ],
    "metrics": {
        "total_empty_km": "float (sum of all assignment distances)",
        "avg_empty_run_km": "float (total / wagons_matched)",
        "total_cost": "float (UAH, optimized)",
        "naive_total_cost": "float (UAH, first-fit baseline)",
        "cost_saved": "float (UAH, naive - optimized)",
        "match_rate": "float (0.0-1.0, orders_matched / total_orders)",
        "wagons_matched": "integer",
        "orders_matched": "integer",
        "orders_unmatched": "integer"
    }
}
```

#### Notes

- One order with `quantity: N` produces up to N separate assignments (one per wagon)
- `route` is the full shortest path through the station graph (Dijkstra)
- `cost_empty_run = empty_run_km * 20 UAH/km` (from task spec)
- `estimated_hours = empty_run_km / 40 km/h` (avg freight train speed)
- `naive_total_cost` uses first-fit assignment (no distance optimization) as baseline
- Orders are prioritized by `desired_date` (earliest deadline first)
- Edges are bidirectional (undirected graph)

### `GET /health`

Returns `{"status": "ok"}`.

## Algorithm

1. Build undirected weighted graph from stations + edges
2. Compute all-pairs shortest paths (Dijkstra)
3. Sort orders by `desired_date` (most urgent first)
4. For each order: assign N nearest idle wagons of matching type
5. Compute naive baseline for cost comparison
6. Return assignments + unmatched + metrics

## Project Structure

```
algos/
├── main.py              # FastAPI app entry point
├── matching/            # matching algorithm package
│   ├── __init__.py
│   ├── models.py        # Pydantic request/response models
│   ├── graph.py         # Dijkstra + shortest paths
│   ├── matcher.py       # Greedy matching engine
│   ├── naive.py         # Naive baseline for comparison
│   └── cost.py          # Cost and ETA formulas
├── tests/
│   └── test_request.json
├── pyproject.toml
└── README.md
```
