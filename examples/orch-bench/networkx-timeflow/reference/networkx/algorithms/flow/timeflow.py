import networkx as nx
from networkx.algorithms.flow.lowerbound import _arcs, _int


def expand(G, horizon, balances, storage=None):
    _int(horizon, 0)
    arcs = _arcs(G)
    if any(not isinstance(n, str) or not n for n in G):
        raise ValueError("string node IDs required")
    if G.is_multigraph() and any(not isinstance(a[2], str) or not a[2] for a in arcs):
        raise ValueError("string keys required")
    if not isinstance(balances, dict) or storage is not None and not isinstance(storage, dict):
        raise ValueError("balance/storage mappings required")
    H = nx.MultiDiGraph(horizon=horizon)
    H.add_nodes_from(((n, tick), {"demand": 0}) for n in G for tick in range(horizon + 1))
    for position, value in balances.items():
        if (not isinstance(position, tuple) or len(position) != 2
                or type(position[1]) is not int or position not in H):
            raise ValueError("unknown balance position")
        H.nodes[position]["demand"] = _int(value)
    if sum(nx.get_node_attributes(H, "demand").values()):
        raise ValueError("unbalanced demands")
    for u, v, key, cap, weight, lower in arcs:
        data = G[u][v][key] if G.is_multigraph() else G[u][v]
        transit = _int(data.get("transit", 1), 1)
        for tick in range(horizon - transit + 1):
            H.add_edge((u, tick), (v, tick + transit), key=("move", u, v, key, tick),
                       capacity=cap, weight=weight, lower=lower)
    for node, record in (storage or {}).items():
        if node not in G or not isinstance(record, dict) or set(record) - {
                "capacity", "weight", "lower"}:
            raise ValueError("invalid storage")
        cap, weight, lower = (_int(record.get("capacity"), 0),
                              _int(record.get("weight", 0)), _int(record.get("lower", 0), 0))
        if lower > cap:
            raise ValueError("invalid storage lower bound")
        for tick in range(horizon):
            H.add_edge((node, tick), (node, tick + 1), key=("hold", node, tick),
                       capacity=cap, weight=weight, lower=lower)
    return H
