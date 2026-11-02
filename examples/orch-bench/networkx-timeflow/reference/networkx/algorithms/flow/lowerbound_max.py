import networkx as nx
from networkx.algorithms.flow.lowerbound import _arcs, _int, solve_bounded_flow


def max_flow(G, s, t, capacity="capacity", weight="weight", lower="lower"):
    arcs = _arcs(G, capacity, weight, lower)
    if s not in G or t not in G or s == t or any(_int(G.nodes[n].get("demand", 0)) for n in G):
        raise ValueError("distinct terminals and zero demands required")
    H = nx.MultiDiGraph()
    H.add_nodes_from(G)
    for u, v, k, cap, _, bound in arcs:
        H.add_edge(u, v, key=k, capacity=cap, weight=0, lower=bound)
    bridge = object()
    total = sum(row[3] for row in arcs)
    H.add_edge(t, bridge, key="bridge", capacity=total)
    H.add_edge(bridge, s, key="bridge", capacity=total)
    _, feasible = solve_bounded_flow(H)
    low, high = feasible[t][bridge]["bridge"], sum(row[3] for row in arcs if row[0] == s)

    def solve(value):
        probe = G.copy()
        nx.set_node_attributes(probe, 0, "demand")
        probe.nodes[s]["demand"], probe.nodes[t]["demand"] = -value, value
        return solve_bounded_flow(probe, capacity=capacity, weight=weight, lower=lower)[1]

    while low < high:
        middle = (low + high + 1) // 2
        try:
            solve(middle)
            low = middle
        except nx.NetworkXUnfeasible:
            high = middle - 1
    return solve(low)
