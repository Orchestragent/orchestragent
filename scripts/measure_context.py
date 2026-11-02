"""Compare committed context packers on deterministic byte-budget fixtures.

PYTHONPATH=src uv run python scripts/measure_context.py BASE CANDIDATE --output FILE
Measures retained source and bytes, not tokens, model intelligence or end-to-end speed.
Both revisions must be trusted local commits: their Python modules are executed.
"""

import argparse
import importlib.util
import platform
import re
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from orchestragent.envelope import digest
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def load(revision: str, directory: Path) -> ModuleType:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Pass full commit SHAs")
    source = Workspace.git(ROOT, "show", f"{revision}:src/orchestragent/orch_build.py",
                           strip=False)
    name = f"_measured_context_{revision}"
    path = directory / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load committed context packer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists")
    task = OrchTask("T-001", "Compute a transformed sum", ["R1"], ["calc.py"], [],
                    ["test_calc"], "Use the supplied public dependency interfaces")
    owned = {"calc.py": "def calc(x: int) -> int:\n    raise NotImplementedError\n"}
    tests = {"test_calc.py": "import unittest\nfrom calc import calc\n"}
    dependencies = {
        "large.py::lookup": "def lookup(x):\n    return {\n"
        + "".join(f"        {n}: {n + 1},\n" for n in range(200)) + "    }[x]\n",
        "small.py::increment": "def increment(x):\n    return x + 1\n",
        "unicode.py::label": "def label():\n    return '边界 café 📦'\n",
        "small.py::double": "def double(x):\n    return x * 2\n",
    }
    rows = []
    with tempfile.TemporaryDirectory(prefix="orch-context-measure-") as directory:
        modules = [load(revision, Path(directory))
                   for revision in (args.baseline, args.candidate)]
        for retry in (("", ""), ("previous diff", "failed boundary case")):
            for budget in (2100, 2400, 3000, 12000):
                packs = [module.context_pack(task, {"R1": "transform sum"}, "calc -> small",
                                             owned, owned, tests, budget, retry,
                                             dependencies=dependencies) for module in modules]
                measures = []
                for pack in packs:
                    kept = {name: content for name, content in dependencies.items()
                            if f"--- {name} ---\n{content}\n" in pack.task}
                    measures.append({"prompt_bytes": len(pack.text.encode()),
                                     "retained_snippets": len(kept),
                                     "retained_source_bytes": sum(len(s.encode())
                                                                  for s in kept.values()),
                                     "within_budget": len(pack.text.encode()) <= budget,
                                     "prefix_hash": pack.prefix_hash})
                rows.append({"budget_bytes": budget, "retry": bool(retry[1]),
                             "baseline": measures[0], "candidate": measures[1],
                             "same_prefix": packs[0].shared == packs[1].shared,
                             "identical_prompt": packs[0].text == packs[1].text})
    result = {"baseline": args.baseline, "candidate": args.candidate,
              "script_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
              "fixture_hash": digest(dumps(dependencies)), "python": platform.python_version(),
              "measurement": "deterministic context bytes; not model performance", "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps(result) + "\n")
    print(dumps(result))


if __name__ == "__main__":
    main()
