"""Graph construction and Dijkstra shortest paths."""
import heapq
from collections import defaultdict

from matching.models import EdgeIn

BACKEND = "python"


def build_graph(edges: list[EdgeIn]) -> dict[str, list[tuple[str, float]]]:
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        adj[e.from_station_id].append((e.to_station_id, e.distance_km))
        adj[e.to_station_id].append((e.from_station_id, e.distance_km))
    return dict(adj)


def dijkstra(adj, source):
    dist = {source: 0.0}
    prev = {source: None}
    heap = [(0.0, source)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, float("inf")):
            continue
        for v, w in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, prev


def reconstruct_path(prev, target):
    path = []
    cur = target
    while cur is not None:
        path.append(cur)
        cur = prev.get(cur)
    path.reverse()
    return path


def all_pairs_shortest(adj, station_ids):
    dist_matrix = {}
    prev_matrix = {}
    for sid in station_ids:
        dist_matrix[sid], prev_matrix[sid] = dijkstra(adj, sid)
    return dist_matrix, prev_matrix
