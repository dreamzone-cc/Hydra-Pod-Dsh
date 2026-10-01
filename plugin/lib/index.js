// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * dsh-hydra-pod, host half:
 * - GET /api/hydra-pod/status runs `hydra-pod-dsh status --json` (read-only, no quota spent;
 *   cached briefly so several open tabs share one run);
 * - human commands through ctx.commands (architecture §25, §43), executed directly
 *   without a model message: /hydra-pod-status, /hydra-pod-timeline [WF],
 *   /hydra-pod-control <WF> <cancel|pause|resume|approve|reject> <reason…>.
 *   Arguments are validated and passed as an argv array (no shell).
 */
import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'hydra-pod-status'
export const inject = []

export const STATUS_PATH = '/api/hydra-pod/status'
const CACHE_MS = 4000
const RUN_TIMEOUT_MS = 20000

/** The status command: $HYDRA_POD_DSH_BIN, $HYDRA_POD_BIN_DIR (what install.sh
 *  honors), the installed link, or PATH. */
export function statusCommand(env = process.env, home = homedir()) {
  if (env.HYDRA_POD_DSH_BIN) return env.HYDRA_POD_DSH_BIN
  if (env.HYDRA_POD_BIN_DIR) return join(env.HYDRA_POD_BIN_DIR, 'hydra-pod-dsh')
  const linked = join(home, '.local/bin/hydra-pod-dsh')
  return existsSync(linked) ? linked : 'hydra-pod-dsh'
}

/** Same-origin loopback requests only, as dsh's own plugins require. */
export function trustedRequest(req) {
  const remote = req.socket?.remoteAddress
  if (remote !== '127.0.0.1' && remote !== '::1' && remote !== '::ffff:127.0.0.1') return false
  if (req.headers['sec-fetch-site'] === 'cross-site') return false
  const host = req.headers.host
  if (host === undefined) return false
  const origin = req.headers.origin
  if (origin === undefined) return true
  try {
    return new URL(origin).host === new URL(`http://${host}`).host
  } catch {
    return false
  }
}

function send(res, status, body) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  })
  res.end(typeof body === 'string' ? body : JSON.stringify(body))
}

/** Runs the status command at most once per CACHE_MS; concurrent callers share a run. */
export function createStatusReader(run = defaultRun, now = Date.now) {
  let cached
  let inflight
  return async function read() {
    if (cached && now() - cached.at < CACHE_MS) return cached.body
    if (!inflight) {
      inflight = run().then((body) => {
        cached = { at: now(), body }
        return body
      }).finally(() => { inflight = undefined })
    }
    return inflight
  }
}

function defaultRun() {
  return new Promise((resolve, reject) => {
    execFile(statusCommand(), ['status', '--json'], { timeout: RUN_TIMEOUT_MS, maxBuffer: 1 << 20 }, (error, stdout) => {
      if (error) return reject(error)
      try {
        JSON.parse(stdout)
        resolve(stdout)
      } catch (e) {
        reject(e)
      }
    })
  })
}

const WF_ID = /^WF-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const ACTIONS = new Set(['cancel', 'pause', 'resume', 'approve', 'reject'])
const PROFILE = /^[a-z0-9][a-z0-9-]{0,31}$/

/** argv for one command line, or an error string. Pure, so it is unit-tested. */
export function commandArgs(name, rawInput) {
  const words = String(rawInput || '').trim().split(/\s+/).filter(Boolean)
  if (name === 'hydra-pod-status') {
    return words.length ? 'usage: /hydra-pod-status' : ['status']
  }
  if (name === 'hydra-pod-timeline') {
    if (words.length > 1 || (words[0] && !WF_ID.test(words[0]))) return 'usage: /hydra-pod-timeline [WF-<ticket>]'
    return ['wf', 'timeline', ...words, '--limit', '20']
  }
  if (name === 'hydra-pod-control') {
    const [wf, action, ...reason] = words
    if (!wf || !WF_ID.test(wf) || !ACTIONS.has(action) || !reason.length) {
      return 'usage: /hydra-pod-control WF-<ticket> cancel|pause|resume|approve|reject <reason>'
    }
    return ['wf', 'human', wf, action, '--reason', reason.join(' ')]
  }
  if (name === 'hydra-pod-profile') {
    if (!words.length) return ['profile', 'list']
    if (words.length === 1 && PROFILE.test(words[0])) return ['profile', 'set', words[0]]
    return 'usage: /hydra-pod-profile [economy|balanced|max-quality]'
  }
  return 'unknown command'
}

/** The project the user is working in: the one `status` reports. */
async function currentProject(read) {
  try {
    return JSON.parse(await read()).project || undefined
  } catch {
    return undefined
  }
}

function runText(args) {
  return new Promise((resolve) => {
    execFile(statusCommand(), args, { timeout: RUN_TIMEOUT_MS, maxBuffer: 1 << 20 }, (error, stdout, stderr) => {
      resolve({ ok: !error, text: String(stdout || '').trim() || String(stderr || '').trim() || (error ? String(error.message) : '') })
    })
  })
}

export function apply(ctx) {
  const read = createStatusReader()
  ctx.inject(['commands'], (cmdCtx) => {
    const specs = [
      ['hydra-pod-status', 'Hydra-Pod: who is working, workflow state and subscription usage', undefined],
      ['hydra-pod-timeline', 'Hydra-Pod: workflow timeline from the project ledger', '[WF-<ticket>]'],
      ['hydra-pod-control', 'Hydra-Pod: your decision on a workflow (recorded in the ledger)',
        'WF-<ticket> cancel|pause|resume|approve|reject <reason>'],
      ['hydra-pod-profile', 'Hydra-Pod: show or set the team profile (how much the pod may spend)',
        '[economy|balanced|max-quality]'],
    ]
    for (const [cmd, description, hint] of specs) {
      cmdCtx.commands.register({
        name: cmd,
        description,
        ...(hint ? { input: { hint } } : {}),
        handler: async ({ rawInput }) => {
          const args = commandArgs(cmd, rawInput)
          if (typeof args === 'string') return { kind: 'error', text: args }
          const project = cmd === 'hydra-pod-status' ? undefined : await currentProject(read)
          if (cmd !== 'hydra-pod-status' && !project) {
            return { kind: 'error', text: 'no active Hydra-Pod project (start one with /hydra-pod)' }
          }
          const r = await runText(project ? [...args, '--project', project] : args)
          return { kind: r.ok ? 'success' : 'error', text: r.text }
        },
      })
    }
  })
  ctx.inject(['webServer', 'connection'], (webCtx) => {
    webCtx.webServer.register({
      kind: 'exact',
      path: STATUS_PATH,
      handler: async (req, res) => {
        const rejection = webCtx.connection.requestRejection(req)
        if (rejection !== undefined) return send(res, rejection, { error: rejection === 401 ? 'unauthorized' : 'forbidden' })
        if (!trustedRequest(req)) return send(res, 403, { error: 'forbidden' })
        if (req.method !== 'GET') return send(res, 405, { error: 'method not allowed' })
        try {
          send(res, 200, await read())
        } catch {
          send(res, 503, { error: 'hydra-pod-dsh status failed; run it in a terminal to see why' })
        }
      },
    })
  })
}
