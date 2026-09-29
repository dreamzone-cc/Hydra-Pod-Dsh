"""Manager usage from DSH session logs (architecture §16 rev 2: the Manager row).

DSH stores each session as `$DSH_HOME/sessions/<cwd-key>/<session-id>/session.v<N>.jsonl.zstd`.
Every `assistant/message` event carries `data.usage` and `data.message.source`
({provider, model}). A session counts toward a workflow when it invoked the
`/hydra-pod` skill and its workspace is the project (or contains it and the log
names the project path). Only messages inside the workflow's time span count.

Read-only; tolerant of a session that is being written (a truncated final frame
or line is skipped). Cost is computed only when `pricing.json` lists the model:
otherwise it stays unknown, never guessed.
"""

import json
import os
from pathlib import Path

from . import tokens

try:
    from compression import zstd  # Python 3.14+
except ImportError:  # pragma: no cover - older Python
    zstd = None

PRICING_FILE = Path(__file__).resolve().parent.parent / "pricing.json"
SKILL_MARK = '<skill_content name=\\"hydra-pod\\"'


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")


def _lines(path: Path):
    raw = path.read_bytes()
    if path.suffix == ".zstd":
        if zstd is None:
            return []
        # Appends add frames; a session being written can end mid-frame.
        out, rest = [], raw
        while rest:
            d = zstd.ZstdDecompressor()
            try:
                out.append(d.decompress(rest))
            except zstd.ZstdError:
                break
            if not d.eof:
                break
            rest = d.unused_data
        raw = b"".join(out)
    return raw.decode("utf-8", errors="replace").splitlines()


def session_files(home: Path | None = None) -> list[Path]:
    root = (home or dsh_home()) / "sessions"
    return sorted(root.glob("*/*/session.v*.jsonl*")) if root.exists() else []


def scan(path: Path, project: str) -> dict | None:
    """Usage records of one session if it is a Hydra-Pod session for `project`.

    Memoized per file version and project: `status` reads the manager rows of
    every workflow, and they all share one project, so each session file is
    decompressed and parsed once per run, not once per workflow.
    """
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, project)
    except OSError:
        return _scan(path, project)
    if key not in _scan_cache:
        _scan_cache[key] = _scan(path, project)
    return _scan_cache[key]


_scan_cache: dict[tuple, dict | None] = {}


def _scan(path: Path, project: str) -> dict | None:
    lines = _lines(path)
    if not lines:
        return None
    try:
        header = json.loads(lines[0])
    except ValueError:
        return None
    cwd = header.get("cwd") or ""
    text_has_project = False
    uses_skill = False
    records = []
    for line in lines[1:]:
        if SKILL_MARK in line:
            uses_skill = True
        if project in line:
            text_has_project = True
        if '"assistant/message"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue  # the line being written
        data = e.get("data") or {}
        usage = data.get("usage") or {}
        src = (data.get("message") or {}).get("source") or {}
        if usage:
            records.append({"time": e.get("time", 0) / 1000, "provider": src.get("provider"),
                            "model": src.get("model"), "usage": usage})
    in_scope = cwd == project or (project.startswith(cwd.rstrip("/") + "/") and text_has_project)
    if not (uses_skill and in_scope):
        return None
    return {"session": header.get("id"), "cwd": cwd, "records": records}


def pricing() -> dict:
    try:
        return {k: v for k, v in json.loads(PRICING_FILE.read_text()).items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def records_for(project: str, since: float, until: float | None = None, home: Path | None = None) -> dict:
    """Every manager message in the span: {sessions, records[time, provider, model, tokens]}."""
    sessions, records = [], []
    for f in session_files(home):
        s = scan(f, str(project))
        if s is None:
            continue
        sessions.append(s["session"])
        for r in s["records"]:
            if r["time"] < since or (until is not None and r["time"] > until):
                continue
            records.append({"time": r["time"], "provider": r["provider"], "model": r["model"],
                            "tokens": tokens.normalize(r["usage"])})
    records.sort(key=lambda r: r["time"])
    return {"sessions": sessions, "records": records}


def usage_for(project: str, since: float, until: float | None = None, home: Path | None = None) -> dict:
    """Manager tokens per provider/model for one project and time span."""
    prices = pricing()
    rows: dict[tuple, dict] = {}
    got = records_for(project, since, until, home)
    for r in got["records"]:
        key = (r["provider"], r["model"])
        row = rows.setdefault(key, {"provider": key[0], "model": key[1], "messages": 0, "tokens": tokens.zero()})
        row["messages"] += 1
        row["tokens"] = tokens.add(row["tokens"], r["tokens"])
    for row in rows.values():
        t = row["tokens"]
        row.update({f: t[f] for f in tokens.FIELDS})  # flat copies for older readers
        row["work_tokens"] = tokens.work(t)
        row["cost_usd"] = cost(prices, row["provider"], row["model"], t)
    return {"sessions": got["sessions"], "by_model": list(rows.values())}


def cost(prices: dict, provider, model, t: dict) -> float | None:
    """USD from pricing.json (per million tokens), or None when the model has no price."""
    p = prices.get(f"{provider}/{model}")
    if p is None:
        return None
    return round(sum(t[f] * p.get(f, p.get("output", 0) if f == "reasoning" else 0) for f in tokens.FIELDS) / 1e6, 6)
