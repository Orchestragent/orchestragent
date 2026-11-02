"""Frozen black-box acceptance; never installed in a model's working checkout."""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from boltons.buildcacheutils import ResultStore
from boltons.buildgraphutils import BuildGraph
from boltons.buildrunutils import BuildRunner
from boltons.cacheutils import SingleFlightCache
from boltons.iterutils import dependency_layers


def node(deps=(), version="1", resources=None):
    return {"deps": list(deps), "version": version, "resources": resources or {}}


class BuildGraphAcceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "results.json"

    def threads(self, functions):
        values, errors = {}, {}

        def invoke(index, function):
            try:
                values[index] = function()
            except Exception as error:
                errors[index] = error

        threads = [threading.Thread(target=invoke, args=(i, fn), daemon=True)
                   for i, fn in enumerate(functions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads), "computation deadlocked")
        return values, errors

    def test_r1_stable_layers_and_whole_graph_validation(self):
        graph = {"join": ["left", "right", "left"], "right": ["root"],
                 "root": [], "left": ["root"], "free": []}
        original = json.loads(json.dumps(graph))
        self.assertEqual(dependency_layers(graph), [["root", "free"], ["right", "left"], ["join"]])
        self.assertEqual(graph, original)
        self.assertEqual(dependency_layers({}), [])
        for invalid in ({"a": ["missing"]}, {"a": ["a"]}, {"a": ["b"], "b": ["a"]},
                        {"a": "a"}, {"": []}, {1: []}):
            with self.subTest(graph=invalid), self.assertRaises(ValueError):
                dependency_layers(invalid)

    def test_r2_single_flight_parallel_keys_invalidation_and_failure(self):
        cache, count, value = SingleFlightCache(2), [], object()
        barrier = threading.Barrier(6)

        def request():
            barrier.wait(2)
            return cache.get_or_compute("same", lambda: (count.append(1), value)[1])

        values, errors = self.threads([request] * 6)
        self.assertFalse(errors)
        self.assertEqual(len(count), 1)
        self.assertTrue(all(item is value for item in values.values()))
        parallel = threading.Barrier(2)
        values, errors = self.threads([
            lambda: cache.get_or_compute("x", lambda: parallel.wait(2)),
            lambda: cache.get_or_compute("y", lambda: parallel.wait(2))])
        self.assertFalse(errors, "different keys were serialized")
        self.assertEqual(set(values.values()), {0, 1})
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("release not received")
            return value

        def invalidate():
            self.assertTrue(entered.wait(2))
            self.assertTrue(cache.invalidate("flight"))
            release.set()

        _, errors = self.threads([lambda: cache.get_or_compute("flight", slow), invalidate])
        self.assertFalse(errors)
        self.assertEqual(cache.get_or_compute("flight", lambda: "fresh"), "fresh")

        def broken():
            raise LookupError("broken")

        with self.assertRaisesRegex(LookupError, "broken"):
            cache.get_or_compute("error", broken)
        self.assertEqual(cache.get_or_compute("error", lambda: 0), 0)
        _, errors = self.threads([lambda: cache.get_or_compute(
            "recursive", lambda: cache.get_or_compute("recursive", lambda: 1))])
        self.assertIsInstance(errors.get(0), RuntimeError)
        cache.clear()
        self.assertEqual(cache.get_or_compute("error", lambda: 2), 2)
        for invalid in (True, 0, -1, 1.5):
            with self.subTest(size=invalid), self.assertRaises(ValueError):
                SingleFlightCache(invalid)

    def test_r3_graph_snapshots_fingerprints_and_local_impact(self):
        tasks = {"join": node(["a", "b", "a"]), "b": node(), "a": node(resources={"cpu": 1}),
                 "free": node()}
        graph = BuildGraph(tasks)
        tasks["a"]["version"] = "changed"
        spec = graph.spec("a")
        spec["resources"]["cpu"] = 9
        self.assertEqual(graph.ids, ("join", "b", "a", "free"))
        self.assertEqual(graph.layers(), [["b", "a", "free"], ["join"]])
        self.assertEqual(graph.affected(["a"]), ["join", "a"])
        self.assertEqual(graph.spec("a"), node(resources={"cpu": 1}))
        keys = {"a": "a" * 64, "b": "b" * 64}
        expected = {"id": "join", "version": "1", "resources": {},
                    "dependencies": [["a", keys["a"]], ["b", keys["b"]]]}
        wire = json.dumps(expected, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(wire.encode()).hexdigest()
        self.assertEqual(graph.fingerprint("join", keys), digest)
        for invalid in ({"a": "a" * 64}, {**keys, "extra": "0" * 64}, {**keys, "a": "A" * 64}):
            with self.subTest(keys=invalid), self.assertRaises(ValueError):
                graph.fingerprint("join", invalid)
        with self.assertRaises(ValueError):
            graph.affected(["absent"])
        with self.assertRaises(ValueError):
            BuildGraph({"a": node(resources={"cpu": True})})

    def test_r4_result_store_atomicity_isolation_and_reopen(self):
        key, second = "a" * 64, "b" * 64
        store = ResultStore(self.path)
        payload = {"items": ["雪", 1]}
        store.put(key, payload)
        payload["items"].append(99)
        self.assertFalse(self.path.exists())
        store.save()
        original = self.path.read_bytes()
        read = store.get(key)
        read["items"].clear()
        self.assertEqual(ResultStore(self.path).get(key), {"items": ["雪", 1]})
        for invalid in (float("nan"), object()):
            with self.subTest(value=repr(invalid)), self.assertRaises(ValueError):
                store.put(second, invalid)
        self.assertEqual(self.path.read_bytes(), original)
        store.put(second, None)
        with patch("boltons.fileutils.atomic_rename", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                store.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        store.invalidate([second])
        self.assertEqual(store.invalidate([key, key, second]), 1)
        with self.assertRaises(KeyError):
            store.get(key)
        store.save()
        with self.assertRaises(KeyError):
            ResultStore(self.path).get(key)
        self.path.write_text('{"schema_version":2,"values":{}}')
        malformed = self.path.read_bytes()
        with self.assertRaises(ValueError):
            ResultStore(self.path)
        self.assertEqual(self.path.read_bytes(), malformed)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_r5_parallel_execution_and_resource_release(self):
        graph = BuildGraph({"a": node(), "b": node(), "join": node(["a", "b"])})
        parallel = threading.Barrier(2)

        def independent(_):
            parallel.wait(2)
            return 3

        result = BuildRunner(graph, {"a": independent, "b": independent,
                                    "join": lambda deps: sum(deps.values())}, max_workers=2).run()
        self.assertEqual(result["results"], {"a": 3, "b": 3, "join": 6})
        self.assertEqual(result["max_active"], 2)
        constrained = BuildGraph({"bad": node(resources={"cpu": 1}),
                                  "good": node(resources={"cpu": 1})})

        def bad(_):
            raise RuntimeError("failed once")

        result = BuildRunner(constrained, {"bad": bad, "good": lambda _: 4},
                             max_workers=2, capacities={"cpu": 1}).run()
        self.assertEqual(result["states"], {"bad": "failed", "good": "done"})
        self.assertEqual(result["max_active"], 1)
        called = []
        with self.assertRaises(ValueError):
            BuildRunner(constrained, {key: lambda _: called.append(1) for key in constrained.ids},
                        capacities={"cpu": 0})
        self.assertEqual(called, [])

    def test_r6_target_closure_and_defensive_results(self):
        graph = BuildGraph({"unused": node(), "join": node(["a"]), "a": node()})
        calls = []

        def mutate(deps):
            deps["a"]["items"].append(2)
            return deps["a"]

        runner = BuildRunner(graph, {"unused": lambda _: calls.append("unused"),
                                    "a": lambda _: {"items": [1]}, "join": mutate})
        result = runner.run(["join", "join"])
        self.assertEqual(result["results"], {"join": {"items": [1, 2]}, "a": {"items": [1]}})
        self.assertEqual(list(result["states"]), ["join", "a"])
        self.assertEqual(set(result), {"results", "states", "errors", "attempts", "max_active"})
        result["results"]["a"]["items"].append(99)
        self.assertEqual(runner.run(["a"])["results"]["a"], {"items": [1]})
        self.assertEqual(runner.run([]), {"results": {}, "states": {}, "errors": {},
                                         "attempts": {}, "max_active": 0})
        for invalid in ("join", ["unknown"]):
            with self.subTest(targets=invalid), self.assertRaises(ValueError):
                runner.run(invalid)
        self.assertEqual(calls, [])

    def test_r7_retries_failures_and_blocked_components(self):
        graph = BuildGraph({"bad": node(), "blocked": node(["bad"]), "free": node()})
        calls = []

        def bad(_):
            calls.append(1)
            raise RuntimeError("broken")

        runner = BuildRunner(graph, {"bad": bad, "blocked": lambda _: self.fail("blocked ran"),
                                    "free": lambda _: False}, retries=2)
        result = runner.run()
        self.assertEqual(result["states"], {"bad": "failed", "blocked": "blocked", "free": "done"})
        self.assertEqual(result["attempts"], {"bad": 3, "blocked": 0, "free": 1})
        self.assertEqual(result["errors"], {"bad": "RuntimeError: broken"})
        self.assertEqual(result["results"], {"free": False})
        result = runner.run()
        self.assertEqual(result["states"]["free"], "cached")
        self.assertEqual(result["attempts"]["bad"], 3)
        self.assertEqual(len(calls), 6)
        invalid_attempts = []

        def json_retry(_):
            invalid_attempts.append(1)
            return float("nan") if len(invalid_attempts) == 1 else 7

        retried = BuildRunner(BuildGraph({"value": node()}), {"value": json_retry}, retries=1).run()
        self.assertEqual(retried["states"], {"value": "done"})
        self.assertEqual(retried["attempts"], {"value": 2})
        self.assertEqual(retried["results"], {"value": 7})
        self.assertFalse(retried["errors"])

    def test_r8_restart_reuses_success_and_version_change_is_local(self):
        tasks = {"a": node(), "b": node(["a"]), "free": node(), "join": node(["b", "free"])}
        counts = {key: 0 for key in tasks}

        def function(key):
            def compute(deps):
                counts[key] += 1
                return sum(deps.values()) if deps else (2 if key == "a" else 7)
            return compute

        functions = {key: function(key) for key in tasks}
        first = BuildRunner(BuildGraph(tasks), functions, ResultStore(self.path)).run()
        self.assertEqual(first["results"]["join"], 9)
        second = BuildRunner(BuildGraph(tasks), functions, ResultStore(self.path)).run()
        self.assertEqual(second["states"], dict.fromkeys(tasks, "cached"))
        self.assertEqual(second["max_active"], 0)
        self.assertEqual(second["attempts"], dict.fromkeys(tasks, 0))
        tasks["a"]["version"] = "2"
        third = BuildRunner(BuildGraph(tasks), functions, ResultStore(self.path)).run()
        self.assertEqual(third["states"], {
            "a": "done", "b": "done", "free": "cached", "join": "done"})
        self.assertEqual(counts, {"a": 2, "b": 2, "free": 1, "join": 2})
        self.assertFalse(third["errors"])
        partial_path = self.path.with_name("partial.json")
        partial = BuildGraph({"bad": node(), "child": node(["bad"]), "free": node()})

        def failed(_):
            raise RuntimeError("broken")

        first = BuildRunner(partial, {"bad": failed, "child": lambda _: 2, "free": lambda _: None},
                            ResultStore(partial_path)).run()
        self.assertEqual(first["states"], {"bad": "failed", "child": "blocked", "free": "done"})
        recovered = BuildRunner(partial, {"bad": lambda _: 1, "child": lambda _: 2,
                                         "free": lambda _: self.fail("persisted success ran")},
                                ResultStore(partial_path)).run()
        self.assertEqual(recovered["states"], {"bad": "done", "child": "done", "free": "cached"})
        self.assertEqual(recovered["results"], {"bad": 1, "child": 2, "free": None})
