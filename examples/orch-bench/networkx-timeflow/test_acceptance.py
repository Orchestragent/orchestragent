"""Held-out composition checks; small optima use enumeration, not lower-bound reduction."""

import copy
import importlib
import inspect
import io
import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import networkx as nx


def module(name):
    return importlib.import_module("networkx.algorithms.flow." + name)


def arcs(graph):
    return (list(graph.edges(keys=True, data=True)) if graph.is_multigraph() else
            [(u, v, None, d) for u, v, d in graph.edges(data=True)])


def optimum(graph, terminals=None):
    """Independent finite integer oracle, used only on tiny acceptance fixtures."""
    edges = arcs(graph)
    best = None
    for values in itertools.product(*(range(d.get("lower", 0), d["capacity"] + 1)
                                       for _, _, _, d in edges)):
        net = {n: 0 for n in graph}
        cost = 0
        for (u, v, _, data), value in zip(edges, values, strict=True):
            net[u] -= value
            net[v] += value
            cost += value * data.get("weight", 0)
        if terminals is None:
            if any(net[n] != graph.nodes[n].get("demand", 0) for n in graph):
                continue
            candidate = (cost,)
        else:
            s, t = terminals
            if net[s] > 0 or net[t] != -net[s] or any(
                    net[n] for n in graph if n not in terminals):
                continue
            candidate = (net[s], cost)
        best = candidate if best is None or candidate < best else best
    return best


def bounded_graph():
    graph = nx.MultiDiGraph()
    graph.add_nodes_from([("s", {"demand": -3}), ("m", {"demand": 0}), ("t", {"demand": 3})])
    for u, v, k, cap, weight, lower in [
        ("s", "m", "a", 3, 2, 1), ("m", "t", "b", 3, 1, 1),
        ("s", "t", "c", 2, 6, 0), ("t", "s", "d", 1, -5, 0),
        ("s", "s", "loop", 2, -1, 0), ("m", "s", "reverse", 1, 0, 0),
    ]:
        graph.add_edge(u, v, key=k, capacity=cap, weight=weight, lower=lower)
    return graph


def temporal_graph():
    graph = nx.MultiDiGraph()
    graph.add_edge("a", "b", key="fast", capacity=1, weight=3, transit=1)
    graph.add_edge("a", "b", key="slow", capacity=1, weight=1, transit=2)
    return graph, {("a", 0): -2, ("b", 2): 2}, {
        "a": {"capacity": 1, "weight": 0}, "b": {"capacity": 1, "weight": 1}}


def custom_graph():
    graph = nx.DiGraph()
    graph.add_node("a", need=-2)
    graph.add_node("b", need=2)
    graph.add_edge("a", "b", limit=3, cost=4, floor=1)
    return graph


