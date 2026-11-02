"""Frozen public-behaviour oracle; installed only after each agent has finished."""

import copy
import hashlib
import importlib
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import networkx as nx


def module(name):
    return importlib.import_module("networkx.deployment." + name)


class DeploymentAcceptance(unittest.TestCase):
    def setUp(self):
        self.hosts = {
            "a": {"zone": "z1", "slots": 2},
            "b": {"zone": "z2", "slots": 2},
            "c": {"zone": "z3", "slots": 2},
        }
        self.services = {
            "db": {
                "replicas": 1,
                "requires": [],
                "allowed_zones": None,
                "costs": {"a": 1, "b": 4, "c": 5},
            },
            "api": {
                "replicas": 2,
                "requires": ["db"],
                "allowed_zones": None,
                "costs": {"a": 2, "b": 1, "c": 3},
            },
            "worker": {
                "replicas": 1,
                "requires": [],
                "allowed_zones": None,
                "costs": {"a": 3, "b": 3, "c": 1},
            },
        }
        self.network = nx.Graph()
        self.network.add_weighted_edges_from([("a", "b", 1), ("a", "c", 3), ("b", "c", 1)])

    def model(self, services=None, hosts=None, network=None):
        return module("model").Model(
            self.services if services is None else services,
            self.hosts if hosts is None else hosts,
            self.network if network is None else network,
        )

    def plan(self, **kwargs):
        return module("planner").compile_plan(self.model(), **kwargs)

    def test_r1_normalized_snapshots_and_whole_model_validation(self):
        model = self.model()
        self.assertEqual(len(model.fingerprint), 64)
        self.services["api"]["requires"].append("db")
        reordered = dict(reversed(list(self.services.items())))
        self.assertEqual(model.fingerprint, self.model(reordered).fingerprint)
        changed = model.services
        changed["db"]["replicas"] = 100
        model.network.remove_node("a")
        self.assertEqual(model.services["db"]["replicas"], 1)
        self.assertIn("a", model.network)
        empty = self.model({}, {}, nx.Graph())
        expected = b'{"hosts":{},"network":{"edges":[],"nodes":[]},"services":{}}'
        self.assertEqual(empty.fingerprint, hashlib.sha256(expected).hexdigest())
        self.assertNotEqual(empty.fingerprint, model.fingerprint)
        self.assertEqual(module("model").canonical_json({"z": 1, "a": "雪"}), '{"a":"雪","z":1}')
        for value in (0, True, -1, "2"):
            bad = copy.deepcopy(self.services)
            bad["db"]["replicas"] = value
            with self.subTest(replicas=value), self.assertRaises(ValueError):
                self.model(bad)
        for deps in (["missing"], ["api"], ["worker"]):
            bad = copy.deepcopy(self.services)
            bad["db"]["requires"] = deps
            if deps == ["worker"]:
                bad["worker"]["requires"] = ["db"]
            with self.subTest(deps=deps), self.assertRaises(ValueError):
                self.model(bad)

    def test_r2_global_cost_capacity_and_failure_domain_constraints(self):
        placement = module("placement").place(self.model())
        self.assertEqual(
            placement, {"placements": {"api": ["a", "b"], "db": ["a"], "worker": ["c"]}, "cost": 5}
        )
        small = {
            "first": {
                "replicas": 1,
                "requires": [],
                "allowed_zones": None,
                "costs": {"a": 1, "b": 2},
            },
            "second": {
                "replicas": 1,
                "requires": [],
                "allowed_zones": None,
                "costs": {"a": 1, "b": 100},
            },
        }
        hosts = {"a": {"zone": "x", "slots": 1}, "b": {"zone": "y", "slots": 1}}
        network = nx.path_graph(["a", "b"])
        nx.set_edge_attributes(network, 1, "weight")
        result = module("placement").place(self.model(small, hosts, network))
        self.assertEqual(result, {"placements": {"first": ["b"], "second": ["a"]}, "cost": 3})
        same_zone = {key: {"zone": "same", "slots": 10} for key in self.hosts}
        with self.assertRaises(nx.NetworkXUnfeasible):
            module("placement").place(self.model(hosts=same_zone))
        self.assertEqual(
            module("placement").place(self.model({}, {}, nx.Graph())), {"placements": {}, "cost": 0}
        )

    def test_r3_minimum_cost_undirected_edge_disjoint_routing(self):
        graph = nx.Graph()
        graph.add_weighted_edges_from(
            [("a", "b", 1), ("b", "d", 1), ("a", "c", 2), ("c", "d", 2), ("b", "c", 1)]
        )
        model = self.model(network=graph)
        paths = module("routing").route(model, "a", "d", 2)
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths, sorted(paths))
        used = set()
        cost = 0
        for path in paths:
            self.assertEqual((path[0], path[-1], len(path)), ("a", "d", len(set(path))))
            for left, right in zip(path, path[1:], strict=False):
                edge = frozenset((left, right))
                self.assertNotIn(edge, used)
                used.add(edge)
                cost += graph[left][right]["weight"]
        self.assertEqual(cost, 6)
        self.assertEqual(module("routing").route(model, "a", "a", 9), [["a"]])
        with self.assertRaises(nx.NetworkXUnfeasible):
            module("routing").route(model, "a", "d", 3)
        for k in (0, True, -1):
            with self.subTest(k=k), self.assertRaises(ValueError):
                module("routing").route(model, "a", "d", k)
        self.assertTrue(nx.utils.graphs_equal(graph, model.network))

    def test_r4_composed_batches_keep_dependencies_and_scan_past_nonfitting_work(self):
        placements = module("placement").place(self.model())["placements"]
        self.assertEqual(
            module("batches").rollout_batches(self.model(), placements, 3),
            [["db", "worker"], ["api"]],
        )
        services = {
            name: {
                "replicas": replicas,
                "requires": [],
                "allowed_zones": None,
                "costs": {host: 1 for host in self.hosts},
            }
            for name, replicas in (("a", 2), ("b", 1), ("c", 1))
        }
        prescribed = {"a": ["a", "b"], "b": ["a"], "c": ["c"]}
        self.assertEqual(
            module("batches").rollout_batches(self.model(services), prescribed, 3),
            [["a", "c"], ["b"]],
        )
        with self.assertRaises(ValueError):
            module("batches").rollout_batches(self.model(), placements, 1)
        bad = {**placements, "api": ["a", "a"]}
        with self.assertRaises(ValueError):
            module("batches").rollout_batches(self.model(), bad)

    def test_r5_atomic_cas_idempotence_corruption_and_concurrent_writers(self):
        api = module("journal")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            journal = api.Journal(path)
            self.assertEqual(journal.read(), {"schema": 1, "revision": 0, "events": []})
            self.assertFalse(path.exists())
            event = {"id": "event:雪", "payload": {"nested": [1, 2]}}
            self.assertEqual(journal.append(0, event), 1)
            original = path.read_bytes()
            self.assertEqual(api.Journal(path).append(0, copy.deepcopy(event)), 1)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(api.ConflictError):
                journal.append(0, {"id": "other", "payload": None})
            with self.assertRaises(api.ConflictError):
                journal.append(1, {"id": event["id"], "payload": "different"})
            with self.assertRaises(ValueError):
                journal.append(1, {"id": "bad", "payload": float("nan")})
            with patch("os.replace", side_effect=OSError("disk fault")), self.assertRaises(OSError):
                journal.append(1, {"id": "good", "payload": True})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                {p.name for p in Path(directory).iterdir()}, {"state.json", "state.json.lock"}
            )
            state = journal.read()
            state["events"][0]["event"]["payload"]["nested"].append(99)
            self.assertEqual(journal.read()["events"][0]["event"]["payload"]["nested"], [1, 2])
            race = Path(directory) / "race.json"

            def append(index):
                try:
                    return api.Journal(race).append(0, {"id": str(index), "payload": index})
                except api.ConflictError:
                    return None

            with ThreadPoolExecutor(max_workers=4) as pool:
                replies = list(pool.map(append, range(4)))
            self.assertEqual(replies.count(1), 1)
            self.assertEqual(api.Journal(race).read()["revision"], 1)
            path.write_text('{"schema":1,"revision":2,"events":[]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                api.Journal(path).read()

    def test_r6_compiled_plan_joins_all_algorithms_and_fingerprints_full_payload(self):
        plan = self.plan(path_redundancy=2, max_parallel=3)
        self.assertEqual(
            set(plan),
            {
                "schema",
                "id",
                "deployment",
                "dependencies",
                "placements",
                "cost",
                "batches",
                "routes",
                "path_redundancy",
                "max_parallel",
            },
        )
        payload = {key: value for key, value in plan.items() if key != "id"}
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.assertEqual(plan["id"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((plan["cost"], plan["batches"]), (5, [["db", "worker"], ["api"]]))
        self.assertEqual(
            [(r["producer"], r["consumer"], r["source"], r["target"]) for r in plan["routes"]],
            [("db", "api", "a", "a"), ("db", "api", "a", "b")],
        )
        self.assertEqual(plan["routes"][0]["paths"], [["a"]])
        self.assertEqual(len(plan["routes"][1]["paths"]), 2)
        disconnected = nx.empty_graph(["a", "b", "c"])
        with self.assertRaises(nx.NetworkXUnfeasible):
            module("planner").compile_plan(self.model(network=disconnected))
        for controls in (
            {"path_redundancy": 0},
            {"path_redundancy": True},
            {"max_parallel": 0},
            {"max_parallel": True},
        ):
            with self.subTest(controls=controls), self.assertRaises(ValueError):
                module("planner").compile_plan(self.model({}, {}, nx.Graph()), **controls)

    def test_r7_durable_leases_fencing_retry_expiry_and_parallel_reconciliation(self):
        api, storage = module("controller"), module("journal")
        plan = self.plan(max_parallel=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.json"
            controller = api.Controller(plan, storage.Journal(path))
            self.assertEqual(controller.snapshot(0)["revision"], 0)
            self.assertFalse(path.exists())
            db = controller.claim("w1", 0, 2)
            self.assertEqual(db, {"service": "db", "worker": "w1", "token": 1, "expires": 2})
            self.assertEqual(controller.claim("w1", 0, 2), db)
            worker = controller.claim("w2", 0, 2)
            self.assertTrue(controller.ack(worker, True, 1))
            self.assertFalse(controller.ack(worker, True, 999))
            with self.assertRaises(storage.ConflictError):
                controller.ack(worker, False, 1)
            reopened = api.Controller(plan, storage.Journal(path))
            renewed = reopened.claim("w3", 2, 2)
            self.assertEqual((renewed["service"], renewed["token"]), ("db", 4))
            with self.assertRaises(storage.ConflictError):
                reopened.ack(db, True, 2)
            self.assertTrue(reopened.ack(renewed, True, 3))
            lease = reopened.claim("w4", 3, 2)
            self.assertTrue(reopened.ack(lease, False, 3))
            retry = reopened.claim("w5", 4, 2)
            self.assertEqual((retry["service"], retry["token"]), ("api", 8))
            self.assertIsNone(reopened.claim("w6", 6, 2))
            state = reopened.snapshot(6)
            self.assertEqual(state["states"], {"api": "failed", "db": "done", "worker": "done"})
            self.assertEqual(state["attempts"], {"api": 2, "db": 2, "worker": 1})
            self.assertEqual((state["leases"], state["revision"]), ({}, 8))
            with self.assertRaises(ValueError):
                api.Controller(plan, storage.Journal(path), max_attempts=9)
            race_path = Path(directory) / "parallel.json"

            def claim(index):
                candidate = api.Controller(plan, storage.Journal(race_path))
                for _ in range(8):
                    try:
                        return candidate.claim(str(index), 0, 2)
                    except storage.ConflictError:
                        pass
                self.fail("bounded caller retry did not resolve four-way contention")

            with ThreadPoolExecutor(max_workers=4) as pool:
                claims = [value for value in pool.map(claim, range(4)) if value is not None]
            self.assertEqual({item["service"] for item in claims}, {"db", "worker"})
            self.assertEqual(len({item["token"] for item in claims}), 2)
            broken_path = Path(directory) / "expired.json"
            broken = api.Controller(plan, storage.Journal(broken_path), max_attempts=1)
            broken.claim("w1", 0, 2)
            other = broken.claim("w2", 0, 2)
            broken.ack(other, True, 1)
            self.assertIsNone(broken.claim("w3", 2, 2))
            self.assertEqual(
                broken.snapshot(2)["states"], {"api": "blocked", "db": "failed", "worker": "done"}
            )
            extra = copy.deepcopy(self.services)
            extra["extra"] = {
                "replicas": 1,
                "requires": ["worker"],
                "allowed_zones": None,
                "costs": {"a": 10, "b": 10, "c": 1},
            }
            independent = module("planner").compile_plan(self.model(extra), max_parallel=3)
            continued = api.Controller(
                independent, storage.Journal(Path(directory) / "branches.json"), max_attempts=1
            )
            continued.claim("lost", 0, 2)
            completed = continued.claim("live", 0, 2)
            continued.ack(completed, True, 1)
            survivor = continued.claim("live", 2, 2)
            self.assertEqual(survivor["service"], "extra")
            continued.ack(survivor, True, 3)
            self.assertEqual(
                continued.snapshot(3)["states"],
                {"api": "blocked", "db": "failed", "extra": "done", "worker": "done"},
            )

    def test_r8_cli_uses_real_planner_and_durable_controller_without_losing_files(self):
        cli = module("cli")

        def invoke(arguments, expected=0):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(arguments)
            self.assertEqual(code, expected, err.getvalue())
            if code == 0:
                return json.loads(out.getvalue())
            self.assertTrue(err.getvalue().strip())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, journal, lease_file = [
                root / name for name in ("input.json", "plan.json", "journal.json", "lease.json")
            ]
            source.write_text(
                json.dumps(
                    {
                        "services": self.services,
                        "hosts": self.hosts,
                        "network": {
                            "nodes": list(self.network),
                            "edges": [
                                [a, b, data["weight"]]
                                for a, b, data in self.network.edges(data=True)
                            ],
                        },
                    }
                )
            )
            plan = invoke(["plan", str(source), str(output)])
            self.assertEqual(plan, json.loads(output.read_text()))
            state = invoke(["status", str(output), str(journal), "--now", "0"])
            self.assertEqual(state["revision"], 0)
            self.assertFalse(journal.exists())
            lease = invoke(["claim", str(output), str(journal), "w", "--now", "0", "--ttl", "5"])
            lease_file.write_text(json.dumps(lease))
            args = ["ack", str(output), str(journal), str(lease_file), "--now", "1", "--success"]
            self.assertTrue(invoke(args))
            self.assertFalse(invoke(args))
            original = journal.read_bytes()
            lease_file.write_text(json.dumps({**lease, "token": lease["token"] + 99}))
            invoke(args, 2)
            self.assertEqual(journal.read_bytes(), original)
            preserved = output.read_bytes()
            source.write_text('{"services":{},"hosts":{},"network":null}')
            invoke(["plan", str(source), str(output)], 2)
            self.assertEqual(output.read_bytes(), preserved)
