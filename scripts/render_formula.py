"""Render the Homebrew formula for a release from its wheel.

    python3 scripts/render_formula.py dist/orchestragent-0.1.0-py3-none-any.whl > orchestragent.rb
"""

import hashlib
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "packaging" / "homebrew" / "orchestragent.rb.in"


def render(wheel: Path) -> str:
    match = re.fullmatch(r"orchestragent-(\d+\.\d+\.\d+)-py3-none-any\.whl", wheel.name)
    if match is None:
        raise SystemExit(f"not an orchestragent release wheel: {wheel.name}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return (TEMPLATE.read_text().replace("@VERSION@", match.group(1))
            .replace("@SHA256@", digest))


if __name__ == "__main__":
    sys.stdout.write(render(Path(sys.argv[1])))
