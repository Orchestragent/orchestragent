#!/bin/sh
# Build dist/orch-linux.tar.gz: the tracked sources plus hash-checked tree-sitter wheels
# for x86_64 and aarch64 (CPython 3.12 and 3.13), an installer and a launcher.
#   scripts/package_linux.sh            # from a clean checkout; needs uv and network
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
if [ -n "$(git status --porcelain)" ]; then
  echo "package_linux: commit or stash changes first (the package is built from HEAD)" >&2
  exit 1
fi
revision="$(git rev-parse --short HEAD)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/orch-linux"
mkdir -p "$stage/wheels"
# Tracked files only: no .data, no keys, no virtualenv, no caches.
git archive HEAD src scripts docs examples pyproject.toml uv.lock README.md AGENTS.md \
  | tar -x -C "$stage"
cp packaging/linux/install.sh packaging/linux/orch packaging/linux/QUICKSTART.md "$stage/"
uv export --only-group index --no-emit-project --format requirements-txt --quiet \
  > "$stage/wheels/requirements.txt"
for platform in x86_64 aarch64; do
  for python in 3.12 3.13; do
    python3 -m pip download --quiet --disable-pip-version-check --only-binary=:all: \
      --no-deps --require-hashes -r "$stage/wheels/requirements.txt" \
      --dest "$stage/wheels" --python-version "$python" --implementation cp \
      --platform "manylinux_2_28_$platform" --platform "manylinux_2_17_$platform" \
      --platform "manylinux2014_$platform" --platform "manylinux_2_5_$platform" \
      --platform "manylinux1_$platform"
  done
done
printf '%s\n' "$revision" > "$stage/REVISION"
mkdir -p dist
COPYFILE_DISABLE=1 tar -czf dist/orch-linux.tar.gz -C "$work" orch-linux
(cd dist && sha256sum orch-linux.tar.gz | tee orch-linux.tar.gz.sha256)
echo "built dist/orch-linux.tar.gz from $revision ($(du -h dist/orch-linux.tar.gz | cut -f1))"
