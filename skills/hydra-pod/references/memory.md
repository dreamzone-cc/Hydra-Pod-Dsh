# Project memory: lessons, the steward, and project skills

Three stores, from smallest to largest. Each one feeds the context packs automatically; you only decide what goes in.

## Lessons

After a REWORK, a valid finding, or an accepted conclusion that a later ticket could repeat, record one rule: `hydra-pod-dsh lesson add --text '<rule>' --path <file> [--tag <word>] --wf WF-<T>`. Matching lessons go into later context packs automatically.

## The steward's core memory

At the start of a session read `.hydra/steward/core.md` if it exists (run `hydra-pod-dsh steward init` once per project). Keep its Architecture, Invariants, Conventions and Danger zones current with `hydra-pod-dsh steward set <section> --text '…' --reason '…'`: rules only, detail goes to skills and lessons.

## Project skills

When the same steps recur across tickets, write them once as a project skill (`hydra-pod-dsh skill add <name> --description '…' --file <body.md> --paths '<globs>' --keywords '<words>'`), review it, and `skill approve` it. Context packs attach trial and active skills automatically; your APPROVE and REWORK decisions score them, which promotes or demotes them (`skill list`). A skill in review needs `skill revise` and your approval again.

## Drafts from the skill smith

Every few closed tickets, run `hydra-pod-dsh skill mine`. It lists what keeps repeating in the project's records (lessons that share files or tags, alike REWORK reasons, alike valid findings, suggestions adopted twice), with the evidence. It spends nothing. For a proposal worth a skill, `hydra-pod-dsh skill draft <P-id>` has a cheap read-only model write the skill from that evidence. The draft enters the library only as a candidate: read it (`skill show`), correct it with `skill revise` if needed, and approve it yourself.
