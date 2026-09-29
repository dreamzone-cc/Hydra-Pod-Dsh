// Tests for plugin/lib/index.js (run: node --test tests/)
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createStatusReader, statusCommand, trustedRequest } from '../plugin/lib/index.js'

const req = (remote, headers) => ({ socket: { remoteAddress: remote }, headers })

test('trustedRequest: loopback same-origin only', () => {
  assert.equal(trustedRequest(req('127.0.0.1', { host: '127.0.0.1:3080' })), true)
  assert.equal(trustedRequest(req('127.0.0.1', { host: '127.0.0.1:3080', origin: 'http://127.0.0.1:3080' })), true)
  assert.equal(trustedRequest(req('10.0.0.5', { host: '127.0.0.1:3080' })), false)
  assert.equal(trustedRequest(req('127.0.0.1', { host: '127.0.0.1:3080', origin: 'http://evil.test' })), false)
  assert.equal(trustedRequest(req('::1', { host: 'localhost:3080', 'sec-fetch-site': 'cross-site' })), false)
  assert.equal(trustedRequest(req('127.0.0.1', {})), false)
})

test('statusCommand: env override, then PATH fallback', () => {
  assert.equal(statusCommand({ HYDRA_POD_DSH_BIN: '/x/y' }, '/nohome'), '/x/y')
  assert.equal(statusCommand({ HYDRA_POD_BIN_DIR: '/opt/bin' }, '/nohome'), '/opt/bin/hydra-pod-dsh')
  assert.equal(statusCommand({}, '/nonexistent-home'), 'hydra-pod-dsh')
})

test('createStatusReader: one run per cache window, shared by concurrent callers', async () => {
  let runs = 0
  let t = 0
  const read = createStatusReader(async () => { runs++; return `{"n":${runs}}` }, () => t)
  const [a, b] = await Promise.all([read(), read()])
  assert.equal(runs, 1)
  assert.equal(a, b)
  t = 1000
  await read()
  assert.equal(runs, 1)
  t = 10000
  assert.equal(await read(), '{"n":2}')
})

test('createStatusReader: a failed run is not cached', async () => {
  let fail = true
  const read = createStatusReader(async () => { if (fail) throw new Error('x'); return '{}' }, () => 0)
  await assert.rejects(read())
  fail = false
  assert.equal(await read(), '{}')
})

import { commandArgs } from '../plugin/lib/index.js'

test('commandArgs: strict grammar, argv only, reason required for control', () => {
  assert.deepEqual(commandArgs('hydra-pod-status', ''), ['status'])
  assert.match(commandArgs('hydra-pod-status', 'x'), /usage/)
  assert.deepEqual(commandArgs('hydra-pod-timeline', ' WF-T6 '), ['wf', 'timeline', 'WF-T6', '--limit', '20'])
  assert.match(commandArgs('hydra-pod-timeline', 'WF-T6; rm -rf /'), /usage/)
  assert.deepEqual(commandArgs('hydra-pod-control', 'WF-T6 pause lunch break'),
    ['wf', 'human', 'WF-T6', 'pause', '--reason', 'lunch break'])
  assert.match(commandArgs('hydra-pod-control', 'WF-T6 pause'), /usage/)
  assert.match(commandArgs('hydra-pod-control', 'WF-T6 delete everything'), /usage/)
  assert.match(commandArgs('hydra-pod-control', '$(id) pause x'), /usage/)
  assert.match(commandArgs('other', ''), /unknown/)
})

import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../plugin/lib/index.js'

