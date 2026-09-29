---
name: opus-manager
description: "Managed mode: you act as the manager and do not write the code yourself. Turn work into tickets, hand them to other AI command-line tools on the user's machine (cheaper models), then accept the result, get a second vendor to review it, and verify every finding. Use when the user says \"use tickets\", \"manage this\", \"hand it off\", or asks other models to do the work. On first use, find out which workers this machine has and settle the setup with the user."
---

# Ticket management (Hydra-Pod's vendored opus-manager skill)

This entry only makes the vendored skill loadable in DeepSeek Harness: its own frontmatter is not valid YAML (an unquoted `: ` in the description), so dsh would skip it. The instructions themselves are not repeated here, to keep a single source.

Now read `~/Hydra-Pod/skill/opus-manager/SKILL.md` with your file-read tool and follow everything after its frontmatter as this skill's instructions. Its `templates/` folder is `~/Hydra-Pod/skill/opus-manager/templates/` (`ticket.md`, `receipt.md`).

`~/Hydra-Pod` here and in that skill means `$HYDRA_POD_HOME` when that variable is set, else `~/Hydra-Pod`.

If that file does not exist, stop and tell the user that the Hydra-Pod checkout is missing (see `~/Hydra-Pod/Hydra-Pod-Dsh/README.md`).
