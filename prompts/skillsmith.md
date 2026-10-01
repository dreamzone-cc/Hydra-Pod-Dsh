# Skill smith prompt (`hydra-pod-dsh skill draft`)

Turns one recurring pattern from the project's records into a project skill draft. The draft enters the library as a candidate only; the manager reviews and approves it. The format is parsed: keep it exact.

```text
You write one reusable instruction ("skill") for coding agents working in this repository. You run headless: nobody can answer questions. You may read files with your tools; you cannot run commands or change anything.
The same problem has come up several times (source: <kind>). The cases, from the project's own records:
<evidence>
Files involved: <paths>

Write the skill that would have prevented these cases: concrete steps and checks for this codebase (names, files, commands that exist here), not general advice. Read the files to get them right.
Reply in exactly this shape, and nothing else:
NAME: <lowercase-words-joined-by-dashes, at most 48 characters>
DESCRIPTION: <one sentence, at most 200 characters: when the skill applies and what it prevents>
PATHS: <comma-separated globs of the files it applies to>
KEYWORDS: <comma-separated words that, in a ticket, mean it applies>
BODY:
<the skill itself: at most 25 lines of numbered steps and checks>
```
