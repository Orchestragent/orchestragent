#!/bin/sh
# Put a self-contained orch inside Orch Studio.app: a relocatable CPython
# (python-build-standalone, via uv) with the orchestragent wheel installed into it, and a
# launcher at Contents/Resources/orch-runtime/bin/orch. The app prefers this orch, so a
# downloaded app needs neither Homebrew nor a terminal.
#
# usage: apps/macos/scripts/bundle_runtime.sh path/to/OrchStudio.app
set -eu
APP=$1
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
RUNTIME="$APP/Contents/Resources/orch-runtime"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

rm -rf "$RUNTIME"
mkdir -p "$RUNTIME/bin"

uv python install 3.12 --install-dir "$WORK/pythons"
PYHOME=$(find "$WORK/pythons" -maxdepth 1 -type d -name 'cpython-3.12*' | head -n 1)
[ -n "$PYHOME" ] || { echo "no CPython 3.12 was installed" >&2; exit 1; }
cp -R "$PYHOME" "$RUNTIME/python"
PY="$RUNTIME/python/bin/python3"

(cd "$ROOT" && uv build --wheel -o "$WORK/wheel")
uv pip install --python "$PY" --break-system-packages --no-cache "$WORK"/wheel/*.whl

# What orch never uses: the test suite, IDLE, Tk, and bytecode caches (rebuilt on demand).
LIB=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')
rm -rf "$LIB/test" "$LIB/idlelib" "$LIB/tkinter" "$LIB/turtledemo" "$LIB/ensurepip"
rm -rf "$RUNTIME/python/lib/tcl"* "$RUNTIME/python/lib/tk"* "$RUNTIME/python/lib/itcl"*
rm -f "$RUNTIME/python/bin/idle"* "$RUNTIME/python/bin/pydoc"*
find "$RUNTIME" -name __pycache__ -prune -exec rm -rf {} +

cat > "$RUNTIME/bin/orch" <<'EOS'
#!/bin/sh
# orch bundled with Orch Studio: an isolated interpreter (-I ignores PYTHONPATH and user
# site-packages), so whatever Python the user has installed never interferes.
here=$(cd "$(dirname "$0")/.." && pwd)
exec "$here/python/bin/python3" -I -m orchestragent.orch_cli "$@"
EOS
chmod 755 "$RUNTIME/bin/orch"

# Every Mach-O file is signed (ad-hoc) before the app itself is.
find "$RUNTIME" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) | while read -r file; do
  if file -b "$file" | grep -q 'Mach-O'; then
    codesign --force --sign - "$file"
  fi
done

# Works with nothing on PATH but the system's own directories, as when opened from Finder.
env -i HOME="$HOME" PATH=/usr/bin:/bin "$RUNTIME/bin/orch" --help >/dev/null
du -sh "$RUNTIME"
