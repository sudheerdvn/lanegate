#!/usr/bin/env sh
# Verify the wheel users install, rather than the source checkout.
#
# This safeguard is intentionally POSIX-shell-only. Windows projects should use
# an equivalent Python guard instead.

set -eu

fail() {
    printf '%s\n' "packaging-smoke: $*" >&2
    exit 1
}

REPO_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/lanegate-packaging-smoke.XXXXXX") \
    || fail "could not create temporary workspace"
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup 0
trap 'cleanup; exit 1' HUP INT TERM

PYTHON=${PYTHON:-python}
DIST_DIR="$WORK_DIR/dist"
VENV_DIR="$WORK_DIR/venv"
SCRATCH_DIR="$WORK_DIR/scratch"

(
    cd "$REPO_ROOT"
    "$PYTHON" -m build --wheel --outdir "$DIST_DIR"
)

set -- "$DIST_DIR"/*.whl
[ "$1" != "$DIST_DIR/*.whl" ] || fail "wheel build produced no wheel"
[ "$#" -eq 1 ] || fail "wheel build produced more than one wheel"
WHEEL=$1

"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --no-cache-dir "$WHEEL"

# Use the venv's absolute console-script path so a source-tree entry point or
# PATH override cannot satisfy this check.
"$VENV_DIR/bin/lanegate" --version

mkdir "$SCRATCH_DIR"
(
    cd "$SCRATCH_DIR"
    "$VENV_DIR/bin/lanegate" init --defaults
)
[ -f "$SCRATCH_DIR/.lanegate.yml" ] || fail "lanegate init did not create .lanegate.yml"

printf '%s\n' "packaging-smoke: installed wheel and initialized scratch project"
