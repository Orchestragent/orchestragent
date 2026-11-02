import networkx as nx
from networkx.algorithms.flow.timeschedule import solve


def analyze(G, horizon, balances, scenarios, storage=None):
    if not isinstance(scenarios, dict):
        raise ValueError("scenarios required")
    normalized = {}
    for name, edges in scenarios.items():
        if not isinstance(name, str) or not name or not isinstance(edges, list):
            raise ValueError("invalid scenario")
        selected = []
        for edge in edges:
            if not isinstance(edge, (tuple, list)) or len(edge) != 3:
                raise ValueError("invalid outage")
            u, v, key = edge
            if (not isinstance(u, str) or not isinstance(v, str)
                    or not (key is None or isinstance(key, str))
                    or u not in G or v not in G[u] or (not G.is_multigraph() and key is not None)
                    or G.is_multigraph() and key not in G[u][v]):
                raise ValueError("unknown outage edge")
            selected.append((u, v, key))
        normalized[name] = selected
    baseline = solve(G, horizon, balances, storage)["cost"]
    results = {}
    for name in sorted(normalized):
        graph = G.copy()
        for u, v, key in set(normalized[name]):
            if graph.is_multigraph():
                graph.remove_edge(u, v, key)
            else:
                graph.remove_edge(u, v)
        try:
            cost = solve(graph, horizon, balances, storage)["cost"]
            results[name] = {"feasible": True, "cost": cost, "delta": cost - baseline}
        except nx.NetworkXUnfeasible:
            results[name] = {"feasible": False, "cost": None, "delta": None}
    return {"baseline_cost": baseline, "scenarios": results}
