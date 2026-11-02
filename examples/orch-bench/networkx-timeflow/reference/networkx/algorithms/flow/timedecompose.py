from networkx.algorithms.flow.lowerbound import _int
from networkx.algorithms.flow.timeflow import expand
from networkx.algorithms.flow.timeio import canonical_json
from networkx.algorithms.flow.timeschedule import _order, _record


def paths(G, horizon, balances, schedule, storage=None):
    H = expand(G, horizon, balances, storage)
    if not isinstance(schedule, dict) or set(schedule) != {"cost", "flow"}:
        raise ValueError("invalid schedule")
    _int(schedule["cost"])
    if not isinstance(schedule["flow"], list):
        raise ValueError("flow must be a list")
    lookup, remaining, records, net = {}, {}, {}, {n: 0 for n in H}
    for u, v, key in H.edges(keys=True):
        record = _record(u, v, key, 0)
        ident = _order(record)
        lookup[ident] = (u, v, key)
        remaining[ident] = 0
        records[ident] = record
    seen = set()
    for record in schedule["flow"]:
        if not isinstance(record, dict) or set(record) != {
                "kind", "source", "target", "key", "depart", "arrive", "amount"}:
            raise ValueError("invalid arc record")
        amount = _int(record["amount"], 1)
        _int(record["depart"], 0)
        _int(record["arrive"], 0)
        if (record["kind"] not in ("move", "hold")
                or not isinstance(record["source"], str) or not isinstance(record["target"], str)
                or not (record["key"] is None or isinstance(record["key"], str))):
            raise ValueError("invalid arc identifiers")
        ident = _order(record)
        if ident not in lookup or ident in seen or records[ident] != {**record, "amount": 0}:
            raise ValueError("unknown/duplicate arc")
        seen.add(ident)
        remaining[ident] = amount
    cost = 0
    for ident, (u, v, key) in lookup.items():
        data, amount = H[u][v][key], remaining[ident]
        if not data["lower"] <= amount <= data["capacity"]:
            raise ValueError("bound violation")
        net[u] -= amount
        net[v] += amount
        cost += amount * data["weight"]
    if cost != schedule["cost"] or any(net[n] != H.nodes[n]["demand"] for n in H):
        raise ValueError("cost/balance mismatch")
    supplies = {n: -d for n, d in H.nodes(data="demand") if d < 0}
    demands = {n: d for n, d in H.nodes(data="demand") if d > 0}
    adjacent = {n: sorted(i for i, edge in lookup.items() if edge[0] == n) for n in H}
    result = []
    for start in sorted(supplies, key=lambda n: (n[1], n[0])):
        while supplies[start]:
            node, route, selected = start, [list(start)], []
            while not demands.get(node, 0):
                arc = next((i for i in adjacent[node] if remaining[i]), None)
                if arc is None:
                    raise ValueError("unroutable residual flow")
                selected.append(arc)
                node = lookup[arc][1]
                route.append(list(node))
            amount = min(supplies[start], demands[node], *(remaining[i] for i in selected))
            supplies[start] -= amount
            demands[node] -= amount
            for arc in selected:
                remaining[arc] -= amount
            result.append({"amount": amount, "route": route,
                           "edges": [{"kind": records[i]["kind"], "key": records[i]["key"]}
                                     for i in selected]})
    if any(remaining.values()):
        raise ValueError("unaccounted residual flow")
    return sorted(result, key=canonical_json)
