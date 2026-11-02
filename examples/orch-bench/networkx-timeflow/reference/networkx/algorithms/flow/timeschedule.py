from networkx.algorithms.flow.lowerbound import solve_bounded_flow
from networkx.algorithms.flow.timeflow import expand


def _order(record):
    return (record["depart"], record["arrive"], record["source"], record["target"],
            record["kind"], record["key"] or "")


def _record(u, v, key, amount):
    return {"kind": key[0], "source": u[0], "target": v[0],
            "key": key[3] if key[0] == "move" else None,
            "depart": u[1], "arrive": v[1], "amount": amount}


def solve(G, horizon, balances, storage=None):
    H = expand(G, horizon, balances, storage)
    cost, flow = solve_bounded_flow(H)
    rows = [_record(u, v, key, flow[u][v][key]) for u, v, key in H.edges(keys=True)
            if flow[u][v][key] > 0]
    return {"cost": cost, "flow": sorted(rows, key=_order)}
