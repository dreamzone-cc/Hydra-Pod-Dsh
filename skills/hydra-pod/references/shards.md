# Shards, keepers and the blackboard

The pod can work on more code than any one model's window holds: each model holds a part (a shard), and they exchange short, checked facts instead of files. You, the manager, see only the plan and the facts.

## Allocating a ticket

- `hydra-pod-dsh shards list` shows the project's shards and their sizes in tokens.
- `hydra-pod-dsh shards allocate WF-<T>` (before `wf pack`) gives the shards the ticket changes to the builder, and the shards it only reads either to the builder too (when everything fits its window) or to read-only keepers. It records the choice.
  - `split-needed` (exit 3): the shards the ticket changes do not fit the builder's window. Split the ticket.
  - `locked` (exit 3): a running workflow is changing the same shard. Wait, or re-plan so the two do not overlap.
  - "windows assumed": an agent has no `context_window` in agents.json. Tell the user, so they can fill in the figure from the provider's documentation.
- The context pack then tells the builder which shards to ask about instead of reading, with the exact command and the question cap (6 per workflow).

## The blackboard

- `hydra-pod-dsh ask "<question>" --wf WF-<T> [--shard S-…]` answers from a still-valid fact when one exists, with no model call. Otherwise the shard's keeper answers, with file:line references. The answer is stored, and it goes stale by itself when a cited file changes.
- A keeper's answer with no valid reference is stored with low confidence. Never build a decision on it without checking the code yourself.
- `hydra-pod-dsh bb list [--all]` shows the facts (`--all` includes the stale ones). When you have verified something yourself, record it with `hydra-pod-dsh bb add --wf WF-<T> --shard S-… --question '…' --answer '…' --refs path:line`, so later tickets reuse it.