test('commands end to end: registered on ctx.commands, control writes a user decision to the ledger', async () => {
  const bin = join(dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'hydra-pod-dsh')
  const project = mkdtempSync(join(tmpdir(), 'hpd-proj-'))
  const cache = mkdtempSync(join(tmpdir(), 'hpd-cache-'))
  const env = { ...process.env, XDG_CACHE_HOME: cache, HYDRA_POD_DSH_BIN: bin }
  execFileSync(bin, ['wf', 'start', 'T1', '--objective', 'o', '--project', project], { env })
  execFileSync(bin, ['wf', 'advance', 'WF-T1', 'PLANNING', '--project', project], { env })  // sets the stage → project
  const saved = { ...process.env }
  Object.assign(process.env, env)
  try {
    const registered = {}
    const ctx = {
      inject(keys, fn) {
        if (keys.includes('commands')) fn({ commands: { register: (d) => { registered[d.name] = d } } })
      },
    }
    apply(ctx)
    assert.deepEqual(Object.keys(registered).sort(), ['hydra-pod-control', 'hydra-pod-status', 'hydra-pod-timeline'])
    for (const d of Object.values(registered)) assert.match(d.name, /^[a-z][a-z0-9_-]*$/)
    const bad = await registered['hydra-pod-control'].handler({ rawInput: 'WF-T1 explode now' })
    assert.equal(bad.kind, 'error')
    const ok = await registered['hydra-pod-control'].handler({ rawInput: 'WF-T1 pause coffee' })
    assert.equal(ok.kind, 'success', ok.text)
    const last = readFileSync(join(project, '_receipts', 'ledger.jsonl'), 'utf8').trim().split('\n').pop()
    const ev = JSON.parse(last)
    assert.equal(ev.type, 'hydra/human-decision')
    assert.equal(ev.actor.kind, 'user')
    assert.equal(ev.payload.to, 'BLOCKED')
    const tl = await registered['hydra-pod-timeline'].handler({ rawInput: '' })
    assert.equal(tl.kind, 'success')
    assert.match(tl.text, /human-decision/)
    const st = await registered['hydra-pod-status'].handler({ rawInput: '' })
    assert.equal(st.kind, 'success')
    assert.match(st.text, /Hydra-Pod/)
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k]
    Object.assign(process.env, saved)
  }
})

function routeHandler(rejectionOf) {
  const routes = {}
  const ctx = {
    inject(keys, fn) {
      if (!keys.includes('webServer')) return
      fn({
        webServer: { register: (r) => { routes[r.path] = r.handler } },
        connection: { requestRejection: (req) => rejectionOf(req) },
      })
    },
  }
  apply(ctx)
  const handler = routes['/api/hydra-pod/status']
  assert.ok(handler, 'route registered')
  return handler
}

function fakeRes() {
  const r = { statusCode: 0, headers: {}, body: '' }
  r.writeHead = (code, headers) => { r.statusCode = code; r.headers = headers }
  r.end = (b) => { r.body = String(b) }
  return r
}

const loopReq = (method = 'GET', headers = { host: '127.0.0.1:3080' }) => ({ method, socket: { remoteAddress: '127.0.0.1' }, headers })

test('route: session rejection wins, then trust, then method', async () => {
  // dsh says there is no session -> 401 before anything else runs
  let res = fakeRes()
  await routeHandler(() => 401)(loopReq(), res)
  assert.equal(res.statusCode, 401)
  assert.match(res.body, /unauthorized/)
  assert.equal(res.headers['cache-control'], 'no-store')

  res = fakeRes()
  await routeHandler(() => 403)(loopReq(), res)
  assert.equal(res.statusCode, 403)

  // a session, but the request is not loopback/same-origin -> 403
  res = fakeRes()
  await routeHandler(() => undefined)({ method: 'GET', socket: { remoteAddress: '10.0.0.5' }, headers: { host: 'x' } }, res)
  assert.equal(res.statusCode, 403)

  // trusted session + loopback, but not GET -> 405
  res = fakeRes()
  await routeHandler(() => undefined)(loopReq('POST'), res)
  assert.equal(res.statusCode, 405)
})

test('route: a failing status command degrades to 503, a working one to 200 JSON', async () => {
  const saved = { ...process.env }
  const cache = mkdtempSync(join(tmpdir(), 'hpd-route-'))
  Object.assign(process.env, { XDG_CACHE_HOME: cache, HYDRA_POD_DSH_BIN: '/nonexistent/hydra-pod-dsh' })
  try {
    let res = fakeRes()
    await routeHandler(() => undefined)(loopReq(), res)
    assert.equal(res.statusCode, 503)
    assert.match(res.body, /status failed/)

    const bin = join(dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'hydra-pod-dsh')
    Object.assign(process.env, { HYDRA_POD_DSH_BIN: bin, OPENCODE_DB: '/nonexistent/o.db', ZAI_KEY_FILE: '/nonexistent/key' })
    res = fakeRes()
    await routeHandler(() => undefined)(loopReq(), res)
    assert.equal(res.statusCode, 200)
    assert.equal(res.headers['content-type'], 'application/json; charset=utf-8')
    assert.equal(JSON.parse(res.body).schema_version, 1)
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k]
    Object.assign(process.env, saved)
  }
})
