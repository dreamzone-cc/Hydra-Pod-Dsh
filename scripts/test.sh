#!/usr/bin/env bash
# Run every test suite. usage: scripts/test.sh   (exit status 1 when any suite fails)
set -uo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
fail=0

echo "== python =="
python3 -B -m unittest discover -s "$here/tests" || fail=1

echo "== node =="
node --test "$here/tests/plugin.test.mjs" || fail=1

exit "$fail"
