"""Hidden acceptance: R1-R20 end to end, from the repository root:

    python3 -m unittest acceptance.test_cluster

Four Rust nodes and one Go router run on loopback; the TypeScript CLI writes and reads
through the router; placement is checked against the Python planner and an independent
FNV-1a here; a node is killed, written around, restarted and read-repaired; a torn log tail
is recovered; and the Java metrics command sums the nodes' STATS.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_BIN = ROOT / "node" / "target" / "release" / "mkv-node"
ROUTER_BIN = ROOT / "router" / "bin" / "mkv-router"
NODES = ["n1", "n2", "n3", "n4"]
REPLICAS, WRITE_QUORUM, READ_QUORUM = 3, 2, 2
VALUES = {
    "plain": "v1",
    "spaced key": "a b c",
    "eq%": "=%=",
    "unicode-ключ": "значение ✓",
    "empty": "",
    "multi": "line1\nline2",
    "user:1": "u1",
    "user:2": "u2",
    "user:3": "u3",
}
UNRESERVED = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")


def encode(text: str) -> str:
    if text == "":
        return "="
    return "".join(chr(b) if b in UNRESERVED else f"%{b:02X}" for b in text.encode())


def decode(token: str) -> str:
    if token == "=":
        return ""
    out, i = bytearray(), 0
    while i < len(token):
        if token[i] == "%":
            out.append(int(token[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(ord(token[i]))
            i += 1
    return out.decode()


def fnv_rank(key: str, nodes: list[str]) -> list[str]:
    def score(node: str) -> int:
        value = 0xCBF29CE484222325
        for byte in node.encode() + b"\x00" + key.encode():
            value = ((value ^ byte) * 0x100000001B3) % (1 << 64)
        return value
    return sorted(nodes, key=lambda node: (-score(node), node.encode()))


def run(args: list[str], cwd: Path, timeout: float = 600, **kw: object) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, **kw)
    if done.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
    return done


def ready(process: subprocess.Popen[str], what: str) -> int:
    """Wait for the `READY <port>` line."""
    box: dict[str, str] = {}

    def read() -> None:
        assert process.stdout is not None
        box["line"] = process.stdout.readline()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(30)
    line = box.get("line", "")
    if not line.startswith("READY "):
        process.kill()
        raise AssertionError(f"{what} did not print READY: {line!r}")
    return int(line.split()[1])


class Cluster:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.nodes: dict[str, subprocess.Popen[str]] = {}
        self.ports: dict[str, int] = {}
        self.router: subprocess.Popen[str] | None = None
        self.router_port = 0

    def start_node(self, node: str, port: int = 0) -> None:
        process = subprocess.Popen(
            [str(NODE_BIN), "--id", node, "--port", str(port), "--dir", str(self.work / node)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.nodes[node] = process
        self.ports[node] = ready(process, f"node {node}")
        if port:
            assert self.ports[node] == port, "a restarted node must listen on its old port"

    def kill_node(self, node: str) -> None:
        process = self.nodes.pop(node)
        process.send_signal(signal.SIGKILL)
        process.wait(10)

    def start_router(self) -> None:
        spec = ",".join(f"{node}=127.0.0.1:{self.ports[node]}" for node in NODES)
        self.router = subprocess.Popen(
            [str(ROUTER_BIN), "--id", "r1", "--port", "0", "--nodes", spec,
             "--replicas", str(REPLICAS), "--write-quorum", str(WRITE_QUORUM),
             "--read-quorum", str(READ_QUORUM)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.router_port = ready(self.router, "router")

    def stop(self) -> None:
        for process in [*self.nodes.values(), *([self.router] if self.router else [])]:
            if process.poll() is None:
                process.kill()
                process.wait(10)


def ask(port: int, *lines: str) -> list[str]:
    """Send request lines on one connection; return one response line per request."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        reader = conn.makefile("r", encoding="utf-8", newline="\n")
        answers = []
        for line in lines:
            conn.sendall((line + "\n").encode())
            answers.append(reader.readline().rstrip("\r\n"))
        return answers


