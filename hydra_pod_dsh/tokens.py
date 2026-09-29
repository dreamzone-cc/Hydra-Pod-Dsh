# SPDX-License-Identifier: AGPL-3.0-or-later
"""One token vocabulary for every source (cost log, DSH session logs, UI, report).

Cache reads are kept apart from the work a model actually did: they are billed
far lower, and adding them to one total made a 34k-token build look like 230k
(scripted W1). `work` = input + output + reasoning; cache is always shown
separately and never summed into `work`.
"""

FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")
# Aliases seen in the sources: opencode JSON / cost log, DSH `usage` objects.
ALIASES = {"input": "input", "inputTokens": "input", "output": "output", "outputTokens": "output",
           "reasoning": "reasoning", "reasoningTokens": "reasoning", "thinkingTokens": "reasoning",
           "cache_read": "cache_read", "cacheReadTokens": "cache_read", "cache_write": "cache_write",
           "cacheWriteTokens": "cache_write", "cacheCreationTokens": "cache_write"}


def zero() -> dict:
    return {f: 0 for f in FIELDS}


def normalize(raw: dict | None) -> dict:
    out = zero()
    for k, v in (raw or {}).items():
        f = ALIASES.get(k)
        if f and isinstance(v, (int, float)):
            out[f] += int(v)
    return out


def add(a: dict, b: dict) -> dict:
    return {f: a.get(f, 0) + b.get(f, 0) for f in FIELDS}


def work(t: dict) -> int:
    return t.get("input", 0) + t.get("output", 0) + t.get("reasoning", 0)


def human(n: int) -> str:
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt(t: dict) -> str:
    parts = [f"in {human(t['input'])}", f"out {human(t['output'])}"]
    if t.get("reasoning"):
        parts.append(f"think {human(t['reasoning'])}")
    cache = t.get("cache_read", 0) + t.get("cache_write", 0)
    if cache:
        parts.append(f"cache {human(t.get('cache_read', 0))}r/{human(t.get('cache_write', 0))}w")
    return " · ".join(parts)
