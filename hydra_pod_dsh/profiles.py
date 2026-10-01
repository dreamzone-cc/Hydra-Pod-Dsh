# SPDX-License-Identifier: AGPL-3.0-or-later
"""Team profiles (technical paper §5.1, phase 3): one switch for how much the pod spends.

A profile is a JSON file in profiles/ (economy, balanced, max-quality). The
active one is per project, in `<project>/.hydra/profile`; without it the
project runs `balanced`. Today a profile sets `second_review`: "never",
"always", or null to follow each review stage's own `when` rules. A profile
can never widen the policy: it only chooses among agents the registry allows.
"""

import json
import re
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT = "balanced"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
KEYS = {"name", "description", "second_review"}


def available() -> dict[str, dict]:
    out = {}
    for f in sorted(DIR.glob("*.json")):
        data = json.loads(f.read_text())
        bad = set(data) - KEYS
        if bad or data.get("second_review") not in (None, "never", "always"):
            raise ValueError(f"profiles/{f.name}: unknown keys {sorted(bad)} or bad second_review")
        out[f.stem] = data
    return out


def active_file(project) -> Path:
    return Path(project) / ".hydra" / "profile"


def active(project) -> dict:
    f = active_file(project)
    name = f.read_text().strip() if f.exists() else DEFAULT
    profs = available()
    if name not in profs:
        raise ValueError(f"{f}: unknown profile {name!r} (one of {', '.join(profs)})")
    return profs[name]


def set_active(project, name: str) -> dict:
    if not NAME.match(name or ""):
        raise ValueError(f"bad profile name {name!r}")
    profs = available()
    if name not in profs:
        raise ValueError(f"unknown profile {name!r} (one of {', '.join(profs)})")
    f = active_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(name + "\n")
    return profs[name]