class AcceptanceCluster(unittest.TestCase):
    cluster: Cluster
    work: Path

    @classmethod
    def setUpClass(cls) -> None:
        run(["cargo", "build", "--release", "--offline"], ROOT / "node")
        run(["go", "build", "-o", "bin/mkv-router", "./cmd/mkv-router"], ROOT / "router")
        sources = [str(p) for p in (ROOT / "metrics" / "src").rglob("*.java")]
        shutil.rmtree(ROOT / "metrics" / "build", ignore_errors=True)
        run(["javac", "-d", "build", *sources], ROOT / "metrics")
        cls.work = Path(tempfile.mkdtemp(prefix="meridian-acceptance-"))
        cls.cluster = Cluster(cls.work)
        for node in NODES:
            cls.cluster.start_node(node)
        cls.cluster.start_router()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.stop()
        shutil.rmtree(cls.work, ignore_errors=True)

    # --- helpers --------------------------------------------------------------------------
    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            ["npx", "tsx", "src/cli.ts", "--router", f"127.0.0.1:{self.cluster.router_port}", *args],
            cwd=ROOT / "client", capture_output=True, text=True, timeout=120)
        if check and done.returncode != 0:
            raise AssertionError(f"cli {args} failed ({done.returncode}): {done.stderr}")
        return done

    def getv(self, node: str, key: str) -> list[str]:
        return ask(self.cluster.ports[node], f"GETV {encode(key)}")[0].split(" ")

    def holders(self, key: str) -> set[str]:
        return {node for node in self.cluster.nodes if self.getv(node, key)[0] in {"VALUE", "TOMB"}}

    # --- tests (run in name order) ------------------------------------------------------------
    def test_01_values_round_trip_through_the_cli(self) -> None:
        for key, value in VALUES.items():
            self.assertEqual(self.cli("put", key, value).stdout, "1\n", key)
        for key, value in VALUES.items():
            self.assertEqual(self.cli("get", key).stdout, value + "\n", key)

    def test_02_versions_and_deletes(self) -> None:
        self.assertEqual(self.cli("put", "plain", "v2").stdout, "2\n")
        self.assertEqual(self.cli("get", "plain").stdout, "v2\n")
        self.assertEqual(self.cli("del", "plain").stdout, "3\n")
        missing = self.cli("get", "plain", check=False)
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        self.assertIn("not found", missing.stderr)
        self.assertEqual(self.cli("put", "plain", "back").stdout, "4\n")

    def test_03_entries_sit_exactly_on_their_replicas(self) -> None:
        sys.path.insert(0, str(ROOT / "planner"))
        try:
            from meridian_planner.hrw import rank
        finally:
            sys.path.pop(0)
        for key in VALUES:
            expected = fnv_rank(key, NODES)[:REPLICAS]
            self.assertEqual(rank(key, NODES)[:REPLICAS], expected, key)
            self.assertEqual(self.holders(key), set(expected), key)
            value = self.getv(expected[0], key)
            self.assertEqual(value[0], "VALUE")
            self.assertEqual(value[2], "r1")  # the router is the writer

    def test_04_scan_through_the_router(self) -> None:
        self.assertEqual(self.cli("scan", "user:", "2").stdout, "user:1\nuser:2\n")
        self.assertEqual(self.cli("scan", "user:", "10").stdout, "user:1\nuser:2\nuser:3\n")
        everything = self.cli("scan", "", "100").stdout.splitlines()
        self.assertEqual(everything, sorted(everything, key=str.encode))
        self.assertIn("plain", everything)

    def test_05_bad_requests_keep_the_connection(self) -> None:
        answers = ask(self.cluster.router_port, "PUT onlykey", "GET %ZZ", "FROB x",
                      f"GET {encode('user:1')}")
        for answer in answers[:3]:
            self.assertTrue(answer.startswith("ERR BADREQ"), answer)
        self.assertEqual(answers[3].split(" ")[0::2], ["VALUE", "u1"])

    def test_06_a_down_replica_is_written_around_then_repaired(self) -> None:
        key = next(k for k in (f"repair-{i}" for i in range(100))
                   if "n4" in fnv_rank(k, NODES)[:REPLICAS])
        self.assertEqual(self.cli("put", key, "before").stdout.strip(), "1")
        self.assertEqual(self.getv("n4", key), ["VALUE", "1", "r1", "before"])
        port = self.cluster.ports["n4"]
        self.cluster.kill_node("n4")
        self.assertEqual(self.cli("put", key, "after").stdout, "2\n")
        self.assertEqual(self.cli("get", key).stdout, "after\n")
        self.cluster.start_node("n4", port)
        # durable: the restarted node still has what it acknowledged, nothing newer
        self.assertEqual(self.getv("n4", key), ["VALUE", "1", "r1", "before"])
        self.assertEqual(self.cli("get", key).stdout, "after\n")
        self.assertEqual(self.getv("n4", key), ["VALUE", "2", "r1", "after"])  # read repair

    def test_07_a_torn_log_tail_is_recovered(self) -> None:
        port = self.cluster.ports["n2"]
        stored = {key for key in VALUES if "n2" in fnv_rank(key, NODES)[:REPLICAS]}
        before = {key: self.getv("n2", key) for key in stored}
        self.cluster.kill_node("n2")
        log = self.work / "n2" / "log"
        size = log.stat().st_size
        with log.open("ab") as handle:
            handle.write(b"\x00\x00\x01\x00PUTV torn")
        self.cluster.start_node("n2", port)
        self.assertEqual({key: self.getv("n2", key) for key in stored}, before)
        self.assertEqual(log.stat().st_size, size)

    def test_08_metrics_sum_the_nodes_stats(self) -> None:
        lines = [ask(self.cluster.ports[node], "STATS")[0] for node in NODES]
        fields = []
        for node, line in zip(NODES, lines):
            tokens = line.split(" ")
            self.assertEqual(tokens[0], "STATS", line)
            names = [token.split("=", 1)[0] for token in tokens[1:]]
            self.assertEqual(names, ["node", "keys", "puts", "dels", "gets", "scans", "lat"], line)
            parsed = dict(token.split("=", 1) for token in tokens[1:])
            self.assertEqual(parsed["node"], node)
            self.assertEqual(len(parsed["lat"].split(",")), 8)
            fields.append(parsed)
        first = self.work / "stats-a.txt"
        second = self.work / "stats-b.txt"
        # an outdated line for n1 first; the last line per node wins
        stale = lines[0].replace(f"puts={fields[0]['puts']}", "puts=999999")
        first.write_text(stale + "\n" + "\n".join(lines[:2]) + "\n")
        second.write_text("\n".join(lines[2:]) + "\n")
        done = run(["java", "-cp", "metrics/build", "meridian.metrics.Main", str(first),
                    str(second)], ROOT)
        result = dict(token.split("=", 1) for token in done.stdout.strip().split(" "))
        self.assertEqual(list(result), ["nodes", "keys", "puts", "dels", "gets", "scans",
                                        "p50_us", "p99_us"])
        self.assertEqual(result["nodes"], "4")
        for name in ("keys", "puts", "dels", "gets", "scans"):
            self.assertEqual(int(result[name]), sum(int(f[name]) for f in fields), name)
        counts = [sum(int(f["lat"].split(",")[i]) for f in fields) for i in range(8)]
        bounds = ["100", "250", "500", "1000", "2500", "5000", "10000", "inf"]

        def percentile(p: float) -> str:
            rank, seen = -(-p * sum(counts) // 100), 0
            for count, bound in zip(counts, bounds):
                seen += count
                if seen >= rank:
                    return bound
            return "inf"

        self.assertEqual(result["p50_us"], percentile(50))
        self.assertEqual(result["p99_us"], percentile(99))

    def test_09_planner_cli_agrees_with_placement(self) -> None:
        keys = list(VALUES)
        done = run([sys.executable, "-m", "meridian_planner", "--replicas", str(REPLICAS),
                    "--from", ",".join(NODES), "--to", ",".join(NODES + ["n5"])],
                   ROOT / "planner", input="\n".join(keys) + "\n")
        moves = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([m["key"] for m in moves],
                         sorted({m["key"] for m in moves}, key=str.encode))
        expected = {}
        for key in keys:
            old = fnv_rank(key, NODES)[:REPLICAS]
            new = fnv_rank(key, NODES + ["n5"])[:REPLICAS]
            if set(old) != set(new):
                expected[key] = {"key": key, "add": sorted(set(new) - set(old)),
                                 "drop": sorted(set(old) - set(new)), "source": old[0]}
        self.assertEqual({m["key"]: m for m in moves}, expected)
        for move in moves:  # the planner's sources are the nodes that actually hold the key
            self.assertIn(move["source"], self.holders(move["key"]))


if __name__ == "__main__":
    unittest.main()