class TemporalFlowAcceptance(unittest.TestCase):
    def assert_flow(self, graph, flow, expected_cost, terminals=None):
        self.assertEqual(set(flow), set(graph))
        net, cost = {n: 0 for n in graph}, 0
        for u in graph:
            self.assertEqual(set(flow[u]), set(graph[u]))
        for u, v, key, data in arcs(graph):
            if graph.is_multigraph():
                self.assertEqual(set(flow[u][v]), set(graph[u][v]))
            value = flow[u][v][key] if graph.is_multigraph() else flow[u][v]
            self.assertIs(type(value), int)
            self.assertLessEqual(data.get("lower", 0), value)
            self.assertLessEqual(value, data["capacity"])
            net[u] -= value
            net[v] += value
            cost += value * data.get("weight", 0)
        self.assertEqual(cost, expected_cost)
        if terminals is None:
            self.assertEqual(net, {n: graph.nodes[n].get("demand", 0) for n in graph})
        else:
            s, t, amount = terminals
            self.assertEqual(net, {n: -amount if n == s else amount if n == t else 0
                                   for n in graph})

    def test_r1_lower_bound_optima_keys_cycles_and_strict_validation(self):
        solver = module("lowerbound").solve_bounded_flow
        graphs = [bounded_graph(), nx.DiGraph(), nx.DiGraph()]
        graphs[1].add_edge(("u", 1), ("v", 2), capacity=2, weight=4, lower=1)
        nx.set_node_attributes(graphs[1], {("u", 1): -2, ("v", 2): 2}, "demand")
        graphs[2].add_edge("x", "y", capacity=3, weight=-2)
        graphs[2].add_edge("y", "x", capacity=3, weight=0)
        for graph in graphs:
            with self.subTest(nodes=list(graph)):
                original = copy.deepcopy(graph)
                cost, flow = solver(graph)
                self.assertEqual(cost, optimum(graph)[0])
                self.assert_flow(graph, flow, cost)
                self.assertTrue(nx.utils.graphs_equal(graph, original))
        self.assertEqual(solver(nx.MultiDiGraph()), (0, {}))
        self.assertEqual(
            solver(custom_graph(), demand="need", capacity="limit", weight="cost", lower="floor"),
            (8, {"a": {"b": 2}, "b": {}}))
        for attr, value in [("capacity", True), ("capacity", 1.5), ("lower", -1),
                            ("lower", 99), ("weight", False)]:
            graph = bounded_graph()
            graph["s"]["m"]["a"][attr] = value
            with self.subTest(attr=attr, value=value), self.assertRaises(ValueError):
                solver(graph)
        graph = bounded_graph()
        graph.nodes["s"]["demand"] = True
        with self.assertRaises(ValueError):
            solver(graph)
        graph = nx.DiGraph()
        graph.add_nodes_from([("s", {"demand": -1}), ("t", {"demand": 1})])
        with self.assertRaises(nx.NetworkXUnfeasible):
            solver(graph)

    def test_r2_existing_public_api_and_legacy_defaults(self):
        graph = bounded_graph()
        expected = optimum(graph)[0]
        flow = nx.min_cost_flow(graph, lower="lower")
        self.assert_flow(graph, flow, expected)
        self.assertEqual(nx.min_cost_flow_cost(graph, lower="lower"), expected)
        self.assertEqual(nx.cost_of_flow(graph, flow), expected)
        custom = custom_graph()
        self.assertEqual(nx.min_cost_flow_cost(custom, demand="need", capacity="limit",
                                               weight="cost", lower="floor"), 8)
        self.assertEqual(nx.min_cost_flow(custom, demand="need", capacity="limit",
                                         weight="cost", lower="floor"), {"a": {"b": 2}, "b": {}})
        for function in (nx.min_cost_flow, nx.min_cost_flow_cost, nx.max_flow_min_cost):
            parameter = inspect.signature(function).parameters["lower"]
            self.assertIsNone(parameter.default)
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertTrue(hasattr(function, "__wrapped__"))
        legacy = nx.DiGraph()
        legacy.add_node("a", demand=-2)
        legacy.add_node("b", demand=2)
        legacy.add_edge("a", "b", weight=3)
        self.assertEqual(nx.min_cost_flow_cost(legacy), 6)
        self.assertEqual(nx.min_cost_flow(legacy), {"a": {"b": 2}, "b": {}})
        terminals = bounded_graph()
        nx.set_node_attributes(terminals, 0, "demand")
        value, cost = optimum(terminals, ("s", "t"))
        self.assert_flow(terminals, nx.max_flow_min_cost(terminals, "s", "t", lower="lower"),
                         cost, ("s", "t", -value))

    def test_r3_maximum_then_minimum_with_large_capacity(self):
        graph = bounded_graph()
        nx.set_node_attributes(graph, 0, "demand")
        original = copy.deepcopy(graph)
        negative_value, cost = optimum(graph, ("s", "t"))
        flow = module("lowerbound_max").max_flow(graph, "s", "t")
        self.assert_flow(graph, flow, cost, ("s", "t", -negative_value))
        self.assertTrue(nx.utils.graphs_equal(graph, original))
        large = nx.MultiDiGraph()
        unit = 10**7
        for u, v, key, weight in [("s", "t", "direct", 8), ("s", "m", "one", 1),
                                   ("m", "t", "two", 2)]:
            large.add_edge(u, v, key=key, capacity=unit, weight=weight, lower=0)
        self.assert_flow(large, module("lowerbound_max").max_flow(large, "s", "t"),
                         11 * unit, ("s", "t", 2 * unit))
        forced = nx.DiGraph()
        forced.add_edge("s", "t", limit=10, cost=2, floor=9)
        self.assertEqual(module("lowerbound_max").max_flow(
            forced, "s", "t", capacity="limit", weight="cost", lower="floor"),
            {"s": {"t": 10}, "t": {}})
        with self.assertRaises(ValueError):
            module("lowerbound_max").max_flow(graph, "s", "s")
        graph.nodes["m"]["demand"] = 1
        with self.assertRaises(ValueError):
            module("lowerbound_max").max_flow(graph, "s", "t")

    def test_r4_expansion_provenance_and_input_validation(self):
        graph, balances, storage = temporal_graph()
        graph.nodes["a"]["demand"] = "physical metadata is ignored"
        original = copy.deepcopy(graph)
        expanded = module("timeflow").expand(graph, 2, balances, {"a": storage["a"]})
        self.assertEqual(set(expanded), {(n, tick) for n in ("a", "b") for tick in range(3)})
        self.assertEqual({key for _, _, key in expanded.edges(keys=True)}, {
            ("move", "a", "b", "fast", 0), ("move", "a", "b", "fast", 1),
            ("move", "a", "b", "slow", 0), ("hold", "a", 0), ("hold", "a", 1)})
        self.assertEqual(expanded.graph["horizon"], 2)
        self.assertEqual(expanded.nodes[("a", 0)]["demand"], -2)
        self.assertEqual(expanded[("a", 0)][("b", 2)][("move", "a", "b", "slow", 0)],
                         {"capacity": 1, "weight": 1, "lower": 0})
        self.assertTrue(nx.utils.graphs_equal(graph, original))
        for horizon, needs, holds in [(True, balances, storage), (2, {("a", True): 0}, None),
                                      (2, {("missing", 0): 0}, None),
                                      (2, {("a", 0): 1}, None),
                                      (2, balances, {"a": {"capacity": True}})]:
            with self.subTest(h=horizon, needs=needs), self.assertRaises(ValueError):
                module("timeflow").expand(graph, horizon, needs, holds)
        graph["a"]["b"]["slow"]["transit"] = 0
        with self.assertRaises(ValueError):
            module("timeflow").expand(graph, 0, {}, None)

    def test_r5_global_temporal_cost_and_canonical_records(self):
        graph, balances, storage = temporal_graph()
        schedule = module("timeschedule").solve(graph, 2, balances, storage)
        self.assertEqual(set(schedule), {"cost", "flow"})
        self.assertEqual(schedule["cost"], 4)
        expanded = module("timeflow").expand(graph, 2, balances, storage)
        self.assertEqual(schedule["cost"], optimum(expanded)[0])
        self.assertEqual(schedule["flow"], [
            {"kind": "hold", "source": "a", "target": "a", "key": None,
             "depart": 0, "arrive": 1, "amount": 1},
            {"kind": "move", "source": "a", "target": "b", "key": "slow",
             "depart": 0, "arrive": 2, "amount": 1},
            {"kind": "move", "source": "a", "target": "b", "key": "fast",
             "depart": 1, "arrive": 2, "amount": 1}])
        with self.assertRaises(nx.NetworkXUnfeasible):
            module("timeschedule").solve(graph, 2, balances)
        self.assertEqual(module("timeschedule").solve(nx.DiGraph(), 0, {}), {"cost": 0, "flow": []})

    def test_r6_decomposition_consumes_intermediate_demand_and_all_arc_flow(self):
        graph, balances, storage = temporal_graph()
        simple = nx.DiGraph()
        simple.add_edge("a", "b", capacity=2, weight=1)
        simple.add_edge("b", "c", capacity=1, weight=1)
        cases = [(graph, balances, storage),
                 (simple, {("a", 0): -2, ("b", 1): 1, ("c", 2): 1}, None)]
        for incoming in [[("a", "b"), ("x", "b")], [("x", "b"), ("a", "b")]]:
            joined = nx.DiGraph()
            joined.add_edges_from([*incoming, ("b", "d"), ("b", "c")], capacity=1)
            cases.append((joined, {("a", 0): -1, ("x", 0): -1,
                                   ("c", 2): 1, ("d", 2): 1}, None))
        for graph, balances, storage in cases:
            with self.subTest(multigraph=graph.is_multigraph()):
                schedule = module("timeschedule").solve(graph, 2, balances, storage)
                original = copy.deepcopy(schedule)
                shipments = module("timedecompose").paths(graph, 2, balances, schedule, storage)
                self.assertEqual(sum(row["amount"] for row in shipments), 2)
                reconstructed = {}
                for row in shipments:
                    self.assertEqual(set(row), {"amount", "route", "edges"})
                    self.assertEqual(len(row["route"]), len(row["edges"]) + 1)
                    for left, right, edge in zip(row["route"][:-1], row["route"][1:],
                                                  row["edges"], strict=True):
                        ident = (edge["kind"], left[0], right[0], edge["key"], left[1], right[1])
                        reconstructed[ident] = reconstructed.get(ident, 0) + row["amount"]
                expected = {(r["kind"], r["source"], r["target"], r["key"],
                             r["depart"], r["arrive"]):
                            r["amount"] for r in schedule["flow"]}
                self.assertEqual(reconstructed, expected)
                self.assertEqual(shipments, sorted(shipments, key=module("timeio").canonical_json))
                shuffled = {**schedule, "flow": list(reversed(schedule["flow"]))}
                self.assertEqual(
                    module("timedecompose").paths(graph, 2, balances, shuffled, storage), shipments)
                self.assertEqual(schedule, original)
                self.assertEqual({tuple(row["route"][-1]) for row in shipments},
                                 {node for node, need in balances.items() if need > 0})
                if "x" in graph:
                    self.assertEqual({row["route"][0][0]: row["route"][-1][0] for row in shipments},
                                     {"a": "c", "x": "d"})
                for invalid in [dict(schedule, cost=schedule["cost"] + 1),
                                dict(schedule, flow=schedule["flow"] + schedule["flow"][:1])]:
                    with self.assertRaises(ValueError):
                        module("timedecompose").paths(graph, 2, balances, invalid, storage)

    def test_r7_independent_outages_and_negative_cost_delta(self):
        graph, balances, storage = temporal_graph()
        original = copy.deepcopy(graph)
        scenarios = {"slow": [["a", "b", "slow"]], "fast": [("a", "b", "fast")], "none": []}
        result = module("timeaudit").analyze(graph, 2, balances, scenarios, storage)
        self.assertEqual(result, {"baseline_cost": 4, "scenarios": {
            "fast": {"feasible": False, "cost": None, "delta": None},
            "none": {"feasible": True, "cost": 4, "delta": 0},
            "slow": {"feasible": True, "cost": 7, "delta": 3}}})
        self.assertEqual(list(result["scenarios"]), sorted(scenarios))
        self.assertTrue(nx.utils.graphs_equal(graph, original))
        forced = nx.MultiDiGraph()
        forced.add_edge("a", "b", key="pricey", capacity=1, lower=1, weight=10)
        forced.add_edge("a", "b", key="cheap", capacity=1, weight=1)
        value = module("timeaudit").analyze(forced, 1, {("a", 0): -1, ("b", 1): 1},
                                            {"remove": [("a", "b", "pricey")]})
        self.assertEqual(value["scenarios"]["remove"], {"feasible": True, "cost": 1, "delta": -9})
        with self.assertRaises(ValueError):
            module("timeaudit").analyze(graph, 2, balances,
                                        {"bad": [("a", "b", "unknown")]}, storage)

    def test_r8_cli_composition_strict_json_and_atomic_error_preservation(self):
        canonical = module("timeio").canonical_json
        self.assertEqual(canonical({"雪": [1, True], "a": 2}), '{"a":2,"雪":[1,true]}')
        for invalid in [float("nan"), float("inf"), object()]:
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                canonical(invalid)
        graph, balances, storage = temporal_graph()
        data = {"nodes": list(graph), "horizon": 2, "storage": storage,
                "edges": [{"source": u, "target": v, "key": k, **d} for u, v, k, d in arcs(graph)],
                "balances": [{"node": n, "time": t, "demand": d} for (n, t), d in balances.items()],
                "scenarios": {"outage": [["a", "b", "fast"]]}}
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.json", Path(directory) / "output.json"
            for command in ["solve", "audit"]:
                source.write_text(canonical(data))
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = module("timecli").main([command, str(source), "--output", str(target)])
                self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (0, "", ""))
                value = json.loads(target.read_text())
                self.assertEqual(target.read_text(), canonical(value) + "\n")
                if command == "solve":
                    self.assertEqual(set(value), {"schedule", "shipments"})
                    self.assertEqual(value["schedule"]["cost"], 4)
                    self.assertEqual(sum(row["amount"] for row in value["shipments"]), 2)
                else:
                    self.assertFalse(value["scenarios"]["outage"]["feasible"])
            invalid_inputs = [canonical(value) for value in [
                {**data, "extra": 1}, {**data, "edges": data["edges"] * 2},
                {**data, "balances": data["balances"] * 2}]]
            invalid_inputs.append(json.dumps({**data, "scenarios": float("nan")}))
            for invalid in invalid_inputs:
                source.write_text(invalid)
                before = target.read_bytes()
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = module("timecli").main(["solve", str(source), "--output", str(target)])
                self.assertEqual(code, 2)
                self.assertEqual(target.read_bytes(), before)
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                module("timeio").save_json(target, {"bad": float("nan")})
            self.assertEqual(target.read_bytes(), before)
