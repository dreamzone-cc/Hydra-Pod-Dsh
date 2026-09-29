#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Make Hydra-Pod available inside DeepSeek Harness (dsh).
# Links two skills into the dsh user skill root ($DSH_HOME/skills, default ~/.dsh/skills):
#   hydra-pod     -> this checkout's skills/hydra-pod   (the /hydra-pod entry point)
#   opus-manager  -> this checkout's skills/opus-manager (a loader for the vendored
#                    skill in $HYDRA_POD_HOME/skill/opus-manager, whose frontmatter dsh rejects)
# Links, not copies: edits in either checkout need no reinstall.
# usage: install.sh [--force]
# Without --force an existing, different entry is reported and left alone.
# With --force it is first moved to $DSH_HOME/backups/hydra-pod-dsh/<timestamp>/.
# Backups must live outside skills/: dsh scans every entry there, so a backup
# copy would show up as a duplicate skill.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
force=${1:-}
if [ -n "$force" ] && [ "$force" != "--force" ]; then
  echo "install.sh: unknown option '$force' (only --force is accepted)" >&2
  exit 2
fi
dsh_home=${DSH_HOME:-$HOME/.dsh}
hp=${HYDRA_POD_HOME:-$HOME/Hydra-Pod}
skills=$dsh_home/skills
backup_dir=$dsh_home/backups/hydra-pod-dsh/$(date +%Y%m%d-%H%M%S)-$$
missing=0
declined=0

say() { printf '%-8s %s\n' "$1" "$2"; }

link_item() {  # src dest
  local src=$1 dst=$2
  if [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$src")" ]; then
    say same "$dst"; return
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if [ "$force" != --force ]; then
      say differs "$dst (rerun with --force to replace; a backup is kept)"
      declined=1
      return
    fi
    mkdir -p "$backup_dir"; mv "$dst" "$backup_dir/"; say backup "$backup_dir/$(basename "$dst")"
  fi
  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst"; say install "$dst -> $src"
}

if [ ! -f "$hp/skill/opus-manager/SKILL.md" ]; then
  say MISSING "$hp/skill/opus-manager (clone Hydra-Pod there or set HYDRA_POD_HOME)"; exit 1
fi
link_item "$here/skills/hydra-pod" "$skills/hydra-pod"
link_item "$here/skills/opus-manager" "$skills/opus-manager"
# The status command the skill and the dsh-hydra-pod plugin call.
link_item "$here/bin/hydra-pod-dsh" "${HYDRA_POD_BIN_DIR:-$HOME/.local/bin}/hydra-pod-dsh"

# What the skill needs at run time; reported, never installed from here.
for tool in dsh hydra-pod-dispatch hydra-pod-connect opencode python3; do
  if command -v "$tool" >/dev/null 2>&1; then say ok "$tool"; else say MISSING "$tool on PATH"; missing=1; fi
done
# hydra-pod-dispatch points workers at this receipt template (Hydra-Pod's own install.sh puts it there).
receipt=$HOME/.claude/skills/opus-manager/templates/receipt.md
if [ -f "$receipt" ]; then say ok "$receipt"; else say MISSING "$receipt (run $hp/scripts/install.sh)"; missing=1; fi

[ "$missing" = 0 ] || echo "note     fix the MISSING items above before the first /hydra-pod run"
echo "done. dsh watches its skill roots: /hydra-pod appears in new sessions without a restart."
exit "$declined"
