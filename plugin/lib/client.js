// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * dsh-hydra-pod, web half: a composer-dock indicator. The pill names who is working
 * now (manager, builder or reviewer) and the usage of the subscription in use; the
 * popover lists every window of both subscriptions with its reset countdown.
 */
window.__ModuleLoader__.load({
  id: 'dsh-hydra-pod',
  factory: (require) => {
    const module = { exports: {} }
    const React = require('react')
    const h = React.createElement

    const name = 'hydra-pod-status'
    const inject = ['slots']
    const POLL_MS = 5000
    const WARN_PERCENT = 80
    const CSS_ID = 'dsh-hydra-pod/styles'
    const CSS = [
      '.hpd{position:relative;display:inline-flex;align-items:center}',
      '.hpd-pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 8px;border-radius:6px;cursor:pointer;',
      'font:var(--dsw-font-xs-12,12px/1.2 system-ui,sans-serif);font-variant-numeric:tabular-nums;white-space:nowrap;',
      'color:var(--dsw-alias-label-secondary);background:transparent;border:1px solid var(--dsw-alias-border-l2)}',
      '.hpd-pill:hover,.hpd-pill[aria-expanded=true]{color:var(--dsw-alias-label-primary);background:var(--dsw-alias-interactive-bg-hover)}',
      '.hpd-pill:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}',
      '.hpd-dot{width:7px;height:7px;border-radius:50%;background:var(--dsw-alias-label-tertiary,#888)}',
      '.hpd-dot.busy{background:#22c55e;animation:hpd-pulse 1.6s ease-in-out infinite}',
      '.hpd-dot.manager{background:#a78bfa}',
      '@keyframes hpd-pulse{50%{opacity:.35}}',
      '@media (prefers-reduced-motion:reduce){.hpd-dot.busy{animation:none}}',
      '.hpd-warn{color:#f59e0b}',
      '.hpd-pop{position:absolute;bottom:calc(100% + 6px);left:0;z-index:50;width:min(400px,calc(100vw - 32px));max-height:min(70vh,640px);overflow:auto;padding:12px;',
      'border-radius:10px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-l1,var(--dsw-alias-bg,#1b1c20));',
      'box-shadow:0 8px 24px rgba(0,0,0,.25);color:var(--dsw-alias-label-primary);font-size:12px;line-height:1.45}',
      '.hpd-now{margin:0 0 10px;font-weight:600}',
      '.hpd-sub{margin:10px 0 4px;display:flex;justify-content:space-between;gap:8px;color:var(--dsw-alias-label-secondary)}',
      '.hpd-sub b{color:var(--dsw-alias-label-primary);font-weight:600}',
      '.hpd-tag{font-size:11px;padding:0 5px;border-radius:4px;border:1px solid var(--dsw-alias-border-l2)}',
      '.hpd-row{display:grid;grid-template-columns:44px 1fr 44px;align-items:center;gap:8px;margin:3px 0}',
      '.hpd-bar{height:6px;border-radius:3px;background:var(--dsw-alias-border-l2);overflow:hidden}',
      '.hpd-bar>i{display:block;height:100%;background:var(--dsw-alias-brand-primary,#4f7cff)}',
      '.hpd-bar>i.warn{background:#f59e0b}',
      '.hpd-meta{grid-column:2/4;color:var(--dsw-alias-label-secondary);font-size:11px;margin-top:-2px}',
      '.hpd-err{color:var(--dsw-alias-label-secondary);font-style:italic}',
      '.hpd-inuse{color:#22c55e;font-size:11px}',
      '.hpd-wf{margin:0 0 8px;padding:6px 8px;border-radius:6px;background:var(--dsw-alias-interactive-bg,rgba(127,127,127,.08))}',
      '.hpd-wf b{font-weight:600}',
      '.hpd-state{font-size:11px;padding:0 5px;border-radius:4px;border:1px solid var(--dsw-alias-border-l2);margin-left:6px}',
      '.hpd-state.blocked{color:#f59e0b;border-color:#f59e0b}',
      '.hpd-tl{margin:8px 0 0;padding:0;list-style:none;max-height:140px;overflow:auto}',
      '.hpd-tl li{display:grid;grid-template-columns:58px 1fr;gap:6px;font-size:11px;color:var(--dsw-alias-label-secondary);padding:1px 0}',
      '.hpd-tl time{font-variant-numeric:tabular-nums}',
      '.hpd-grid{display:grid;grid-template-columns:auto 1fr auto;gap:2px 8px;font-size:11px;color:var(--dsw-alias-label-secondary)}',
      '.hpd-grid b{color:var(--dsw-alias-label-primary);font-weight:500}',
      '.hpd-run{color:#22c55e}',
      '.hpd-stage{margin:4px 0;padding:4px 0;border-top:1px solid var(--dsw-alias-border-l2)}',
      '.hpd-stage b{font-weight:600;font-size:11px}',
      '.hpd-role{font-size:10px;text-transform:uppercase;letter-spacing:.04em;padding:0 4px;border-radius:3px;border:1px solid var(--dsw-alias-border-l2)}',
      '.hpd-quota{color:var(--dsw-alias-label-tertiary,#888)}',
      '.hpd-sev-high,.hpd-sev-critical{color:#ef4444}',
      '.hpd-sev-medium{color:#f59e0b}',
      '.hpd-h{margin:10px 0 2px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--dsw-alias-label-tertiary,#888)}',
    ].join('')

    function ensureCss() {
      if (document.getElementById(CSS_ID)) return
      const style = document.createElement('style')
      style.id = CSS_ID
      style.textContent = CSS
      document.head.appendChild(style)
    }

    function left(ts, now) {
      if (!ts) return '-'
      const s = Math.max(0, Math.floor(ts - now))
      const d = Math.floor(s / 86400)
      const hh = Math.floor((s % 86400) / 3600)
      const mm = Math.floor((s % 3600) / 60)
      return d ? d + 'd ' + hh + 'h' : hh + 'h ' + String(mm).padStart(2, '0') + 'm'
    }

    function pct(w) {
      return w && w.percent != null ? w.percent + '%' : '-'
    }

    const ROLE = { builder: 'Builder', reviewer: 'Reviewer', manager: 'Manager' }

    function clock(ts) {
      const d = new Date(ts * 1000)
      return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':')
    }

    function eventText(e) {
      const p = e.payload || {}
      const kind = e.type.split('/')[1]
      const what = p.decision || p.action || ''
      const move = p.to ? (p.from ? p.from + ' → ' : '') + p.to : ''
      return [e.workflow_id, kind === 'workflow-state' ? '' : kind, what, move, e.reason ? '— ' + e.reason : '']
        .filter(Boolean).join(' ')
    }

    function money(v) { return v == null ? 'unknown' : '$' + Number(v).toFixed(4) }

    function kfmt(n) {
      n = Number(n || 0)
      return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n)
    }

    // Work = input + output + reasoning. Cache is shown apart and never added to work.
    function tokText(t) {
      if (!t || typeof t !== 'object') return '0 tokens'
      const work = (t.input || 0) + (t.output || 0) + (t.reasoning || 0)
      const cache = (t.cache_read || 0) + (t.cache_write || 0)
      return kfmt(work) + ' work tokens (in ' + kfmt(t.input) + ' · out ' + kfmt(t.output) +
        (t.reasoning ? ' · think ' + kfmt(t.reasoning) : '') + ')' + (cache ? ' + ' + kfmt(cache) + ' cache' : '')
    }

    function dur(s) {
      s = Math.max(0, Math.round(s || 0))
      return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's'
    }

    function share(q, now) {
      if (!q) return null
      return ['5h', 'week'].filter((n) => q[n]).map((n) => n + ' ' + q[n].percent + '% of window' +
        (q[n].resets_at ? ' (resets ' + left(q[n].resets_at, now) + ')' : '')).join(' · ') + ' · ' + q.source
    }

    function Stages({ w, now }) {
      const rows = (w.stages || []).filter((s) => s.actors.length || s.open)
      if (!rows.length) return null
      return h('div', null,
        h('p', { className: 'hpd-h' }, 'Stages — ' + w.id),
        rows.map((s) => h('div', { className: 'hpd-stage', key: s.n },
          h('div', null, h('b', null, '#' + s.n + ' ' + s.state), ' · ' + dur(s.seconds) + (s.open ? ' · now' : '')),
          s.actors.length ? s.actors.map((a, i) => h('div', { className: 'hpd-meta', key: i },
            h('span', { className: 'hpd-role' }, a.role), ' ' + a.model + ' [' + a.billing + ']',
            h('div', null, tokText(a.tokens) +
              (a.list_cost_usd != null ? ' · list ' + money(a.list_cost_usd) : '') +
              (a.zai_credits != null ? ' · ' + a.zai_credits + ' credits' : '') +
              (a.kind === 'manager' ? ' · cost ' + money(a.cost_usd) : '')),
            a.quota ? h('div', { className: 'hpd-quota' }, share(a.quota, now)) : null))
            : h('div', { className: 'hpd-meta' }, 'no model work recorded in this stage'))))
    }

    function WorkflowDetail({ w }) {
      const r = w.resources || {}
      const f = w.findings || {}
      const sev = f.by_severity || {}
      const st = f.by_status || {}
      const b = w.budget_check
      return h('div', { className: 'hpd-meta' },
        h('div', null, 'health: ', h('b', null, w.health || '-'),
          w.health === 'stalled' ? ' — run: hydra-pod-dsh wf recover ' + w.id + ' --reason …' : ''),
        h('div', null, 'resources: ' + (r.runs || 0) + ' run(s) · ' + tokText(r.tokens) + ' · cost ' + money(r.list_cost_usd) +
          ' · Z.ai credits ' + (r.zai_credits == null ? '-' : r.zai_credits) + ' · ' + (r.seconds == null ? '-' : Math.round(r.seconds) + 's')),
        h('div', null, 'findings: ',
          ['critical', 'high', 'medium', 'low'].filter((k) => sev[k]).map((k) =>
            h('span', { key: k, className: 'hpd-sev-' + k }, sev[k] + ' ' + k + ' ')),
          (st.valid || st.partial || st.rejected || st.unverified)
            ? '(valid ' + (st.valid || 0) + ', rejected ' + (st.rejected || 0) + ', unverified ' + (st.unverified || 0) + ')' : 'none'),
        b ? h('div', { className: b.level === 'ok' ? '' : 'hpd-warn' }, 'budget: ' + b.level + ' · ' +
          b.dimensions.map((d) => d.dimension.replace('max_', '') + ' ' + (d.percent == null ? '?' : d.percent + '%')).join(' · ')) : null)
    }

    function Agents({ st }) {
      const ag = st.agents || []
      if (!ag.length) return null
      return h('div', null, h('p', { className: 'hpd-h' }, 'Agents'),
        h('div', { className: 'hpd-grid' }, ag.flatMap((a) => [
          h('b', { key: a.name + 'n' }, a.name),
          h('span', { key: a.name + 'm' }, a.model + ' [' + a.billing + ']'),
          h('span', { key: a.name + 'a', className: a.activity === 'running' ? 'hpd-run' : '' }, a.activity)])))
    }

    function Workflows({ st }) {
      const wfs = st.workflows || []
      const tl = st.timeline || []
      if (!wfs.length && !tl.length && !st.ledger_error) return null
      return h('div', null,
        h('p', { className: 'hpd-h' }, 'Workflow'),
        st.ledger_error ? h('div', { className: 'hpd-err' }, st.ledger_error) : null,
        wfs.map((w) => h('div', { className: 'hpd-wf', key: w.id },
          h('b', null, w.id),
          h('span', { className: 'hpd-state' + (w.state === 'BLOCKED' ? ' blocked' : '') }, w.state),
          h('div', { className: 'hpd-meta' },
            'task ' + (w.task_id || '-') + ' · rework ' + w.rework_attempts + '/' + w.policy.max_rework_attempts +
            ' · review ' + w.review_cycles + '/' + w.policy.max_review_cycles +
            ' · replan ' + w.replans + '/' + w.policy.max_replans))),
        wfs.map((w) => h(WorkflowDetail, { w, key: w.id + '-d' })),
        wfs.map((w) => h(Stages, { w, now: st.now, key: w.id + '-s' })),
        tl.length ? h('ul', { className: 'hpd-tl', 'aria-label': 'Workflow timeline' },
          tl.slice().reverse().map((e) => h('li', { key: e.id },
            h('time', null, clock(e.at)), h('span', null, eventText(e))))) : null)
    }
    const SUB_SHORT = { 'opencode-go': 'Go', zai: 'Z.ai' }

    function useStatus() {
      const [state, setState] = React.useState({ data: null, error: null })
      React.useEffect(() => {
        let alive = true
        let timer
        const load = async () => {
          if (document.visibilityState === 'visible') {
            try {
              const r = await fetch('/api/hydra-pod/status', { cache: 'no-store', credentials: 'same-origin' })
              const body = await r.json()
              if (alive) setState(r.ok ? { data: body, error: null } : { data: null, error: body.error || 'HTTP ' + r.status })
            } catch (e) {
              if (alive) setState((s) => ({ data: s.data, error: 'status unavailable' }))
            }
          }
          if (alive) timer = setTimeout(load, POLL_MS)
        }
        load()
        return () => { alive = false; clearTimeout(timer) }
      }, [])
      return state
    }

    function pillText(st) {
      const a = st.active
      const now = st.now
      const wf = (st.workflows || [])[0]
      if (!a && wf) {
        return { text: 'Hydra-Pod · ' + (wf.task_id || wf.id) + ' · ' + wf.state, dot: wf.state === 'BLOCKED' ? '' : 'manager',
          warn: wf.state === 'BLOCKED' }
      }
      if (!a) {
        const go = st.usage['opencode-go'].windows.find((w) => w.window === '5h')
        const zw = st.usage.zai.windows.find((w) => w.window === 'week')
        return { text: 'Hydra-Pod · idle · Go 5h ' + pct(go) + ' · Z.ai week ' + pct(zw), dot: '', warn: false }
      }
      const parts = ['Hydra-Pod']
      if (a.ticket) parts.push(a.ticket)
      if (a.source === 'manager') {
        parts.push('Manager (' + a.stage + ')')
        return { text: parts.join(' · '), dot: 'manager', warn: false }
      }
      parts.push(ROLE[a.role] + ': ' + a.who)
      const u = a.subscription && st.usage[a.subscription]
      const w = u && u.windows.find((x) => x.window === '5h')
      if (w) parts.push(SUB_SHORT[a.subscription] + ' 5h ' + pct(w) + ' · resets ' + left(w.resets_at, now))
      return { text: parts.join(' · '), dot: 'busy', warn: !!(w && w.percent >= WARN_PERCENT) }
    }

    function Subscription({ u, inUse, now }) {
      return h('div', null,
        h('div', { className: 'hpd-sub' },
          h('span', null, h('b', null, u.provider), ' ', inUse ? h('span', { className: 'hpd-inuse' }, '● in use') : null),
          h('span', { className: 'hpd-tag', title: u.source === 'estimate'
            ? 'Estimated from this machine\'s opencode history; OpenCode Go has no usage API'
            : 'From the official quota endpoint' }, u.source)),
        u.error ? h('div', { className: 'hpd-err' }, u.error) : null,
        u.windows.map((w) => {
          const warn = w.percent != null && w.percent >= WARN_PERCENT
          const amount = w.used_usd != null
            ? '$' + w.used_usd.toFixed(2) + ' of $' + w.limit_usd.toFixed(2)
            : w.used + ' of ' + w.limit
          return h(React.Fragment, { key: w.window },
            h('div', { className: 'hpd-row' },
              h('span', null, w.window),
              h('div', { className: 'hpd-bar', role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100,
                'aria-valuenow': w.percent || 0, 'aria-label': u.provider + ' ' + w.window },
              h('i', { className: warn ? 'warn' : '', style: { width: Math.min(100, w.percent || 0) + '%' } })),
              h('span', { className: warn ? 'hpd-warn' : '' }, pct(w))),
            h('div', { className: 'hpd-row' },
              h('span', { className: 'hpd-meta' }, amount + ' · resets in ' + left(w.resets_at, now))))
        }))
    }

    function HydraPodDock() {
      ensureCss()
      const { data, error } = useStatus()
      const [open, setOpen] = React.useState(false)
      const ref = React.useRef(null)
      React.useEffect(() => {
        if (!open) return
        const close = (e) => { if (e.type === 'keydown' ? e.key === 'Escape' : !ref.current?.contains(e.target)) setOpen(false) }
        document.addEventListener('mousedown', close)
        document.addEventListener('keydown', close)
        return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', close) }
      }, [open])
      if (!data) {
        return error ? h('span', { className: 'hpd-pill', title: error }, h('span', { className: 'hpd-dot' }), 'Hydra-Pod · status unavailable') : null
      }
      const p = pillText(data)
      const a = data.active
      const nowLine = !a ? 'Nobody is working: Hydra-Pod is idle.'
        : a.source === 'manager' ? 'Manager — ' + a.who + ' — ' + a.stage + (a.ticket ? ' — ' + a.ticket : '')
          : ROLE[a.role] + ' — ' + a.who + (a.ticket ? ' — ' + a.ticket : '')
      return h('div', { className: 'hpd', ref },
        h('button', { type: 'button', className: 'hpd-pill' + (p.warn ? ' hpd-warn' : ''), 'aria-expanded': open,
          onClick: () => setOpen((o) => !o) },
        h('span', { className: 'hpd-dot ' + p.dot }), p.text),
        open ? h('div', { className: 'hpd-pop', role: 'dialog', 'aria-label': 'Hydra-Pod status' },
          h('p', { className: 'hpd-now' }, nowLine),
          h(Workflows, { st: data }),
          h(Agents, { st: data }),
          h('p', { className: 'hpd-h' }, 'Subscriptions'),
          h(Subscription, { u: data.usage['opencode-go'], inUse: a && a.subscription === 'opencode-go', now: data.now }),
          h(Subscription, { u: data.usage.zai, inUse: a && a.subscription === 'zai', now: data.now })) : null)
    }

    function apply(ctx) {
      ctx.slots.inject('conversation.composer.dock', () => ctx.slots.register({
        name: 'conversation.composer.dock', id: 'hydra-pod', order: 20,
      }, HydraPodDock))
    }

    module.exports.apply = apply
    module.exports.inject = inject
    module.exports.name = name
    return module.exports
  },
})
