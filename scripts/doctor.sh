#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check that Hydra-Pod is ready to run inside DeepSeek Harness. Spends no quota.
# usage: doctor.sh   (exit status 1 when a required item fails)
set -uo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
dsh_home=${DSH_HOME:-$HOME/.dsh}
hp=${HYDRA_POD_HOME:-$HOME/Hydra-Pod}
fail=0

say() { printf '%-6s %s\n' "$1" "$2"; }
need() {  # label command...
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then say ok "$label"; else say FAIL "$label"; fail=1; fi
}

if command -v dsh >/dev/null 2>&1; then
  say ok "dsh $(dsh --version 2>/dev/null | tail -1)"
else
  say FAIL "dsh not on PATH"; fail=1
fi
need "node on PATH (plugin and skill checks)" command -v node
if command -v python3 >/dev/null 2>&1; then
  py=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null)
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' 2>/dev/null; then
    say ok "python3 $py (3.14+: DSH session logs are read)"
  else
    say WARN "python3 $py is older than 3.14: DSH session logs (.zstd) cannot be read, so manager tokens, the manager-token budget and manager stage rows report nothing"
  fi
else
  say FAIL "python3 not on PATH"; fail=1
fi
need "skill hydra-pod linked in $dsh_home/skills" test "$(readlink -f "$dsh_home/skills/hydra-pod")" = "$here/skills/hydra-pod"
need "skill opus-manager linked in $dsh_home/skills" test "$(readlink -f "$dsh_home/skills/opus-manager")" = "$here/skills/opus-manager"
need "hydra-pod-dsh on PATH" command -v hydra-pod-dsh
need "plugin dsh-hydra-pod in the web profile" test -e "$dsh_home/profiles/web/node_modules/dsh-hydra-pod/package.json"
need "hydra-pod-dispatch on PATH" command -v hydra-pod-dispatch
need "hydra-pod-connect on PATH" command -v hydra-pod-connect
need "opencode on PATH" command -v opencode
need "vendored skill $hp/skill/opus-manager/SKILL.md" test -f "$hp/skill/opus-manager/SKILL.md"
need "receipt template ~/.claude/skills/opus-manager/templates/receipt.md" test -f "$HOME/.claude/skills/opus-manager/templates/receipt.md"
need "agent registry passes policy (hydra-pod-dsh policy)" hydra-pod-dsh policy
need "skill frontmatter valid" node "$here/scripts/check-skill.mjs" "$here"/skills/*/SKILL.md

# dsh is pre-stable: warn when the running version is not the one this checkout was tested on.
pin=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$here/dsh-pin.json" 2>/dev/null)
have=$(dsh --version 2>/dev/null | tail -1)
if [ -n "$pin" ] && [ -n "$have" ] && [ "$have" != "$pin" ]; then
  say WARN "dsh $have is not the tested pin $pin (dsh-pin.json): rerun the tests before relying on it"
fi
# Policy §37a: consumer OAuth bridged into dsh is out of policy for the manager.
for bad in dsh-claude-oauth; do
  if [ -e "$dsh_home/profiles/web/node_modules/$bad/package.json" ]; then
    say FAIL "plugin $bad is installed: it bridges Claude Pro OAuth into dsh (remove: dsh plugin --profile web remove $bad)"; fail=1
  fi
done
if [ -e "$dsh_home/profiles/web/node_modules/dsh-oauth-login/package.json" ]; then
  say WARN "dsh-oauth-login is installed: never pick its pi-anthropic (Claude Pro OAuth) model for the manager"
fi

case "${DSH_PERMISSION_MODE:-workspace-write}" in
  danger-full-access) say note "DSH_PERMISSION_MODE=danger-full-access: dispatch runs without approval prompts" ;;
  *) say note "sandbox ${DSH_PERMISSION_MODE:-workspace-write}: expect one approval per dispatch call (see README)" ;;
esac

echo
hydra-pod-dsh status 2>&1 | sed 's/^/  /' || true
echo
echo "providers (no quota used):"
hydra-pod-connect status </dev/null 2>&1 | sed 's/^/  /' || true
exit "$fail"
