# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is working right now.

Two sources, the live one first:
1. Running worker processes, found in /proc: `opencode run ... -m <model>` (the
   builder, or the reviewer when `--agent reviewer`), and `hydra-pod-connect
   review zcode-lite` (the ZCode reviewer). `hydra-pod-dispatch build|review <T>`
   parents give the ticket id. This needs no cooperation from the manager.
2. The manager's own stage, recorded with `hydra-pod-dsh stage` (planning,
   accepting, validating findings, closing). It goes stale after STAGE_TTL.
"""

import json
import os
import time
from pathlib import Path

from .usage import cache_dir

STAGE_TTL = 6 * 3600
MANAGER = "Claude Opus 5.5 (DeepSeek Harness)"


def stage_file() -> Path:
    return cache_dir() / "stage.json"


def project_stage_file(project) -> Path:
    """The fallback inside the project: the only place DSH's workspace-write sandbox lets a
    command write. `status` (run by the plugin, outside the sandbox) finds it through the
    workspace of the most recent DSH session."""
    return Path(project) / ".hydra" / "live-stage.json"


def _write(f: Path, entry: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")  # unique: two stage writers cannot clobber each other
    tmp.write_text(json.dumps(entry))
    tmp.replace(f)


def set_stage(ticket: str | None, stage: str, project: str, by: str = MANAGER, now: float | None = None) -> dict:
    entry = {"ticket": ticket, "stage": stage, "project": project, "by": by,
             "at": time.time() if now is None else now}
    try:
        _write(stage_file(), entry)
    except OSError:
        _write(project_stage_file(project), entry)   # read-only home: inside DSH's sandbox
        _ignore(project)
    return entry


def _ignore(project) -> None:
    """Keep the runtime stage out of commits (.hydra/ itself holds committed skills and memory)."""
    gi = Path(project) / ".hydra" / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if "live-stage.json" not in lines:
        gi.write_text("\n".join(lines + ["live-stage.json"]) + "\n")


def clear_stage(project=None) -> None:
    if project:
        try:
            project_stage_file(project).unlink()
        except FileNotFoundError:
            pass
    try:
        stage_file().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # read-only cache inside the sandbox: an idle marker in the project supersedes it
        if project:
            _write(project_stage_file(project), {"stage": None, "project": str(project), "at": time.time()})
            _ignore(project)


def read_stage(now: float | None = None, projects=None) -> dict | None:
    """The newest live stage among the cache and the projects' fallbacks (an idle marker wins if newer)."""
    now = time.time() if now is None else now
    if projects is None:
        from .manager_usage import latest_session_cwd
        cwd = latest_session_cwd()
        projects = [cwd] if cwd else []
    entries = []
    for f in [stage_file()] + [project_stage_file(p) for p in projects]:
        try:
            entries.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    entry = max(entries, key=lambda e: e.get("at", 0), default=None)
    if entry is None or entry.get("stage") is None:
        return None
    return entry if now - entry.get("at", 0) < STAGE_TTL else None


def _arg_after(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def classify(argv: list[str]) -> dict | None:
    """The worker a command line belongs to, or None. Pure: tested without /proc."""
    names = [os.path.basename(a) for a in argv[:3]]
    if "opencode" in names and "run" in argv:
        model = _arg_after(argv, "-m") or _arg_after(argv, "--model")
        reviewer = _arg_after(argv, "--agent") == "reviewer"
        return {"role": "reviewer" if reviewer else "builder", "tool": "opencode", "model": model}
    if "hydra-pod-connect" in names and "review" in argv and "zcode-lite" in argv:
        return {"role": "reviewer", "tool": "zcode", "model": "zai-coding-plan/glm-5.3"}
    if "hydra-pod-dispatch" in names:
        for step in ("build", "review"):
            if step in argv:
                i = argv.index(step)
                return {"role": "dispatch", "step": step, "ticket": argv[i + 1] if i + 1 < len(argv) else None}
    return None


def _procs(proc: Path = Path("/proc")):
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            cwd = os.readlink(d / "cwd")
            started = (d / "cmdline").stat().st_mtime
        except OSError:
            continue
        yield int(d.name), [a.decode(errors="replace") for a in argv if a], cwd, started


def running_workers(procs=None) -> list[dict]:
    """Workers found running, each with the ticket its dispatch parent names (if any)."""
    workers, dispatches, seen = [], [], set()
    for pid, argv, cwd, started in (procs if procs is not None else _procs()):
        c = classify(argv)
        if c is None:
            continue
        key = (c["role"], c.get("tool"), c.get("model"), c.get("step"), cwd)
        if key in seen:  # a tool's own child processes repeat its command line
            continue
        seen.add(key)
        c.update(pid=pid, cwd=cwd, since=started)
        (dispatches if c["role"] == "dispatch" else workers).append(c)
    for w in workers:
        match = [d for d in dispatches if d["cwd"] == w["cwd"]]
        if match:
            w["ticket"] = match[0]["ticket"]
            w["step"] = match[0]["step"]
    # A dispatch whose worker has not started yet still tells who is about to work.
    for d in dispatches:
        if not any(w.get("ticket") == d["ticket"] and w["cwd"] == d["cwd"] for w in workers):
            workers.append({"role": "builder" if d["step"] == "build" else "reviewer", "tool": "hydra-pod-dispatch",
                            "model": None, "ticket": d["ticket"], "step": d["step"], "pid": d["pid"],
                            "cwd": d["cwd"], "since": d["since"]})
    return workers
