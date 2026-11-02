"""Independent reference: lower-bound reduction into upstream network simplex."""

import networkx as nx


def _int(value, minimum=None):
    if type(value) is not int or minimum is not None and value < minimum:
        raise ValueError("exact integer required")
    return value


def _arcs(G, capacity="capacity", weight="weight", lower="lower"):
    if not G.is_directed():
        raise ValueError("directed graph required")
    if any(not isinstance(a, str) or not a for a in (capacity, weight, lower)):
        raise ValueError("attribute name required")
    rows = (G.edges(keys=True, data=True) if G.is_multigraph() else
            ((u, v, None, d) for u, v, d in G.edges(data=True)))
    result = []
    for u, v, k, data in rows:
        cap, cost, bound = (_int(data.get(capacity), 0), _int(data.get(weight, 0)),
                            _int(data.get(lower, 0), 0))
        if bound > cap:
            raise ValueError("lower exceeds capacity")
        result.append((u, v, k, cap, cost, bound))
    return result


def solve_bounded_flow(G, demand="demand", capacity="capacity", weight="weight", lower="lower"):
    if not isinstance(demand, str) or not demand:
        raise ValueError("demand attribute required")
    arcs = _arcs(G, capacity, weight, lower)
    needs = {n: _int(G.nodes[n].get(demand, 0)) for n in G}
    if sum(needs.values()):
        raise nx.NetworkXUnfeasible("demands do not sum to zero")
    H = nx.MultiDiGraph() if G.is_multigraph() else nx.DiGraph()
    H.add_nodes_from(G)
    offset = 0
    for u, v, k, cap, cost, bound in arcs:
        needs[u] += bound
        needs[v] -= bound
        attrs = {"c": cap - bound, "w": cost}
        if G.is_multigraph():
            H.add_edge(u, v, key=k, **attrs)
        else:
            H.add_edge(u, v, **attrs)
        offset += cost * bound
    nx.set_node_attributes(H, needs, "d")
    if not H:
        return 0, {}
    cost, residual = nx.network_simplex(H, demand="d", capacity="c", weight="w")
    flow = {u: {} for u in G}
    for u, v, k, _, _, bound in arcs:
        if G.is_multigraph():
            flow[u].setdefault(v, {})[k] = residual[u][v][k] + bound
        else:
            flow[u][v] = residual[u][v] + bound
    return cost + offset, flow
