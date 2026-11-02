import argparse
import json
import sys
from pathlib import Path

import networkx as nx
from networkx.algorithms.flow.timeaudit import analyze
from networkx.algorithms.flow.timedecompose import paths
from networkx.algorithms.flow.timeio import canonical_json, save_json
from networkx.algorithms.flow.timeschedule import solve


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["solve", "audit"])
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        data = json.loads(Path(args.input).read_text())
        canonical_json(data)
        if not isinstance(data, dict) or set(data) - {
                "nodes", "edges", "horizon", "balances", "storage", "scenarios"}:
            raise ValueError("invalid input fields")
        nodes = data["nodes"]
        if (not isinstance(nodes, list) or any(not isinstance(n, str) or not n for n in nodes)
                or len(set(nodes)) != len(nodes)):
            raise ValueError("invalid nodes")
        graph = nx.MultiDiGraph()
        graph.add_nodes_from(nodes)
        seen = set()
        if not isinstance(data["edges"], list) or not isinstance(data["balances"], list):
            raise ValueError("edge/balance lists required")
        for row in data["edges"]:
            if (not isinstance(row, dict)
                    or not {"source", "target", "key", "capacity"} <= row.keys()
                    or set(row) - {"source", "target", "key", "capacity",
                                   "weight", "lower", "transit"}):
                raise ValueError("invalid edge fields")
            u, v, key = row["source"], row["target"], row["key"]
            if (u not in graph or v not in graph or not isinstance(key, str) or not key
                    or (u, v, key) in seen):
                raise ValueError("invalid/duplicate edge")
            seen.add((u, v, key))
            graph.add_edge(u, v, key=key, **{k: v for k, v in row.items()
                                           if k not in {"source", "target", "key"}})
        balances = {}
        for row in data["balances"]:
            if not isinstance(row, dict) or set(row) != {"node", "time", "demand"}:
                raise ValueError("invalid balance record")
            position = (row["node"], row["time"])
            if position in balances:
                raise ValueError("duplicate balance")
            balances[position] = row["demand"]
        horizon, storage = data["horizon"], data.get("storage")
        if args.command == "audit":
            result = analyze(graph, horizon, balances, data.get("scenarios", {}), storage)
        else:
            schedule = solve(graph, horizon, balances, storage)
            result = {"schedule": schedule,
                      "shipments": paths(graph, horizon, balances, schedule, storage)}
        save_json(args.output, result)
        return 0
    except (KeyError, TypeError, ValueError, OSError, nx.NetworkXException) as error:
        print(str(error), file=sys.stderr)
        return 2
