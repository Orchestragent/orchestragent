#!/bin/sh
# One-command orch demo on a fresh repository: scripts/orch_demo.sh [dir]
# Spends one Claude planning call (subscription) and a few DSF calls (cents of budget).
set -u
here="$(cd "$(dirname "$0")/.." && pwd)"
dir="${1:-$HOME/orch-demo}"
if [ -e "$dir" ]; then
  echo "$dir already exists; pass another directory" >&2
  exit 1
fi
mkdir -p "$dir" && cd "$dir" || exit 1
git init -q -b main
printf '# orch demo\n' > README.md
git add README.md
git -c user.name=orch-demo -c user.email=demo@localhost commit -q -m "seed"
"$here/scripts/orch" --repo "$dir" doctor --allow-unverified-auth || exit 1
"$here/scripts/orch" --repo "$dir" plan "$here/examples/orch-demo/spec.md" || exit $?
"$here/scripts/orch" --repo "$dir" run
code=$?
echo "report: $dir/.git/orch/*/REPORT.md   next: scripts/orch --repo $dir status|why R1|trace tokenize"
exit $code
