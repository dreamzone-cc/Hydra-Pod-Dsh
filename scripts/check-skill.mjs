#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-or-later
// Validate SKILL.md frontmatter the way dsh-skill-filesystem does. A skill it
// rejects is skipped with only a warning in the host log, so check here first.
// Uses the same `yaml` parser, resolved from the dsh checkout ($DSH_REPO,
// default ~/deepseek-harness), so the verdict matches what dsh will do.
// usage: check-skill.mjs <SKILL.md>...   (exit status 1 if any file is invalid)
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const dshRepo = process.env.DSH_REPO ?? join(homedir(), 'deepseek-harness')
const require = createRequire(join(dshRepo, 'packages/skill/skill-filesystem/package.json'))
let parse
try {
  ;({ parse } = require('yaml'))
} catch {
  console.error(`check-skill: cannot load the yaml parser from the dsh checkout at ${dshRepo}`
    + ' (set DSH_REPO or clone deepseek-harness there)')
  process.exit(1)
}

// Mirrors dsh-skill's SKILL_NAME and skill-filesystem's legacy-key rejection.
const SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const LEGACY_KEYS = ['disableModelInvocation', 'modelInvocable', 'userInvocable']
const BOOL_KEYS = ['disable-model-invocation', 'user-invocable']

export function check(raw) {
  const lines = raw.split('\n')
  if (lines[0].replace(/\r$/, '') !== '---') return ['missing YAML frontmatter']
  const end = lines.findIndex((l, i) => i > 0 && l.replace(/\r$/, '') === '---')
  if (end < 0) return ['missing YAML frontmatter']
  let data
  try {
    data = parse(lines.slice(1, end).join('\n'))
  } catch (error) {
    return [`invalid YAML frontmatter: ${String(error.message).split('\n')[0]}`]
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return ['missing YAML frontmatter']
  const problems = []
  if (typeof data.name !== 'string' || typeof data.description !== 'string') {
    problems.push('frontmatter requires name and description')
  } else if (!SKILL_NAME.test(data.name)) {
    problems.push(`invalid skill name "${data.name}"`)
  }
  for (const key of LEGACY_KEYS) if (Object.hasOwn(data, key)) problems.push(`unsupported field "${key}"`)
  for (const key of BOOL_KEYS) {
    if (Object.hasOwn(data, key) && typeof data[key] !== 'boolean') problems.push(`${key} must be true or false`)
  }
  return problems
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let status = 0
  for (const path of process.argv.slice(2)) {
    for (const p of check(readFileSync(path, 'utf8'))) {
      console.error(`${path}: ${p}`)
      status = 1
    }
  }
  process.exit(status)
}
