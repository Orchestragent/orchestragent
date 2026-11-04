"""Hidden acceptance: R3, R14, R15 and R16 (run in planner/: python3 -m unittest test_acceptance_planner)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from meridian_planner.hrw import rank, score
from meridian_planner.plan import plan

HERE = Path(__file__).resolve().parent
NODES4 = ["n1", "n2", "n3", "n4"]
KEYS = ["alpha", "beta", "user:42", "ключ", "a b%=c", "alpha", "gamma", "delta", "user:7", "zeta"]
GROW = [
    {"key": "a b%=c", "add": ["n5"], "drop": ["n3"], "source": "n1"},
    {"key": "alpha", "add": ["n5"], "drop": ["n3"], "source": "n1"},
    {"key": "beta", "add": ["n5"], "drop": ["n4"], "source": "n1"},
    {"key": "delta", "add": ["n5"], "drop": ["n4"], "source": "n2"},
    {"key": "gamma", "add": ["n5"], "drop": ["n1"], "source": "n2"},
    {"key": "ключ", "add": ["n5"], "drop": ["n4"], "source": "n1"},
]
SHRINK = [
    {"key": "beta", "add": ["n3"], "drop": ["n4"], "source": "n1"},
    {"key": "delta", "add": ["n3"], "drop": ["n4"], "source": "n2"},
    {"key": "user:42", "add": ["n2"], "drop": ["n4"], "source": "n3"},
    {"key": "zeta", "add": ["n2"], "drop": ["n4"], "source": "n1"},
    {"key": "ключ", "add": ["n2"], "drop": ["n4"], "source": "n1"},
]


class AcceptanceHrw(unittest.TestCase):
    def test_scores(self) -> None:
        self.assertEqual(score("n1", "alpha"), 16698026282089534036)
        self.assertEqual(score("n2", "alpha"), 2484404034048581421)
        self.assertEqual(score("node-α", "ключ"), 8599979163672630911)
        self.assertEqual(score("", ""), 12638153115695167455)

    def test_rank(self) -> None:
        nodes = ["n1", "n2", "n3", "n4", "n5"]
        before = list(nodes)
        self.assertEqual(rank("alpha", nodes), ["n1", "n5", "n3", "n2", "n4"])
        self.assertEqual(rank("user:42", nodes), ["n3", "n4", "n2", "n1", "n5"])
        self.assertEqual(rank("", nodes), ["n4", "n5", "n2", "n3", "n1"])
        self.assertEqual(rank("alpha", ["n2", "n2", "n1"]), ["n1", "n2", "n2"])
        self.assertEqual(nodes, before)


class AcceptancePlan(unittest.TestCase):
    def test_growing_the_cluster(self) -> None:
        self.assertEqual(plan(KEYS, NODES4, NODES4 + ["n5"], 2), GROW)

    def test_shrinking_the_cluster(self) -> None:
        self.assertEqual(plan(KEYS, NODES4, ["n1", "n2", "n3"], 2), SHRINK)

    def test_no_surviving_source(self) -> None:
        self.assertEqual(plan(["beta", "alpha"], ["n9"], ["n1", "n2"], 1), [
            {"key": "alpha", "add": ["n1"], "drop": ["n9"], "source": None},
            {"key": "beta", "add": ["n1"], "drop": ["n9"], "source": None},
        ])

    def test_unchanged_cluster_moves_nothing(self) -> None:
        self.assertEqual(plan(KEYS, NODES4, list(reversed(NODES4)), 3), [])


class AcceptanceCli(unittest.TestCase):
    def run_cli(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "meridian_planner", *args], input=stdin,
                              capture_output=True, text=True, cwd=HERE, timeout=60)

    def test_prints_json_lines(self) -> None:
        stdin = "\n".join(KEYS) + "\n\n"
        done = self.run_cli("--replicas", "2", "--from", "n1,n2,n3,n4", "--to",
                            "n1,n2,n3,n4,n5", stdin=stdin)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "".join(json.dumps(m, sort_keys=True) + "\n" for m in GROW))

    def test_last_line_needs_no_line_ending(self) -> None:
        done = self.run_cli("--replicas", "2", "--from", "n1,n2,n3,n4", "--to", "n1,n2,n3",
                            stdin="zeta")
        self.assertEqual(done.stdout, json.dumps(SHRINK[3], sort_keys=True) + "\n")

    def test_missing_arguments_exit_2(self) -> None:
        self.assertEqual(self.run_cli("--replicas", "2", "--from", "n1").returncode, 2)


if __name__ == "__main__":
    unittest.main()
