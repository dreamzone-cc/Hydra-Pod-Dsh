"""Subscription usage for the workers, read without spending quota.

- OpenCode Go: there is no official usage API (only the web console), so usage
  is ESTIMATED from opencode's local database, which records the list-price cost
  of every assistant message. Go limits are per model, in dollars: 5 hours = 20%
  and a week = 50% of the monthly limit (https://opencode.ai/docs/go/). Windows
  are treated as rolling; the docs do not say. Only this machine's opencode use
  is counted.
- Z.ai GLM Coding Plan (also what ZCode draws on): exact, from the official quota
  endpoint through Hydra-Pod's own hydra_pod.quota. The result is cached for a
  minute so the UI can poll; the key itself is never cached or returned.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

HOUR = 3600
WINDOWS = (("5h", 5 * HOUR, 0.20), ("week", 7 * 24 * HOUR, 0.50), ("month", 30 * 24 * HOUR, 1.00))
LIMITS_FILE = Path(__file__).resolve().parent.parent / "limits.json"
ZAI_CACHE_TTL = 60


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "hydra-pod-dsh"


def opencode_db() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return Path(os.environ.get("OPENCODE_DB") or base / "opencode/opencode.db")


def monthly_limits() -> dict[str, float]:
    """{"opencode-go/<model>": monthly limit in USD} from limits.json."""
    return {k: float(v["monthly_usd"]) for k, v in json.loads(LIMITS_FILE.read_text()).items()
            if not k.startswith("_")}


def go_spend(db: Path, provider: str, model: str, since: float) -> list[tuple[float, float]]:
    """[(time, cost)] of assistant messages for one model since `since` (epoch seconds)."""
    uri = "file:" + urllib.parse.quote(str(db)) + "?mode=ro"  # quote: a path with ?/# must not break the URI
    con = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        rows = con.execute(
            "SELECT time_created, json_extract(data, '$.cost') FROM session_message"
            " WHERE json_extract(data, '$.model.providerID') = ? AND json_extract(data, '$.model.id') = ?"
            " AND json_extract(data, '$.cost') IS NOT NULL AND time_created >= ?",
            (provider, model, int(since * 1000))).fetchall()
    finally:
        con.close()
    return [(t / 1000, float(c or 0)) for t, c in rows]


def go_usage(model_ref: str, now: float | None = None, db: Path | None = None) -> dict:
    """Estimated usage of one OpenCode Go model, e.g. "opencode-go/deepseek-v4.1-flash"."""
    now = time.time() if now is None else now
    db = db or opencode_db()
    out = {"provider": "OpenCode Go", "model": model_ref, "source": "estimate", "windows": []}
    try:
        monthly = monthly_limits().get(model_ref)
    except (OSError, ValueError) as e:
        out["error"] = f"limits.json unreadable ({e.__class__.__name__})"
        return out
    if monthly is None:
        out["error"] = f"no monthly limit for {model_ref} in limits.json"
        return out
    provider, _, model = model_ref.partition("/")
    try:
        spend = go_spend(db, provider, model, now - WINDOWS[-1][1])
    except sqlite3.Error as e:
        out["error"] = f"opencode database unreadable: {e.__class__.__name__}"
        return out
    for name, seconds, share in WINDOWS:
        inside = [(t, c) for t, c in spend if t > now - seconds]
        used = sum(c for _, c in inside)
        limit = monthly * share
        oldest = min((t for t, c in inside if c > 0), default=None)
        out["windows"].append({
            "window": name,
            "used_usd": round(used, 4),
            "limit_usd": round(limit, 2),
            "percent": round(100 * used / limit, 1) if limit else None,
            # Rolling window: the oldest counted spend stops counting at this time.
            "resets_at": oldest + seconds if oldest is not None else None,
        })
    return out


def _load_hydra_pod_quota():
    hp = Path(os.environ.get("HYDRA_POD_HOME") or Path.home() / "Hydra-Pod")
    if str(hp) not in sys.path:
        sys.path.insert(0, str(hp))
    from hydra_pod import quota  # noqa: E402  (Hydra-Pod is an external checkout)
    return quota


def zai_usage(now: float | None = None, fetch=None) -> dict:
    """Exact Z.ai GLM Coding Plan usage, cached for ZAI_CACHE_TTL seconds."""
    now = time.time() if now is None else now
    out = {"provider": "Z.ai GLM Coding Plan", "model": "zai-coding-plan/glm-5.3", "source": "official", "windows": []}
    cache = cache_dir() / "zai-quota.json"
    q = None
    try:
        cached = json.loads(cache.read_text())
        if now - cached["at"] < ZAI_CACHE_TTL:
            q = cached["quota"]
    except (OSError, ValueError, KeyError):
        pass
    if q is None:
        try:
            q = (fetch or _load_hydra_pod_quota().zai_quota)()
        except ImportError as e:
            out["error"] = f"Z.ai quota unavailable: hydra_pod.quota not importable ({e})"
            return out
        except (OSError, ValueError, KeyError, TypeError) as e:
            # the quota endpoint is a network dependency; a failure degrades, never crashes `status`
            out["error"] = f"Z.ai quota unavailable ({e.__class__.__name__})"
            return out
        if q is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"at": now, "quota": q}))
            tmp.replace(cache)  # a concurrent reader never sees a half-written cache
    if q is None:
        out["error"] = "Z.ai quota unavailable (no key file or no network)"
        return out
    out["plan"] = q.get("level")
    try:
        for w in q["windows"]:
            total = w["total"]
            out["windows"].append({
                "window": {"5h": "5h", "1w": "week"}.get(w["window"], w["window"]),
                "used": w["used"], "limit": total, "remaining": w["remaining"],
                "percent": round(100 * w["used"] / total, 1) if total else None,
                "resets_at": w["resets_at"],
            })
    except (KeyError, TypeError):
        out["windows"], out["error"] = [], "Z.ai quota response malformed"
    return out
