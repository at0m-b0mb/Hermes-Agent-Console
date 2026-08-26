/* HERMES — Agent Operations Console
   Vanilla JS, no build step, no dependencies. */

const App = {
  token: '',
  boot: null,
  view: 'command',
  state: {
    agents: [], tasks: [], runs: [], approvals: [], escalations: [],
    duties: [], leaderboard: [], stats: {}, workforce: {}, feed: [],
    modelCache: {},
  },
  es: null,

  /* ---------------------------------------------------------- lifecycle */
  async init() {
    const url = new URL(location.href);
    const fromUrl = url.searchParams.get('token');
    if (fromUrl) {
      localStorage.setItem('hermes.token', fromUrl);
      url.searchParams.delete('token');
      history.replaceState({}, '', url.pathname + url.hash);
    }
    this.token = localStorage.getItem('hermes.token') || '';
    if (!this.token) return this.lock();
    try { await this.api('/api/bootstrap'); } catch { return this.lock(); }
    this.start();
  },

  lock() {
    document.getElementById('lock').hidden = false;
    document.getElementById('shell').hidden = true;
    document.getElementById('tokenInput').addEventListener('keydown', e => {
      if (e.key === 'Enter') this.unlock();
    });
  },

  async unlock() {
    const v = document.getElementById('tokenInput').value.trim();
    if (!v) return;
    this.token = v;
    try {
      await this.api('/api/bootstrap');
      localStorage.setItem('hermes.token', v);
      document.getElementById('lock').hidden = true;
      this.start();
    } catch { this.toast('That token was not accepted.', 'err'); }
  },

  async start() {
    document.getElementById('lock').hidden = true;
    document.getElementById('shell').hidden = false;
    this.boot = await this.api('/api/bootstrap');
    this.applyTheme(this.theme());
    this.bindKeys();
    this.renderNav();
    this.connect();
    await this.refresh();
    this.go(location.hash.slice(1) || 'command');
    setInterval(() => this.refresh(true), 6000);
  },

  /* -------------------------------------------------------------- data */
  async api(path, opts = {}) {
    const r = await fetch(path, {
      ...opts,
      headers: { 'Content-Type': 'application/json', 'X-Hermes-Token': this.token,
                 ...(opts.headers || {}) },
    });
    const data = await r.json().catch(() => ({ error: 'Bad response from Hermes' }));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  },

  async refresh(quiet) {
    try {
      const [agents, tasks, approvals, escalations, stats, wf] = await Promise.all([
        this.api('/api/agents'), this.api('/api/tasks?limit=200'),
        this.api('/api/approvals'), this.api('/api/escalations'),
        this.api('/api/stats'), this.api('/api/workforce'),
      ]);
      Object.assign(this.state, {
        agents: agents.agents, tasks: tasks.tasks, approvals: approvals.approvals,
        escalations: escalations.escalations, stats, workforce: wf,
      });
      this.renderNav(); this.renderWorkforce();
      if (!quiet || ['command', 'agents', 'work'].includes(this.view)) this.render();
    } catch (e) { if (!quiet) this.toast(e.message, 'err'); }
  },

  /* ------------------------------------------------------------- events */
  connect() {
    if (this.es) this.es.close();
    this.es = new EventSource(`/api/events?token=${encodeURIComponent(this.token)}`);
    this.es.onmessage = e => this.onEvent(JSON.parse(e.data));
    this.es.onerror = () => setTimeout(() => this.connect(), 4000);
  },

  onEvent(ev) {
    const f = this.describe(ev);
    if (f) {
      this.state.feed.unshift({ ...f, ts: ev.ts });
      this.state.feed = this.state.feed.slice(0, 220);
      if (this.view === 'command') this.paintFeed();
    }
    if (['approval_requested', 'escalation'].includes(ev.kind)) {
      this.refresh(true);
      const msg = ev.kind === 'escalation'
        ? `${ev.payload.agent} needs your input` : `Approval needed: ${ev.payload.tool}`;
      this.toast(msg, 'gold');
      this.notify('Hermes needs you', ev.kind === 'escalation'
        ? `${ev.payload.agent}: ${ev.payload.question || ev.payload.reason || 'is blocked'}`
        : `${ev.payload.agent} wants to run ${ev.payload.tool}`);
    }
    if (ev.kind === 'run_finished') {
      this.notify(ev.payload.status === 'done' ? 'Run finished' : 'Run failed',
        `${ev.payload.agent || 'An agent'} · ${ev.payload.task || ev.payload.status || ''}`.trim());
    }
    if (['run_finished', 'run_started', 'task_picked_up', 'approval_decided',
         'escalation_answered', 'task_retry', 'duty_due'].includes(ev.kind)) this.refresh(true);
  },

  describe(ev) {
    const p = ev.payload || {};
    const M = {
      run_started:     ['info','▸', `<b>${esc(p.agent)}</b> started <b>${esc(p.task)}</b>`, `${p.provider}/${p.model}`],
      task_picked_up:  ['info','↥', `<b>${esc(p.agent)}</b> picked up work`, esc(p.task)],
      step:            null,
      thought:         ['info','◦', 'reasoning', esc(p.text || '').slice(0, 260)],
      tool_call:       ['info','→', `calls <b>${esc(p.tool)}</b>`, esc(JSON.stringify(p.args || {})).slice(0, 240)],
      auto_approved:   ['ok','✓', `auto-approved <b>${esc(p.tool)}</b>`, `${p.autonomy} autonomy`],
      tool_result:     [p.ok ? 'ok' : 'err', p.ok ? '✓' : '✕', `<b>${esc(p.tool)}</b> ${p.ok ? 'succeeded' : 'failed'}`, esc(p.output || '').slice(0, 260)],
      tool_denied:     ['warn','⊘', `you denied <b>${esc(p.tool)}</b>`, ''],
      security_block:  ['err','🛡', `<b>SECURITY BLOCK</b>`, esc(p.reason || '')],
      budget_halt:     ['err','$', `<b>Budget ceiling reached</b>`, esc(p.reason || '')],
      malformed_call:  ['warn','~', `malformed tool call — nudged`, `attempt ${p.attempt}`],
      verifying:       ['info','⚖', 'quality gate checking the work', ''],
      verify_passed:   ['ok','⚖', '<b>quality gate passed</b>', ''],
      verify_rejected: ['warn','⚖', '<b>quality gate rejected</b> — sending it back', esc(p.missing || '').slice(0, 260)],
      approval_requested: ['gold','⏸', `waiting on you for <b>${esc(p.tool)}</b>`, esc(p.agent)],
      approval_decided:['info', p.approved ? '✓' : '✕', `you ${p.approved ? 'approved' : 'denied'} it`, ''],
      escalation:      ['gold','🖐', `<b>${esc(p.agent)}</b> escalated`, esc(p.question || p.reason || '')],
      escalation_answered: ['ok','↩', 'you answered — task requeued', esc(p.answer || '').slice(0,200)],
      task_retry:      ['warn','↻', `retrying <b>${esc(p.task)}</b>`, `attempt ${p.attempt}`],
      duty_due:        ['info','⏱', `standing duty due: <b>${esc(p.duty)}</b>`, ''],
      finished:        null,
      run_finished:    [p.status === 'done' ? 'ok' : 'err', p.status === 'done' ? '●' : '●',
                        `run ${esc(p.status)}`, `${p.steps} steps · $${(p.cost || 0).toFixed(4)}`],
      eval_done:       ['gold','★', `scored <b>${p.score}</b>/100`, esc(p.notes || '')],
      ollama_pull:     ['info','⇩', `Ollama pull ${esc(p.state)}`, esc(p.model)],
      workforce_started: ['ok','⚙', 'workforce dispatcher started', ''],
    };
    const m = M[ev.kind];
    if (!m) return null;
    return { cls: m[0], ico: m[1], text: m[2], det: m[3] };
  },

  /* ---------------------------------------------------------- chrome */
  renderNav() {
    const s = this.state;
    const items = [
      ['command', '◈', 'Command', 0],
      ['agents', '◉', 'Agents', 0],
      ['work', '≡', 'Work', s.tasks.filter(t => t.status === 'queued' || t.status === 'running').length],
      ['inbox', '⏸', 'Inbox', s.approvals.length + s.escalations.length, true],
      ['performance', '★', 'Performance', 0],
      ['runs', '⟲', 'Runs', 0],
      ['security', '🛡', 'Security', 0],
      ['settings', '⚙', 'Settings', 0],
    ];
    document.getElementById('nav').innerHTML = `
      <div class="nav-label">Operations</div>
      ${items.slice(0, 4).map(i => this.navItem(i)).join('')}
      <div class="nav-label">Oversight</div>
      ${items.slice(4).map(i => this.navItem(i)).join('')}`;
  },

  navItem([id, ico, label, badge, alert]) {
    return `<div class="nav-item ${this.view === id ? 'active' : ''}" onclick="App.go('${id}')">
      <span class="nav-ico">${ico}</span><span>${label}</span>
      <span class="nav-badge ${alert ? 'alert' : ''} ${badge ? '' : 'zero'}">${badge}</span></div>`;
  },

  renderWorkforce() {
    const wf = this.state.workforce || {};
    const el = document.getElementById('wfToggle');
    el.classList.toggle('on', !!wf.running);
    document.getElementById('wfState').textContent = wf.running ? 'Workforce on' : 'Workforce paused';
    document.getElementById('wfDetail').textContent = wf.running
      ? `${wf.active || 0} working · ${wf.queued || 0} queued`
      : 'agents will not self-start';
  },

  async toggleWorkforce() {
    const on = this.state.workforce.running;
    await this.api(`/api/workforce/${on ? 'stop' : 'start'}`, { method: 'POST' });
    this.toast(on ? 'Workforce paused — agents will not pick up new work.'
                  : 'Workforce running — agents now pick up their own queue.', on ? '' : 'ok');
    this.refresh();
  },

  go(v) {
    this.view = v; location.hash = v;
    this.renderNav(); this.render();
  },

  render() {
    const V = {
      command: () => this.vCommand(), agents: () => this.vAgents(),
      work: () => this.vWork(), inbox: () => this.vInbox(),
      performance: () => this.vPerformance(), runs: () => this.vRuns(),
      security: () => this.vSecurity(), settings: () => this.vSettings(),
    };
    const T = {
      command: ['Command', 'live view of your workforce'],
      agents: ['Agents', 'your team and what each one may touch'],
      work: ['Work', 'the queue, in progress, and standing duties'],
      inbox: ['Inbox', 'agents waiting on a decision from you'],
      performance: ['Performance', 'how well each agent is actually doing'],
      runs: ['Runs', 'complete history with full transcripts'],
      security: ['Security', 'audit trail, hard limits and spend caps'],
      settings: ['Settings', 'AI backends, keys and safety ceilings'],
    };
    document.getElementById('viewTitle').textContent = T[this.view][0];
    document.getElementById('viewSub').textContent = T[this.view][1];
    document.getElementById('viewActions').innerHTML = this.actions();
    (V[this.view] || V.command)();
  },

  actions() {
    if (this.view === 'agents') return `<button class="btn btn-primary" onclick="App.hireDrawer()">+ Hire an agent</button>`;
    if (this.view === 'work') return `<button class="btn btn-primary" onclick="App.taskDrawer()">+ Assign work</button>`;
    if (this.view === 'command') return `<button class="btn btn-primary" onclick="App.taskDrawer()">+ Assign work</button>`;
    return '';
  },

  set(html) { document.getElementById('view').innerHTML = html; },

  /* ======================================================= COMMAND */
  vCommand() {
    const s = this.state.stats, wf = this.state.workforce;
    const fresh = !s.runs && !localStorage.getItem('hermes.welcomed');
    const running = this.state.tasks.filter(t => t.status === 'running');
    const queued = this.state.tasks.filter(t => t.status === 'queued');
    this.set(`
      ${fresh ? `<div class="card welcome" style="margin-bottom:18px">
        <div class="card-h"><h3>Welcome to Hermes</h3>
          <div class="right"><button class="btn btn-ghost btn-sm" onclick="App.dismissWelcome()">Dismiss</button></div></div>
        <p class="dim" style="font-size:13px;margin-bottom:18px;max-width:70ch">
          Hermes runs a team of AI agents that work like employees: you assign the job, they
          pick it up on their own, do it, and a quality gate checks the work before any of
          them is allowed to call it finished. Three steps to your first result.</p>
        <div class="grid g3">
          ${step(1, 'Check a backend is ready', 'Ollama runs free and offline on this machine. A free Groq or Gemini key works too.', 'Open settings', "App.go('settings')")}
          ${step(2, 'Hire your team', 'Pick from ready-made roles — personal assistant, inbox manager, researcher, file librarian. Each arrives configured, with its own standing duties.', 'Browse roles', 'App.hireDrawer()')}
          ${step(3, 'Give someone a job', 'Describe what you need in plain English. Watch it work, live, in the activity feed below.', 'Assign work', 'App.taskDrawer()')}
        </div>
      </div>` : ''}
      <div class="grid g4" style="margin-bottom:18px">
        ${tile('Agents', s.agents ?? 0, 'on the team')}
        ${tile('Working now', wf.active ?? 0, `${wf.queued ?? 0} queued`, 'green')}
        ${tile('Success rate', s.success_rate == null ? '—' : s.success_rate + '%', `${s.runs || 0} runs`, 'gold')}
        ${tile('Spend', '$' + (s.cost ?? 0).toFixed(4), `${fmtN(s.tokens || 0)} tokens`, (s.cost > 0 ? 'gold' : ''))}
      </div>

      ${(this.state.approvals.length || this.state.escalations.length) ? `
        <div class="card" style="margin-bottom:18px;border-color:rgba(245,185,59,.3)">
          <div class="card-h"><h3>⏸ Waiting on you</h3>
            <span class="count">${this.state.approvals.length + this.state.escalations.length}</span>
            <div class="right"><button class="btn btn-sm" onclick="App.go('inbox')">Open inbox</button></div></div>
          <div class="grid g2">
            ${this.state.escalations.slice(0,2).map(e => this.escCard(e)).join('')}
            ${this.state.approvals.slice(0,2).map(a => this.apprCard(a)).join('')}
          </div>
        </div>` : ''}

      <div class="grid g2">
        <div class="card">
          <div class="card-h"><h3>Live activity</h3>
            <span class="count" id="feedCount">${this.state.feed.length}</span></div>
          <div class="feed" id="feed"></div>
        </div>
        <div>
          <div class="card" style="margin-bottom:14px">
            <div class="card-h"><h3>In progress</h3><span class="count">${running.length}</span></div>
            ${running.length ? running.map(t => this.taskRow(t)).join('')
              : `<div class="empty"><div class="big">◌</div><p>Nothing running. Assign work, or let a standing duty come due.</p></div>`}
          </div>
          <div class="card">
            <div class="card-h"><h3>Queue</h3><span class="count">${queued.length}</span>
              <div class="right">${wf.running ? '<span class="pill p-green">auto-dispatch on</span>'
                : '<span class="pill p-grey">dispatch paused</span>'}</div></div>
            ${queued.length ? queued.slice(0, 6).map(t => this.taskRow(t)).join('')
              : `<div class="empty"><p class="muted">Queue is empty.</p></div>`}
          </div>
        </div>
      </div>`);
    this.paintFeed();
  },

  dismissWelcome() {
    localStorage.setItem('hermes.welcomed', '1');
    document.querySelector('.welcome')?.remove();
  },

  paintFeed() {
    const el = document.getElementById('feed');
    if (!el) return;
    const c = document.getElementById('feedCount');
    if (c) c.textContent = this.state.feed.length;
    el.innerHTML = this.state.feed.length ? this.state.feed.slice(0, 90).map(f => `
      <div class="fe ${f.cls}">
        <span class="fe-t">${time(f.ts)}</span>
        <span class="fe-i">${f.ico}</span>
        <span class="fe-b">${f.text}${f.det ? `<div class="det">${f.det}</div>` : ''}</span>
      </div>`).join('')
      : `<div class="empty"><div class="big">◈</div><p>No activity yet. Everything your agents do appears here as it happens.</p></div>`;
  },

  /* ======================================================== AGENTS */
  vAgents() {
    const a = this.state.agents;
    if (!a.length) return this.set(`<div class="empty"><div class="big">◉</div>
      <p>No agents yet. Create one and give it a job.</p>
      <div style="height:16px"></div>
      <button class="btn btn-primary" onclick="App.hireDrawer()">+ Hire an agent</button></div>`);
    this.set(`<div class="grid g3">${a.map(x => this.agentCard(x)).join('')}</div>`);
    a.forEach(async x => {
      try {
        const c = await this.api(`/api/agents/${x.id}/scorecard`);
        const el = document.getElementById(`sc-${x.id}`);
        if (el) el.innerHTML = `
          <div class="agent-stat"><div class="k">Runs</div><div class="v">${c.runs}</div></div>
          <div class="agent-stat"><div class="k">Success</div><div class="v">${c.success_rate == null ? '—' : c.success_rate + '%'}</div></div>
          <div class="agent-stat"><div class="k">Score</div><div class="v">${c.avg_score == null ? '—' : c.avg_score}</div></div>`;
      } catch {}
    });
  },

  agentCard(a) {
    const lvl = (this.boot.autonomy_levels || []).find(l => l.id === (a.autonomy || 'supervised')) || {};
    const g = a.grants || {};
    const allow = Object.values(g).filter(v => v === 'allow').length;
    const ask = Object.values(g).filter(v => v === 'ask').length;
    return `<div class="agent-card" style="--accent:${esc(a.accent)}" onclick="App.agentDrawer('${a.id}')">
      <div class="agent-top">
        <div class="agent-av">${esc(a.emoji || '🤖')}</div>
        <div style="flex:1;min-width:0">
          <div class="agent-name">${esc(a.name)}
            <span class="status-dot sd-${a.status === 'working' ? 'working' : 'idle'}" style="margin-left:5px"></span></div>
          <div class="agent-role">${esc(a.role || 'no speciality set')}</div>
        </div>
      </div>
      <div class="agent-meta">
        <span class="pill p-indigo">${esc(a.provider)}</span>
        <span class="pill p-grey">${esc(shortModel(a.model))}</span>
        <span class="pill ${a.autonomy === 'autonomous' ? 'p-gold' : a.autonomy === 'trusted' ? 'p-violet' : 'p-grey'}">${esc(lvl.label || 'Supervised')}</span>
      </div>
      <div class="agent-meta" style="margin-top:7px">
        <span class="muted" style="font-size:11.5px">${allow} tools free · ${ask} ask first</span>
      </div>
      <div class="agent-stats" id="sc-${a.id}">
        <div class="agent-stat"><div class="k">Runs</div><div class="v">—</div></div>
      </div>
    </div>`;
  },

  /* ========================================================== WORK */
  vWork() {
    const cols = [['queued', 'Queued'], ['running', 'In progress'], ['done', 'Completed'],
                  ['failed', 'Needs attention']];
    const bucket = st => this.state.tasks.filter(t =>
      st === 'failed' ? ['failed', 'incomplete', 'halted', 'cancelled'].includes(t.status) : t.status === st);
    this.set(`
      <div class="tabs">
        <div class="tab on" onclick="App.workTab(this,'board')">Task board</div>
        <div class="tab" onclick="App.workTab(this,'duties')">Standing duties</div>
      </div>
      <div id="workBody"></div>`);
    this.workTab(document.querySelector('.tab'), 'board');
  },

  async workTab(el, which) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
    el.classList.add('on');
    const body = document.getElementById('workBody');
    if (which === 'board') {
      const cols = [['queued', 'Queued'], ['running', 'In progress'], ['done', 'Completed'],
                    ['failed', 'Needs attention']];
      const bucket = st => this.state.tasks.filter(t =>
        st === 'failed' ? ['failed', 'incomplete', 'halted', 'cancelled'].includes(t.status) : t.status === st);
      body.innerHTML = `<div class="board">${cols.map(([k, label]) => {
        const items = bucket(k);
        return `<div><div class="col-h"><span class="t">${label}</span><span class="n">${items.length}</span></div>
          <div class="col-body">${items.length ? items.slice(0, 30).map(t => this.taskRow(t)).join('')
            : `<div class="card" style="padding:22px;text-align:center"><span class="muted" style="font-size:12px">empty</span></div>`}</div></div>`;
      }).join('')}</div>`;
    } else {
      const d = await this.api('/api/duties');
      this.state.duties = d.duties;
      body.innerHTML = `
        <div class="card" style="margin-bottom:14px">
          <div class="card-h"><h3>Standing duties</h3>
            <span class="count">${d.duties.length}</span>
            <div class="right"><button class="btn btn-primary btn-sm" onclick="App.dutyDrawer()">+ New duty</button></div></div>
          <p class="muted" style="font-size:12.5px;margin-bottom:14px">Recurring responsibilities. Hermes turns each one into a real task on its cadence, and the assigned agent picks it up automatically — no one has to remember.</p>
          ${d.duties.length ? d.duties.map(x => this.dutyRow(x)).join('')
            : `<div class="empty"><div class="big">⏱</div><p>No standing duties. Add one for anything that should happen on a schedule — a daily summary, an hourly check, a weekly tidy-up.</p></div>`}
        </div>`;
    }
  },

  dutyRow(d) {
    const a = this.state.agents.find(x => x.id === d.agent_id);
    return `<div class="task">
      <div class="task-b">
        <div class="task-t">${esc(d.title)} ${d.active ? '' : '<span class="pill p-grey">paused</span>'}</div>
        <div class="task-m">
          <span>${a ? esc(a.emoji + ' ' + a.name) : 'unassigned'}</span>
          <span>every ${cadence(d.cadence_minutes)}</span>
          <span>${d.runs || 0} run${d.runs === 1 ? '' : 's'}</span>
          <span>next ${d.next_run_at ? rel(d.next_run_at) : '—'}</span>
        </div>
      </div>
      <div class="task-acts">
        <button class="btn btn-sm" onclick="App.toggleDuty('${d.id}',${d.active ? 0 : 1})">${d.active ? 'Pause' : 'Resume'}</button>
        <button class="btn btn-sm btn-danger" onclick="App.delDuty('${d.id}')">Delete</button>
      </div></div>`;
  },

  async toggleDuty(id, active) {
    await this.api(`/api/duties/${id}`, { method: 'PATCH', body: JSON.stringify({ active }) });
    this.workTab(document.querySelectorAll('.tab')[1], 'duties');
  },
  async delDuty(id) {
    if (!confirm('Delete this standing duty? Tasks it already created are kept.')) return;
    await this.api(`/api/duties/${id}`, { method: 'DELETE' });
    this.workTab(document.querySelectorAll('.tab')[1], 'duties');
  },

  taskRow(t) {
    const a = this.state.agents.find(x => x.id === t.agent_id);
    const P = { queued: 'p-grey', running: 'p-green', done: 'p-green', failed: 'p-red',
                incomplete: 'p-red', halted: 'p-red', cancelled: 'p-grey' };
    const src = { duty: '⏱ duty', plan: '⚑ self-planned', operator: '' }[t.source] || '';
    return `<div class="task ${t.status === 'running' ? 'running' : ''}">
      <div class="task-b">
        <div class="task-t">${esc(t.title)}</div>
        <div class="task-m">
          <span class="pill ${P[t.status] || 'p-grey'}">${esc(t.status)}</span>
          ${a ? `<span>${esc(a.emoji)} ${esc(a.name)}</span>` : '<span class="muted">unassigned</span>'}
          ${t.priority === 'high' ? '<span class="pill p-gold">high</span>' : ''}
          ${src ? `<span class="muted">${src}</span>` : ''}
          ${t.attempts > 0 ? `<span class="muted">attempt ${t.attempts + 1}</span>` : ''}
          <span class="muted">${rel(t.created_at)}</span>
        </div>
        ${t.result ? `<div class="task-r">${esc(t.result.slice(0, 700))}</div>` : ''}
        ${t.error ? `<div class="task-r" style="color:var(--red)">${esc(t.error.slice(0, 400))}</div>` : ''}
      </div>
      <div class="task-acts">
        ${t.status === 'running'
          ? `<button class="btn btn-sm btn-danger" onclick="App.cancelTask('${t.id}')">Stop</button>`
          : `<button class="btn btn-sm" onclick="App.runTask('${t.id}')">Run now</button>`}
        <button class="btn btn-sm btn-ghost" onclick="App.openRuns('${t.id}')">Runs</button>
      </div></div>`;
  },

  async runTask(id) {
    try { await this.api(`/api/tasks/${id}/run`, { method: 'POST' }); this.toast('Started.', 'ok'); this.refresh(); }
    catch (e) { this.toast(e.message, 'err'); }
  },
  async cancelTask(id) {
    await this.api(`/api/tasks/${id}/cancel`, { method: 'POST' });
    this.toast('Stopping after the current step.'); this.refresh();
  },

  /* ========================================================= INBOX */
  vInbox() {
    const { approvals, escalations } = this.state;
    if (!approvals.length && !escalations.length)
      return this.set(`<div class="empty"><div class="big">✓</div>
        <p>Nothing waiting. Your agents are unblocked.</p>
        <p class="muted" style="margin-top:6px;font-size:12px">Requests to run a risky tool, and questions agents cannot answer alone, land here.</p></div>`);
    this.set(`
      ${escalations.length ? `<div class="card" style="margin-bottom:16px">
        <div class="card-h"><h3>🖐 Questions from your agents</h3><span class="count">${escalations.length}</span></div>
        <p class="muted" style="font-size:12.5px;margin-bottom:14px">An agent hit something it could not decide alone. Answering puts the task straight back in the queue with your answer attached.</p>
        <div class="grid g2">${escalations.map(e => this.escCard(e)).join('')}</div></div>` : ''}
      ${approvals.length ? `<div class="card">
        <div class="card-h"><h3>⏸ Permission requests</h3><span class="count">${approvals.length}</span></div>
        <p class="muted" style="font-size:12.5px;margin-bottom:14px">The agent is paused mid-run, waiting. Approve and it continues immediately.</p>
        <div class="grid g2">${approvals.map(a => this.apprCard(a)).join('')}</div></div>` : ''}`);
  },

  apprCard(a) {
    const spec = (this.boot.tools || []).find(t => t.name === a.tool) || {};
    return `<div class="alert-card">
      <div class="alert-h">
        <span class="who">wants to run <span class="mono">${esc(a.tool)}</span></span>
        <span class="danger-tag dg-${esc(spec.danger || 'low')}">${esc(spec.danger || '')}</span>
      </div>
      <div class="alert-body">${esc(spec.desc || '')}</div>
      <div class="alert-args">${esc(JSON.stringify(a.args, null, 2).slice(0, 900))}</div>
      <div class="alert-acts">
        <button class="btn btn-primary btn-sm" onclick="App.decide('${a.id}',true)">Approve</button>
        <button class="btn btn-danger btn-sm" onclick="App.decide('${a.id}',false)">Deny</button>
      </div></div>`;
  },

  escCard(e) {
    return `<div class="alert-card esc">
      <div class="alert-h"><span class="who">${esc(e.agent_emoji || '')} ${esc(e.agent_name)} is blocked</span></div>
      <div class="alert-body"><b>${esc(e.task_title || '')}</b><br>${esc(e.reason || '')}</div>
      <div class="alert-args">${esc(e.question || '')}</div>
      <textarea id="ans-${e.id}" placeholder="Your answer — the agent resumes with this…" style="margin-bottom:9px"></textarea>
      <div class="alert-acts">
        <button class="btn btn-primary btn-sm" onclick="App.answer('${e.id}')">Answer &amp; resume</button>
        <button class="btn btn-ghost btn-sm" onclick="App.dismissEsc('${e.id}')">Dismiss</button>
      </div></div>`;
  },

  async decide(id, approved) {
    try {
      await this.api(`/api/approvals/${id}/decide`, { method: 'POST', body: JSON.stringify({ approved }) });
      this.toast(approved ? 'Approved — the agent is continuing.' : 'Denied.', approved ? 'ok' : '');
      this.refresh();
    } catch (e) { this.toast(e.message, 'err'); this.refresh(); }
  },
  async answer(id) {
    const v = document.getElementById(`ans-${id}`).value.trim();
    if (!v) return this.toast('Write an answer first.', 'err');
    await this.api(`/api/escalations/${id}/answer`, { method: 'POST', body: JSON.stringify({ answer: v }) });
    this.toast('Answered — task requeued.', 'ok'); this.refresh();
  },
  async dismissEsc(id) {
    await this.api(`/api/escalations/${id}/dismiss`, { method: 'POST' });
    this.refresh();
  },

  /* =================================================== PERFORMANCE */
  async vPerformance() {
    this.set('<div class="empty"><span class="spin"></span></div>');
    const { agents } = await this.api('/api/leaderboard');
    if (!agents.length) return this.set('<div class="empty"><p>No agents yet.</p></div>');
    this.set(`
      <p class="muted" style="font-size:12.5px;margin-bottom:16px">Every completed run can be scored two ways: a judge model grades it automatically against a fixed rubric, and you can rate it yourself. Your rating always wins.</p>
      <div class="grid g3">
        ${agents.map(a => `<div class="card" style="border-left:2px solid ${esc(a.accent)}">
          <div class="agent-top" style="margin-bottom:14px">
            <div class="agent-av">${esc(a.emoji || '🤖')}</div>
            <div><div class="agent-name">${esc(a.name)}</div>
              <div class="agent-role">${esc(a.role || '')}</div></div>
          </div>
          <div class="grid g4" style="gap:10px">
            ${miniStat('Score', a.avg_score == null ? '—' : a.avg_score)}
            ${miniStat('Success', a.success_rate == null ? '—' : a.success_rate + '%')}
            ${miniStat('Runs', a.runs)}
            ${miniStat('Cost', '$' + a.cost.toFixed(3))}
          </div>
          ${a.trend && a.trend.length > 1 ? `<div style="margin-top:14px">
            <div class="stat-k" style="margin-bottom:6px">Score trend</div>
            <div class="spark">${a.trend.map(v => `<i style="height:${Math.max(6, v)}%"></i>`).join('')}</div>
          </div>` : ''}
          <div style="margin-top:14px;display:flex;gap:16px;font-size:11.5px" class="muted">
            <span>${a.avg_steps} avg steps</span><span>${fmtN(a.tokens_in + a.tokens_out)} tokens</span>
            <span>${a.graded} graded</span>
          </div>
        </div>`).join('')}
      </div>`);
  },

  /* ========================================================== RUNS */
  async vRuns(taskId) {
    this.set('<div class="empty"><span class="spin"></span></div>');
    const { runs } = await this.api(taskId ? `/api/tasks/${taskId}/runs` : '/api/runs');
    if (!runs.length) return this.set('<div class="empty"><div class="big">⟲</div><p>No runs yet.</p></div>');
    this.set(`<div class="card"><div class="card-h"><h3>Run history</h3>
      <span class="count">${runs.length}</span>
      ${taskId ? `<div class="right"><button class="btn btn-sm" onclick="App.go('runs')">Show all</button></div>` : ''}</div>
      ${runs.map(r => {
        const a = this.state.agents.find(x => x.id === r.agent_id);
        const t = this.state.tasks.find(x => x.id === r.task_id);
        return `<div class="task" style="cursor:pointer" onclick="App.runDrawer('${r.id}')">
          <div class="task-b">
            <div class="task-t">${esc(t ? t.title : r.task_id)}</div>
            <div class="task-m">
              <span class="pill ${r.status === 'done' ? 'p-green' : r.status === 'running' ? 'p-gold' : 'p-red'}">${esc(r.status)}</span>
              ${a ? `<span>${esc(a.emoji)} ${esc(a.name)}</span>` : ''}
              <span class="muted">${r.steps} steps</span>
              <span class="muted">${fmtN(r.tokens_in + r.tokens_out)} tok</span>
              <span class="muted">$${r.cost.toFixed(4)}</span>
              <span class="muted">${(r.latency_ms / 1000).toFixed(1)}s</span>
              <span class="muted">${rel(r.started_at)}</span>
            </div>
          </div>
          <div class="task-acts"><button class="btn btn-sm">Transcript</button></div>
        </div>`;
      }).join('')}</div>`);
  },

  openRuns(taskId) { this.view = 'runs'; this.renderNav(); this.vRuns(taskId); },

  async runDrawer(id) {
    const { run } = await this.api(`/api/runs/${id}`);
    const human = (run.evals || []).find(e => e.kind === 'human');
    const auto = (run.evals || []).find(e => e.kind === 'auto');
    // Kept so the export button can reach the run without a second fetch.
    this.state.openRun = { ...run, id, task_title: run.task && run.task.title,
                           result: run.task && run.task.result };
    this.drawer(`Run · ${esc(run.task ? run.task.title : id)}`, `
      <div class="grid g4" style="margin-bottom:18px">
        ${miniStat('Status', run.status)} ${miniStat('Steps', run.steps)}
        ${miniStat('Tokens', fmtN(run.tokens_in + run.tokens_out))}
        ${miniStat('Cost', '$' + run.cost.toFixed(4))}
      </div>
      ${run.task && run.task.result ? `<div class="card" style="margin-bottom:16px">
        <div class="card-h"><h3>Final result</h3></div>
        <div class="tr-c" style="max-height:none">${esc(run.task.result)}</div></div>` : ''}

      <div class="card" style="margin-bottom:16px">
        <div class="card-h"><h3>Rate this run</h3>
          ${auto ? `<span class="pill p-gold">judge: ${auto.score}</span>` : ''}
          ${human ? `<span class="pill p-green">yours: ${human.score}</span>` : ''}
          <div class="right"><button class="btn btn-sm" onclick="App.judge('${id}')">Ask judge model</button></div></div>
        ${auto && auto.notes ? `<p class="muted" style="font-size:12.5px;margin-bottom:12px">${esc(auto.notes)}</p>` : ''}
        <div class="row">
          <input type="number" id="rateScore" min="0" max="100" placeholder="0–100" value="${human ? human.score : ''}">
          <input id="rateNotes" placeholder="what was good or bad (optional)" value="${human ? esc(human.notes || '') : ''}">
          <button class="btn btn-primary" style="flex:0 0 auto" onclick="App.rate('${id}')">Save</button>
        </div>
      </div>

      <div class="card-h"><h3>Transcript</h3><span class="count">${run.transcript.length} entries</span>
        <div class="right"><button class="btn btn-sm" onclick="App.exportRun(App.state.openRun)">↓ Export Markdown</button></div></div>
      <div class="tr">${run.transcript.map(e => e.role === 'tool' ? `
        <div class="tr-e tool ${e.ok === false ? 'bad' : ''}">
          <div class="tr-h"><span>${e.ok === false ? '✕' : '✓'}</span><span>${esc(e.tool || 'tool')}</span></div>
          ${e.args ? `<div class="tr-c" style="color:var(--indigo);max-height:120px">${esc(JSON.stringify(e.args))}</div>` : ''}
          <div class="tr-c">${esc(String(e.content || '').slice(0, 4000))}</div></div>` : `
        <div class="tr-e agent">
          <div class="tr-h"><span>◦</span><span>agent · step ${e.step || ''}</span></div>
          <div class="tr-c">${esc(String(e.content || '').slice(0, 4000))}</div></div>`).join('')}
      </div>`);
  },

  async rate(id) {
    const score = parseFloat(document.getElementById('rateScore').value);
    if (isNaN(score)) return this.toast('Enter a score from 0 to 100.', 'err');
    await this.api(`/api/runs/${id}/rate`, { method: 'POST',
      body: JSON.stringify({ score, notes: document.getElementById('rateNotes').value }) });
    this.toast('Rating saved.', 'ok'); this.closeDrawer();
  },
  async judge(id) {
    try {
      this.toast('Judge model is scoring the run…');
      const r = await this.api(`/api/runs/${id}/judge`, { method: 'POST' });
      this.toast(`Scored ${r.score}/100.`, 'ok'); this.runDrawer(id);
    } catch (e) { this.toast(e.message, 'err'); }
  },

  /* ====================================================== SECURITY */
  async vSecurity() {
    this.set('<div class="empty"><span class="spin"></span></div>');
    const [audit, sec] = await Promise.all([
      this.api('/api/security/audit?limit=250'), this.api('/api/security'),
    ]);
    const c = audit.chain;
    this.set(`
      <div class="seal ${c.ok ? '' : 'broken'}" style="margin-bottom:18px">
        <span class="ico">${c.ok ? '🛡' : '⚠'}</span>
        <div><b>${c.ok ? 'Audit chain intact' : `Audit chain BROKEN at entry #${c.broken_at}`}</b>
          <div class="muted" style="font-size:12px">${c.ok
            ? `${c.entries} entries, each cryptographically linked to the one before it. Editing or deleting any row breaks the chain and shows up here.`
            : esc(c.reason || '')}</div></div>
      </div>

      <div class="grid g4" style="margin-bottom:18px">
        ${tile('Protected paths', sec.protected_paths.length, 'always blocked')}
        ${tile('Blocked commands', sec.blocked_commands.length, 'always refused')}
        ${tile('Spend, 24h', '$' + sec.spent_24h.toFixed(4), `ceiling $${sec.caps.per_day}`, 'gold')}
        ${tile('Audit entries', c.entries, 'tamper-evident')}
      </div>

      <div class="grid g2" style="margin-bottom:18px">
        <div class="card"><div class="card-h"><h3>🔒 Never reachable by any agent</h3></div>
          <p class="muted" style="font-size:12.5px;margin-bottom:12px">These hold regardless of an agent's filesystem scope, granted tools, or autonomy level. No prompt can talk past them.</p>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${sec.protected_paths.map(p => `<span class="pill p-grey mono" style="text-transform:none">${esc(p)}</span>`).join('')}</div></div>
        <div class="card"><div class="card-h"><h3>⛔ Commands always refused</h3></div>
          <p class="muted" style="font-size:12.5px;margin-bottom:12px">Even a fully autonomous agent with shell access cannot run these.</p>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${sec.blocked_commands.map(b => `<span class="pill p-red" style="text-transform:none">${esc(b.why)}</span>`).join('')}</div></div>
      </div>

      <div class="card">
        <div class="card-h"><h3>Audit log</h3><span class="count">newest first</span></div>
        <div style="max-height:520px;overflow:auto">
          ${audit.entries.map(e => `<div class="audit-row">
            <span class="a-ts">${time(e.ts)}</span>
            <span class="a-actor">${esc(e.actor)}</span>
            <span class="a-act">${esc(e.action)}</span>
            <span class="a-det" title="${esc(e.detail)}">${esc(e.detail)}</span></div>`).join('')}
        </div></div>`);
  },

  /* ====================================================== SETTINGS */
  async vSettings() {
    const b = this.boot = await this.api('/api/bootstrap');
    const s = b.settings;
    const notify = this.notifyOn();
    this.set(`
      <div class="card" style="margin-bottom:16px">
        <div class="card-h"><h3>This console</h3>
          <span class="count">stored in this browser only</span></div>
        <p class="muted" style="font-size:12.5px;margin-bottom:14px">Preferences for how the console looks and behaves on this machine. They are not part of your Hermes configuration and are never sent anywhere.</p>
        <div class="row" style="gap:10px;flex-wrap:wrap">
          <button class="btn" onclick="App.toggleTheme()">
            ${this.theme() === 'dark' ? '☀ Switch to light' : '☾ Switch to dark'}</button>
          <button class="btn ${notify ? 'btn-primary' : ''}" onclick="App.askNotify()">
            ${notify ? '✓ Desktop notifications on' : 'Turn on desktop notifications'}</button>
          <button class="btn" onclick="App.shortcuts()">Keyboard shortcuts</button>
        </div>
        <p class="muted" style="font-size:12px;margin-top:12px">Notifications only fire while this tab is in the background, and only for the two things worth interrupting you: an agent waiting on your decision, and a run finishing.</p>
      </div>

      <div class="card" style="margin-bottom:16px">
        <div class="card-h"><h3>AI backends</h3>
          <span class="count">${b.providers.filter(p => p.ok).length} of ${b.providers.length} ready</span></div>
        <p class="muted" style="font-size:12.5px;margin-bottom:14px">Hermes talks to all of these. Pick per agent — a cheap local model for routine work, a strong cloud model for the hard jobs.</p>
        ${b.providers.map(p => `<div class="prov ${p.ok ? 'ready' : ''}">
          <div class="prov-i">${({ ollama: '🖥', groq: '⚡', gemini: '✦', anthropic: '◆', openai: '◎', custom: '⚙' })[p.id]}</div>
          <div class="prov-b">
            <div class="prov-n">${esc(p.label)}
              <span class="pill ${p.tier === 'local' ? 'p-green' : p.tier === 'free' ? 'p-indigo' : p.tier === 'paid' ? 'p-gold' : 'p-grey'}">${esc(p.tier)}</span>
              <span class="pill ${p.ok ? 'p-green' : 'p-grey'}">${p.ok ? 'ready' : 'not set up'}</span></div>
            <div class="prov-d">${esc(p.blurb)}</div>
            <div class="prov-d" style="margin-top:3px">${esc(p.detail)}</div>
          </div>
          <div class="prov-a">
            ${p.needs_key ? `<input id="key-${p.id}" type="password" placeholder="${p.has_key ? '•••••••• stored' : 'paste API key'}" style="width:186px">
              <button class="btn btn-sm" onclick="App.saveKey('${p.id}')">Save</button>` : ''}
            ${p.signup ? `<a class="btn btn-sm btn-ghost" href="${esc(p.signup)}" target="_blank" rel="noopener">Get key ↗</a>` : ''}
          </div></div>`).join('')}
        <label class="f" style="margin-top:14px"><span>Custom endpoint base URL</span>
          <input id="set-custom.base_url" value="${esc(s['custom.base_url'] || '')}" placeholder="http://localhost:1234/v1">
          <span class="hint">Any OpenAI-compatible server: LM Studio, vLLM, OpenRouter, Together.</span></label>
      </div>

      <div class="grid g2">
        <div class="card">
          <div class="card-h"><h3>⚖ Quality gate</h3></div>
          <p class="muted" style="font-size:12.5px;margin-bottom:14px">Before an agent is allowed to call a task finished, a second model checks the work was actually done — not merely described. This is what stops an agent reporting success on work it never performed.</p>
          <label class="f"><span>Quality gate</span>
            <select id="set-verify.enabled">
              <option value="1" ${s['verify.enabled'] === '1' ? 'selected' : ''}>On — verify before accepting any result</option>
              <option value="0" ${s['verify.enabled'] === '0' ? 'selected' : ''}>Off — accept the agent's word</option>
            </select></label>
          <div class="row">
            <label class="f"><span>Judge backend</span>
              <select id="set-judge.provider" onchange="App.loadModels('judge')">
                <option value="">the agent's own model</option>
                ${b.providers.map(p => `<option value="${p.id}" ${s['judge.provider'] === p.id ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}
              </select></label>
            <label class="f"><span>Judge model</span>
              <select id="set-judge.model"><option value="${esc(s['judge.model'] || '')}">${esc(s['judge.model'] || '—')}</option></select></label>
          </div>
          <p class="muted" style="font-size:11.5px">A separate judge is stronger than self-review. A small local model works fine here.</p>
        </div>

        <div class="card">
          <div class="card-h"><h3>🛡 Safety ceilings</h3></div>
          <p class="muted" style="font-size:12.5px;margin-bottom:14px">Hard stops. A run that hits a ceiling halts immediately rather than continuing to spend.</p>
          <div class="row">
            <label class="f"><span>Max spend per run (USD)</span>
              <input id="set-safety.max_cost_per_run" value="${esc(s['safety.max_cost_per_run'])}"></label>
            <label class="f"><span>Max spend per day (USD)</span>
              <input id="set-safety.max_cost_per_day" value="${esc(s['safety.max_cost_per_day'])}"></label>
          </div>
          <div class="row">
            <label class="f"><span>Shell timeout (seconds)</span>
              <input id="set-safety.shell_timeout" value="${esc(s['safety.shell_timeout'])}"></label>
            <label class="f"><span>Default step limit</span>
              <input id="set-safety.max_steps" value="${esc(s['safety.max_steps'])}"></label>
          </div>
          <p class="muted" style="font-size:11.5px">Local models cost $0, so caps only bite on paid backends.</p>
        </div>
      </div>

      <div class="card" style="margin-top:16px" id="emailCard"></div>

      <div class="card" style="margin-top:16px">
        <div class="card-h"><h3>Where your data lives</h3></div>
        <div class="grid g2">
          <div><div class="stat-k">Workspace</div><div class="mono" style="margin-top:4px">${esc(b.workspace)}</div>
            <p class="muted" style="font-size:11.5px;margin-top:4px">Default filesystem scope for new agents.</p></div>
          <div><div class="stat-k">Hermes home</div><div class="mono" style="margin-top:4px">${esc(b.home)}</div>
            <p class="muted" style="font-size:11.5px;margin-top:4px">Database, encrypted keys and audit log. Nothing leaves this machine except calls to backends you configure.</p></div>
        </div>
      </div>

      <div style="margin-top:18px;display:flex;gap:9px">
        <button class="btn btn-primary" onclick="App.saveSettings()">Save settings</button>
        <button class="btn" onclick="App.vSettings()">Reload</button>
      </div>`);
    if (s['judge.provider']) this.loadModels('judge', s['judge.model']);
    this.renderEmailCard();
  },

  async renderEmailCard() {
    const el = document.getElementById('emailCard');
    if (!el) return;
    const [cfg, pre] = await Promise.all([this.api('/api/email'), this.api('/api/email/presets')]);
    this.emailPresets = pre.presets;
    el.innerHTML = `
      <div class="card-h"><h3>✉ Email</h3>
        <span class="pill ${cfg.configured ? 'p-green' : 'p-grey'}">${cfg.configured ? 'connected' : 'not connected'}</span>
        ${cfg.configured ? `<div class="right"><button class="btn btn-sm btn-danger" onclick="App.disconnectEmail()">Disconnect</button></div>` : ''}</div>

      <div class="alert-card" style="margin-bottom:16px;box-shadow:none">
        <div class="alert-h"><span class="who">🛡 How Hermes keeps this safe</span></div>
        <div class="alert-body" style="margin:0">
          An inbox is a channel strangers control, so anything an agent reads is treated as
          <b>untrusted data</b>, wrapped in warning markers and scanned for prompt-injection.
          More importantly: <b>sending is never automatic</b>. <span class="mono">email_send</span>
          asks you every single time, at every autonomy level — even a perfectly executed
          injection cannot get one message out without you clicking approve.
        </div>
      </div>

      ${cfg.configured ? `
        <div class="row"><div>
          <div class="stat-k">Account</div><div class="mono" style="margin-top:4px">${esc(cfg.address)}</div></div>
          <div><div class="stat-k">IMAP</div><div class="mono" style="margin-top:4px">${esc(cfg.imap_host)}:${esc(cfg.imap_port)}</div></div>
          <div><div class="stat-k">SMTP</div><div class="mono" style="margin-top:4px">${esc(cfg.smtp_host)}:${esc(cfg.smtp_port)}</div></div>
        </div>
        <label class="f" style="margin-top:14px"><span>Recipient allowlist</span>
          <input id="em-allow" value="${esc(cfg.allowed_recipients)}" placeholder="empty = any recipient allowed">
          <span class="hint">Comma-separated addresses or domains. An agent cannot send anywhere else.</span></label>
        <button class="btn btn-sm" onclick="App.saveAllow()">Save allowlist</button>
      ` : `
        <label class="f"><span>Provider</span>
          <select id="em-preset" onchange="App.applyPreset()">
            ${pre.presets.map(p => `<option value="${p.id}">${esc(p.label)}</option>`).join('')}
          </select>
          <span class="hint" id="em-note">${esc(pre.presets[0].note)}</span></label>
        <div class="row">
          <label class="f"><span>Email address</span><input id="em-address" placeholder="you@example.com" autocomplete="off"></label>
          <label class="f"><span>App password</span><input id="em-password" type="password" placeholder="app password, not your login password" autocomplete="off">
            <span class="hint">Stored encrypted in ~/.hermes. Never sent anywhere but your own mail server.</span></label>
        </div>
        <div class="row">
          <label class="f"><span>IMAP host</span><input id="em-imap_host"></label>
          <label class="f" style="flex:0 0 90px"><span>Port</span><input id="em-imap_port" value="993"></label>
          <label class="f"><span>SMTP host</span><input id="em-smtp_host"></label>
          <label class="f" style="flex:0 0 90px"><span>Port</span><input id="em-smtp_port" value="465"></label>
        </div>
        <label class="f"><span>Recipient allowlist (recommended)</span>
          <input id="em-allow" placeholder="e.g. mycompany.com, boss@example.com">
          <span class="hint">Leave empty to allow any recipient. Setting it is the strongest single control you have here.</span></label>
        <button class="btn btn-primary" onclick="App.connectEmail()">Connect mailbox</button>
      `}`;
    if (!cfg.configured) this.applyPreset();
  },

  applyPreset() {
    const id = val('em-preset');
    const p = (this.emailPresets || []).find(x => x.id === id);
    if (!p) return;
    document.getElementById('em-note').textContent = p.note;
    document.getElementById('em-imap_host').value = p.imap[0];
    document.getElementById('em-imap_port').value = p.imap[1];
    document.getElementById('em-smtp_host').value = p.smtp[0];
    document.getElementById('em-smtp_port').value = p.smtp[1];
  },

  async connectEmail() {
    const body = { address: val('em-address'), password: document.getElementById('em-password').value,
      imap_host: val('em-imap_host'), imap_port: val('em-imap_port'),
      smtp_host: val('em-smtp_host'), smtp_port: val('em-smtp_port'),
      allowed_recipients: val('em-allow') };
    if (!body.address || !body.password) return this.toast('Address and app password are both needed.', 'err');
    this.toast('Testing the connection…');
    try {
      const r = await this.api('/api/email/connect', { method: 'POST', body: JSON.stringify(body) });
      this.toast(r.ok ? 'Mailbox connected.' : r.detail, r.ok ? 'ok' : 'err');
      this.renderEmailCard();
    } catch (e) { this.toast(e.message, 'err'); }
  },
  async saveAllow() {
    await this.api('/api/settings', { method: 'POST',
      body: JSON.stringify({ 'email.allowed_recipients': val('em-allow') }) });
    this.toast('Allowlist saved.', 'ok');
  },
  async disconnectEmail() {
    if (!confirm('Disconnect this mailbox? Agents lose all email access immediately.')) return;
    await this.api('/api/email/disconnect', { method: 'POST' });
    this.toast('Mailbox disconnected.'); this.renderEmailCard();
  },

  async saveKey(pid) {
    const el = document.getElementById(`key-${pid}`);
    const key = el.value.trim();
    if (!key) return this.toast('Paste a key first.', 'err');
    const r = await this.api('/api/keys', { method: 'POST', body: JSON.stringify({ provider: pid, key }) });
    el.value = '';
    this.toast(r.status.ok ? `${pid} connected.` : `Saved, but: ${r.status.detail}`, r.status.ok ? 'ok' : 'err');
    this.vSettings();
  },

  async saveSettings() {
    const body = {};
    document.querySelectorAll('[id^="set-"]').forEach(el => { body[el.id.slice(4)] = el.value; });
    await this.api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    this.toast('Settings saved.', 'ok');
    this.boot = await this.api('/api/bootstrap');
  },

  async loadModels(prefix, selected) {
    const pv = document.getElementById(`set-${prefix}.provider`).value;
    const sel = document.getElementById(`set-${prefix}.model`);
    if (!pv) { sel.innerHTML = '<option value="">—</option>'; return; }
    sel.innerHTML = '<option>loading…</option>';
    const { models } = await this.api(`/api/providers/${pv}/models`);
    sel.innerHTML = models.map(m => `<option value="${esc(m)}" ${m === selected ? 'selected' : ''}>${esc(m)}</option>`).join('')
      || '<option value="">no models found</option>';
  },

  /* ================================================= HIRE DRAWER */
  hireDrawer() {
    const tpls = this.boot.templates || [];
    this.drawer('Hire an agent', `
      <p class="muted" style="font-size:12.5px;margin-bottom:18px">Each role arrives with a job
      description, the right tools already granted, and its own standing duties. Everything is
      editable afterwards — a template is a starting point, not a cage.</p>
      <div class="grid g2">
        ${tpls.map(t => `<div class="tpl" style="--accent:${esc(t.accent)}" onclick="App.pickTemplate('${t.id}')">
          <div class="tpl-top">
            <div class="agent-av">${esc(t.emoji)}</div>
            <div style="min-width:0">
              <div class="agent-name">${esc(t.role || 'Blank agent')}</div>
              <div class="tpl-tag">${esc(t.tagline)}</div>
            </div>
          </div>
          <div class="tpl-about">${esc(t.about)}</div>
          <div class="agent-meta">
            <span class="pill ${t.autonomy === 'autonomous' ? 'p-gold' : t.autonomy === 'trusted' ? 'p-violet' : 'p-grey'}">${esc(t.autonomy)}</span>
            ${t.duties && t.duties.length ? `<span class="pill p-indigo">${t.duties.length} standing duty</span>` : ''}
            ${t.needs_email ? `<span class="pill p-green">uses email</span>` : ''}
          </div>
        </div>`).join('')}
      </div>`,
      `<button class="btn" onclick="App.closeDrawer()">Cancel</button>
       <button class="btn btn-ghost" onclick="App.agentDrawer()">Configure from scratch instead</button>`);
  },

  async pickTemplate(id) {
    const t = (this.boot.templates || []).find(x => x.id === id);
    if (id === 'blank') return this.agentDrawer();
    const ready = this.boot.providers.filter(p => p.ok);
    if (!ready.length) { this.closeDrawer(); this.toast('Set up an AI backend first.', 'err'); return this.go('settings'); }
    this.drawer(`Hire ${esc(t.emoji)} ${esc(t.role)}`, `
      <div class="card" style="background:var(--surface-2);margin-bottom:18px">
        <div class="tpl-about" style="margin:0">${esc(t.about)}</div></div>
      <div class="row">
        <label class="f"><span>Name them</span>
          <input id="h-name" value="${esc(t.name)}" placeholder="e.g. Ada"></label>
        <label class="f"><span>Autonomy</span>
          <select id="h-autonomy">
            ${(this.boot.autonomy_levels || []).map(l =>
              `<option value="${l.id}" ${l.id === t.autonomy ? 'selected' : ''}>${esc(l.label)} — ${esc(l.blurb)}</option>`).join('')}
          </select></label>
      </div>
      <div class="row">
        <label class="f"><span>Backend</span>
          <select id="h-provider" onchange="App.hireModels()">
            ${ready.map(p => `<option value="${p.id}">${esc(p.label)}</option>`).join('')}</select></label>
        <label class="f"><span>Model</span><select id="h-model"><option>loading…</option></select></label>
      </div>
      ${t.duties && t.duties.length ? `<label class="cap" style="cursor:pointer">
        <input type="checkbox" id="h-duties" checked style="width:auto;flex:0 0 auto">
        <div><div class="cap-n" style="font-family:var(--font);font-size:13px">Set up its standing duties</div>
          <div class="cap-d">${t.duties.map(d => esc(d.title) + ' (every ' + cadence(d.cadence_minutes) + ')').join(' · ')}</div></div>
      </label>` : ''}
      ${t.needs_email ? `<div class="alert-card" style="margin-top:14px;box-shadow:none">
        <div class="alert-h"><span class="who">✉ This role uses email</span></div>
        <div class="alert-body" style="margin:0">Connect a mailbox in Settings → Email to unlock it.
        Reading is granted; <b>sending always asks you first</b>, at every autonomy level.</div></div>` : ''}`,
      `<button class="btn" onclick="App.hireDrawer()">← Back</button>
       <button class="btn btn-primary" onclick="App.hire('${id}')">Hire ${esc(t.name || 'agent')}</button>`);
    this.hireModels();
  },

  async hireModels() {
    const sel = document.getElementById('h-model');
    if (!sel) return;
    sel.innerHTML = '<option>loading…</option>';
    try {
      const { models } = await this.api(`/api/providers/${val('h-provider')}/models`);
      sel.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')
        || '<option value="">no models found</option>';
    } catch { sel.innerHTML = '<option value="">could not list models</option>'; }
  },

  async hire(id) {
    const body = { name: val('h-name'), provider: val('h-provider'), model: val('h-model'),
                   autonomy: val('h-autonomy'),
                   with_duties: document.getElementById('h-duties')?.checked ?? true };
    if (!body.name) return this.toast('Give them a name.', 'err');
    try {
      const r = await this.api(`/api/templates/${id}/hire`, { method: 'POST', body: JSON.stringify(body) });
      this.closeDrawer();
      this.toast(`${r.agent.emoji} ${r.agent.name} is on the team.` +
        (r.duties.length ? ` Standing duty set up: ${r.duties.join(', ')}.` : ''), 'ok');
      this.boot = await this.api('/api/bootstrap');
      await this.refresh(); this.go('agents');
      if (r.needs_email) setTimeout(() => this.toast('Connect a mailbox in Settings → Email to unlock their inbox tools.'), 900);
    } catch (e) { this.toast(e.message, 'err'); }
  },

  /* ================================================ AGENT DRAWER */
  async agentDrawer(id) {
    const a = id ? this.state.agents.find(x => x.id === id) : null;
    const inv = id ? await this.api(`/api/agents/${id}/inventory`) : null;
    const grants = a ? a.grants : this.boot.defaults.grants;
    const scopes = a ? a.scopes : { fs_roots: [this.boot.workspace], net_allow: [] };
    const groups = {};
    (this.boot.tools || []).forEach(t => { (groups[t.group] = groups[t.group] || []).push(t); });

    this.drawer(a ? `${a.emoji} ${esc(a.name)}` : 'New agent', `
      <div class="tabs">
        <div class="tab on" onclick="App.dTab(this,'profile')">Profile</div>
        <div class="tab" onclick="App.dTab(this,'caps')">Capabilities</div>
        <div class="tab" onclick="App.dTab(this,'auto')">Autonomy</div>
        ${id ? `<div class="tab" onclick="App.dTab(this,'mem')">Memory</div>` : ''}
      </div>

      <div data-pane="profile">
        <div class="row">
          <label class="f" style="flex:0 0 86px"><span>Icon</span>
            <input id="a-emoji" value="${esc(a ? a.emoji : '🤖')}" maxlength="4" style="text-align:center;font-size:19px"></label>
          <label class="f"><span>Name</span>
            <input id="a-name" value="${esc(a ? a.name : '')}" placeholder="e.g. Atlas"></label>
          <label class="f" style="flex:0 0 92px"><span>Accent</span>
            <input id="a-accent" type="color" value="${esc(a ? a.accent : '#F5B93B')}" style="height:38px;padding:3px"></label>
        </div>
        <label class="f"><span>Speciality</span>
          <input id="a-role" value="${esc(a ? a.role : '')}" placeholder="e.g. Research &amp; synthesis"></label>
        <label class="f"><span>Standing instructions</span>
          <textarea id="a-system" rows="6" placeholder="How this agent should work. Be specific — this is its job description.">${esc(a ? a.system_prompt : '')}</textarea>
          <span class="hint">Written into every task it runs. Concrete standards beat vague encouragement.</span></label>
        <div class="row">
          <label class="f"><span>Backend</span>
            <select id="a-provider" onchange="App.agentModels()">
              ${this.boot.providers.map(p => `<option value="${p.id}" ${a && a.provider === p.id ? 'selected' : ''} ${p.ok ? '' : 'disabled'}>${esc(p.label)}${p.ok ? '' : ' — not set up'}</option>`).join('')}
            </select></label>
          <label class="f"><span>Model</span>
            <select id="a-model"><option value="${esc(a ? a.model : '')}">${esc(a ? a.model : 'choose a backend')}</option></select></label>
        </div>
        <div class="row">
          <label class="f"><span>Temperature</span>
            <input id="a-temp" type="number" step="0.1" min="0" max="2" value="${a ? a.temperature : 0.7}">
            <span class="hint">Lower is more consistent.</span></label>
          <label class="f"><span>Step limit</span>
            <input id="a-steps" type="number" min="1" max="60" value="${a ? a.max_steps : 18}">
            <span class="hint">Hard stop per task.</span></label>
        </div>
      </div>

      <div data-pane="caps" hidden>
        <p class="muted" style="font-size:12.5px;margin-bottom:16px">Exactly what this agent can reach, and whether it needs your say-so. <b>Deny</b> removes the tool entirely — the agent is never even told it exists.</p>
        ${Object.entries(groups).map(([g, ts]) => `<div class="cap-group">
          <div class="cap-group-h">${esc(g)}</div>
          ${ts.map(t => `<div class="cap">
            <div style="min-width:0">
              <div class="cap-n">${esc(t.name)} <span class="danger-tag dg-${esc(t.danger)}">${esc(t.danger)}</span>${t.human_only ? ` <span class="danger-tag dg-human">you approve</span>` : ''}</div>
              <div class="cap-d">${esc(t.desc)}${t.human_only ? ` <b>Allow only means this agent has the tool — every message still waits for you, at every autonomy level.</b>` : ''}</div>
            </div>
            <div class="cap-right"><div class="seg" data-tool="${esc(t.name)}">
              ${['allow', 'ask', 'deny'].map(m => `<button data-m="${m}" class="${(grants[t.name] || 'deny') === m ? 'on' : ''}" onclick="App.setGrant(this)">${m}</button>`).join('')}
            </div></div></div>`).join('')}
        </div>`).join('')}
        <label class="f"><span>Filesystem scope</span>
          <input id="a-roots" value="${esc((scopes.fs_roots || []).join(', '))}">
          <span class="hint">Comma-separated. The agent cannot read or write outside these — and credential paths stay blocked even inside them.</span></label>
        <label class="f"><span>Network allowlist</span>
          <input id="a-net" value="${esc((scopes.net_allow || []).join(', '))}" placeholder="leave empty to allow any host">
          <span class="hint">Domains only, e.g. <span class="mono">docs.python.org, api.github.com</span></span></label>
      </div>

      <div data-pane="auto" hidden>
        <p class="muted" style="font-size:12.5px;margin-bottom:16px">How much this agent does without checking in. Autonomy changes <b>when it asks</b>, never <b>what it may touch</b> — the Capabilities tab is the real boundary.</p>
        ${(this.boot.autonomy_levels || []).map(l => `<label class="cap" style="cursor:pointer">
          <input type="radio" name="a-autonomy" value="${l.id}" ${(a ? a.autonomy : 'supervised') === l.id ? 'checked' : ''} style="width:auto;flex:0 0 auto">
          <div><div class="cap-n" style="font-family:var(--font);font-size:13px">${esc(l.label)}</div>
            <div class="cap-d">${esc(l.blurb)}</div></div></label>`).join('')}
        <label class="f" style="margin-top:18px"><span>Working hours</span>
          <input id="a-shift" value="${esc(a ? (a.shift || 'always') : 'always')}" placeholder="always">
          <span class="hint">Either <span class="mono">always</span>, or a window like <span class="mono">09:00-18:00</span>. Outside its hours the agent will not pick up new work.</span></label>
        ${inv ? `<div class="card" style="margin-top:18px;background:var(--surface-2)">
          <div class="card-h"><h3>Current standing</h3></div>
          <div class="grid g4">
            ${miniStat('Free', inv.counts.allow)} ${miniStat('Ask first', inv.counts.ask)}
            ${miniStat('Blocked', inv.counts.deny)} ${miniStat('Memories', inv.memories.length)}
          </div></div>` : ''}
      </div>

      ${id ? `<div data-pane="mem" hidden>
        <p class="muted" style="font-size:12.5px;margin-bottom:14px">What this agent has chosen to remember across tasks.</p>
        ${inv.memories.length ? inv.memories.map(m => `<div class="cap">
          <div style="min-width:0"><div class="cap-n">${esc(m.key)}</div>
            <div class="cap-d">${esc(m.value)}</div></div>
          <div class="cap-right muted" style="font-size:11px">${rel(m.created_at)}</div></div>`).join('')
          : '<div class="empty"><p>Nothing remembered yet.</p></div>'}
        ${inv.memories.length ? `<button class="btn btn-danger btn-sm" style="margin-top:12px" onclick="App.clearMem('${id}')">Clear all memories</button>` : ''}
      </div>` : ''}`,
      `${id ? `<button class="btn btn-danger" onclick="App.delAgent('${id}')">Retire</button>` : ''}
       <button class="btn" onclick="App.closeDrawer()">Cancel</button>
       <button class="btn btn-primary" onclick="App.saveAgent(${id ? `'${id}'` : 'null'})">${id ? 'Save changes' : 'Create agent'}</button>`);
    this.agentModels(a ? a.model : null);
  },

  dTab(el, pane) {
    el.parentElement.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
    el.classList.add('on');
    document.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== pane; });
  },

  setGrant(btn) {
    btn.parentElement.querySelectorAll('button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
  },

  async agentModels(selected) {
    const pv = document.getElementById('a-provider');
    const sel = document.getElementById('a-model');
    if (!pv || !sel) return;
    sel.innerHTML = '<option>loading…</option>';
    try {
      const { models } = await this.api(`/api/providers/${pv.value}/models`);
      sel.innerHTML = models.map(m => `<option value="${esc(m)}" ${m === selected ? 'selected' : ''}>${esc(m)}</option>`).join('')
        || '<option value="">no models available</option>';
    } catch { sel.innerHTML = '<option value="">could not list models</option>'; }
  },

  async saveAgent(id) {
    const grants = {};
    document.querySelectorAll('.seg[data-tool]').forEach(s => {
      grants[s.dataset.tool] = s.querySelector('button.on')?.dataset.m || 'deny';
    });
    const split = v => v.split(',').map(x => x.trim()).filter(Boolean);
    const body = {
      name: val('a-name'), role: val('a-role'), emoji: val('a-emoji'), accent: val('a-accent'),
      system_prompt: val('a-system'), provider: val('a-provider'), model: val('a-model'),
      temperature: parseFloat(val('a-temp')) || 0.7, max_steps: parseInt(val('a-steps')) || 18,
      autonomy: document.querySelector('[name=a-autonomy]:checked')?.value || 'supervised',
      shift: val('a-shift') || 'always',
      grants, scopes: { fs_roots: split(val('a-roots')), net_allow: split(val('a-net')) },
    };
    if (!body.name) return this.toast('Give the agent a name.', 'err');
    if (!body.model) return this.toast('Choose a model.', 'err');
    try {
      if (id) await this.api(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
      else await this.api('/api/agents', { method: 'POST', body: JSON.stringify(body) });
      this.toast(id ? 'Agent updated.' : `${body.name} is on the team.`, 'ok');
      this.closeDrawer(); this.refresh();
    } catch (e) { this.toast(e.message, 'err'); }
  },

  async delAgent(id) {
    if (!confirm('Retire this agent? Its history and scores are kept.')) return;
    await this.api(`/api/agents/${id}`, { method: 'DELETE' });
    this.toast('Agent retired.'); this.closeDrawer(); this.refresh();
  },
  async clearMem(id) {
    if (!confirm('Erase everything this agent remembers?')) return;
    await this.api(`/api/agents/${id}/memories`, { method: 'DELETE' });
    this.toast('Memories cleared.'); this.agentDrawer(id);
  },

  /* ================================================= TASK DRAWER */
  taskDrawer() {
    if (!this.state.agents.length) return this.toast('Create an agent first.', 'err');
    this.drawer('Assign work', `
      <label class="f"><span>What needs doing</span>
        <input id="t-title" placeholder="e.g. Summarise every PDF in my Reports folder"></label>
      <label class="f"><span>The brief</span>
        <textarea id="t-brief" rows="7" placeholder="Everything the agent needs: where the files are, what good looks like, what to produce. Detail here is what separates a task that gets done from one that gets escalated back to you."></textarea></label>
      <div class="row">
        <label class="f"><span>Assign to</span>
          <select id="t-agent">${this.state.agents.map(a =>
            `<option value="${a.id}">${esc(a.emoji)} ${esc(a.name)} — ${esc(a.role || 'generalist')}</option>`).join('')}</select></label>
        <label class="f"><span>Priority</span>
          <select id="t-priority">
            <option value="high">High — jump the queue</option>
            <option value="normal" selected>Normal</option>
            <option value="low">Low — whenever</option>
          </select></label>
      </div>
      <label class="cap" style="cursor:pointer">
        <input type="checkbox" id="t-run" checked style="width:auto;flex:0 0 auto">
        <div><div class="cap-n" style="font-family:var(--font);font-size:13px">Start immediately</div>
          <div class="cap-d">Otherwise it waits in the queue for the dispatcher to pick it up.</div></div></label>`,
      `<button class="btn" onclick="App.closeDrawer()">Cancel</button>
       <button class="btn btn-primary" onclick="App.saveTask()">Assign</button>`);
  },

  async saveTask() {
    const body = { title: val('t-title'), brief: val('t-brief'), agent_id: val('t-agent'),
                   priority: val('t-priority'), run: document.getElementById('t-run').checked };
    if (!body.title) return this.toast('Describe what needs doing.', 'err');
    try {
      await this.api('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
      this.toast(body.run ? 'Assigned and started.' : 'Added to the queue.', 'ok');
      this.closeDrawer(); this.refresh();
    } catch (e) { this.toast(e.message, 'err'); }
  },

  /* ================================================= DUTY DRAWER */
  dutyDrawer() {
    if (!this.state.agents.length) return this.toast('Create an agent first.', 'err');
    this.drawer('New standing duty', `
      <p class="muted" style="font-size:12.5px;margin-bottom:16px">A responsibility that repeats. Hermes creates the task each time it comes due and the agent picks it up on its own.</p>
      <label class="f"><span>Duty</span>
        <input id="d-title" placeholder="e.g. Daily summary of new files in Downloads"></label>
      <label class="f"><span>Standing brief</span>
        <textarea id="d-brief" rows="5" placeholder="Exactly what to do each time it runs."></textarea></label>
      <div class="row">
        <label class="f"><span>Owner</span>
          <select id="d-agent">${this.state.agents.map(a =>
            `<option value="${a.id}">${esc(a.emoji)} ${esc(a.name)}</option>`).join('')}</select></label>
        <label class="f"><span>How often</span>
          <select id="d-cadence">
            <option value="15">Every 15 minutes</option><option value="60">Hourly</option>
            <option value="240">Every 4 hours</option><option value="1440" selected>Daily</option>
            <option value="10080">Weekly</option>
          </select></label>
      </div>
      <label class="cap" style="cursor:pointer">
        <input type="checkbox" id="d-now" style="width:auto;flex:0 0 auto">
        <div><div class="cap-n" style="font-family:var(--font);font-size:13px">Run the first one now</div>
          <div class="cap-d">Otherwise the first run happens after one full interval.</div></div></label>`,
      `<button class="btn" onclick="App.closeDrawer()">Cancel</button>
       <button class="btn btn-primary" onclick="App.saveDuty()">Create duty</button>`);
  },

  async saveDuty() {
    const body = { title: val('d-title'), brief: val('d-brief'), agent_id: val('d-agent'),
                   cadence_minutes: parseInt(val('d-cadence')),
                   start_now: document.getElementById('d-now').checked };
    if (!body.title) return this.toast('Name the duty.', 'err');
    await this.api('/api/duties', { method: 'POST', body: JSON.stringify(body) });
    this.toast('Standing duty created.', 'ok');
    this.closeDrawer(); this.go('work');
  },

  /* ==================================================== chrome bits */

  /* =================================================== keyboard layer
     A console you run all day should be reachable without the mouse. Three
     pieces: a command palette over everything nameable, single-key navigation,
     and a sheet that tells you both exist. State lives in `kb` so the global
     handler can tell "palette open" from "typing in a form".
     ================================================================== */
  kb: { open: null, items: [], sel: 0, chord: 0 },

  isMac: navigator.platform.toUpperCase().includes('MAC'),

  bindKeys() {
    document.getElementById('palKey').textContent = this.isMac ? '⌘K' : 'Ctrl K';

    document.addEventListener('keydown', e => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')
                     || document.activeElement?.isContentEditable;

      // ⌘K / Ctrl+K reaches the palette even from inside a field — that is the
      // whole point of a palette shortcut.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault(); return this.palette();
      }
      if (e.key === 'Escape') {
        if (this.kb.open) { e.preventDefault(); return this.closeOverlay(); }
        if (document.getElementById('drawer')) { e.preventDefault(); return this.closeDrawer(); }
        return;
      }
      if (this.kb.open === 'palette') return this.palKey(e);
      if (this.kb.open) return;
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;

      // `g` then a letter — the two-key idiom people already know from
      // GitHub and Gmail. The chord lapses after a second.
      if (this.kb.chord && Date.now() - this.kb.chord < 1000) {
        const dest = { c: 'command', a: 'agents', w: 'work', i: 'inbox',
                       p: 'performance', r: 'runs', s: 'security', ',': 'settings' }[e.key];
        this.kb.chord = 0;
        if (dest) { e.preventDefault(); this.go(dest); }
        return;
      }
      if (e.key === 'g') { this.kb.chord = Date.now(); return; }
      // Some layouts report shift+/ rather than the composed '?'.
      if (e.key === '?' || (e.shiftKey && e.key === '/')) {
        e.preventDefault(); return this.shortcuts();
      }
      if (e.key === '/' && !e.shiftKey) { e.preventDefault(); return this.palette(); }
      if (e.key === 'n') { e.preventDefault(); return this.taskDrawer(); }
      if (e.key === 't') { e.preventDefault(); return this.toggleTheme(); }
    });
  },

  closeOverlay() {
    this.kb.open = null;
    document.getElementById('overlay').innerHTML = '';
  },

  /* ------------------------------------------------------ command palette */
  palItems() {
    const views = [
      ['command', '◈', 'Command', 'live view of your workforce'],
      ['agents', '◉', 'Agents', 'your team and what each one may touch'],
      ['work', '≡', 'Work', 'the queue, in progress, and standing duties'],
      ['inbox', '⏸', 'Inbox', 'agents waiting on a decision from you'],
      ['performance', '★', 'Performance', 'how well each agent is actually doing'],
      ['runs', '⟲', 'Runs', 'complete history with full transcripts'],
      ['security', '🛡', 'Security', 'audit trail, hard limits and spend caps'],
      ['settings', '⚙', 'Settings', 'AI backends, keys and safety ceilings'],
    ].map(([id, ico, label, desc]) => ({
      sec: 'Go to', ico, title: label, desc,
      hint: 'g ' + (id === 'settings' ? ',' : id[0]),
      run: () => this.go(id),
    }));

    const agents = this.state.agents.map(a => ({
      sec: 'Assign work to', ico: a.emoji || '◉', title: a.name,
      desc: a.role || 'no speciality set',
      run: () => this.taskDrawer(a.id),
    }));

    const wfOn = this.state.workforce.running;
    const actions = [
      { sec: 'Do', ico: '+', title: 'Assign work', desc: 'queue a task for an agent',
        hint: 'n', run: () => this.taskDrawer() },
      { sec: 'Do', ico: '◉', title: 'Hire an agent', desc: 'start from a template or blank',
        run: () => { this.go('agents'); this.hireDrawer(); } },
      { sec: 'Do', ico: wfOn ? '⏸' : '▶', title: wfOn ? 'Pause the workforce' : 'Start the workforce',
        desc: wfOn ? 'agents stop picking up new work' : 'agents begin working their own queue',
        run: () => this.toggleWorkforce() },
      { sec: 'Do', ico: this.theme() === 'dark' ? '☀' : '☾',
        title: this.theme() === 'dark' ? 'Switch to light' : 'Switch to dark',
        desc: 'remembered on this browser', hint: 't', run: () => this.toggleTheme() },
      { sec: 'Do', ico: '?', title: 'Keyboard shortcuts', desc: 'every key this console listens for',
        hint: '?', run: () => this.shortcuts() },
    ];
    return [...actions, ...views, ...agents];
  },

  palette() {
    this.closeDrawer();
    this.kb.open = 'palette';
    this.kb.items = this.palItems();
    this.kb.sel = 0;
    document.getElementById('overlay').innerHTML = `
      <div class="scrim" onclick="App.closeOverlay()">
        <div class="palette" onclick="event.stopPropagation()">
          <div class="pal-in">
            <span>◈</span>
            <input id="palQ" placeholder="Search views, agents and actions…"
                   autocomplete="off" spellcheck="false" oninput="App.palFilter()">
          </div>
          <div class="pal-list" id="palList"></div>
          <div class="pal-foot">
            <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
            <span><kbd>↵</kbd> open</span>
            <span><kbd>esc</kbd> close</span>
            <span style="margin-left:auto"><kbd>?</kbd> all shortcuts</span>
          </div>
        </div>
      </div>`;
    this.palPaint();
    document.getElementById('palQ').focus();
  },

  palFilter() { this.kb.sel = 0; this.palPaint(); },

  palPaint() {
    const q = (document.getElementById('palQ')?.value || '').toLowerCase().trim();
    const list = document.getElementById('palList');

    // Subsequence matching so "asw" finds "Assign work" — but ranked, because a
    // loose match on some other row's description should never outrank the row
    // whose name you actually typed.
    const subseq = s => {
      let i = 0;
      for (const ch of s) if (ch === q[i]) i++;
      return i === q.length;
    };
    const score = it => {
      const title = it.title.toLowerCase(), desc = (it.desc || '').toLowerCase();
      if (title.startsWith(q)) return 0;
      if (title.includes(q)) return 1;
      if (desc.includes(q)) return 2;
      if (subseq(title)) return 3;
      if (subseq(desc) || subseq(it.sec.toLowerCase())) return 4;
      return -1;
    };

    let shown;
    if (!q) {
      shown = this.kb.items.slice();
    } else {
      shown = this.kb.items
        .map((it, i) => ({ it, i, s: score(it) }))
        .filter(r => r.s >= 0)
        .sort((a, b) => a.s - b.s || a.i - b.i)
        .map(r => r.it);
    }
    this.kb.shown = shown;
    if (this.kb.sel >= shown.length) this.kb.sel = Math.max(0, shown.length - 1);

    if (!shown.length) {
      list.innerHTML = `<div class="pal-empty">Nothing matches “${esc(q)}”.</div>`;
      return;
    }
    let html = '', sec = '';
    shown.forEach((it, i) => {
      // Section headings only make sense in the unfiltered list; once results
      // are ranked by relevance they no longer arrive grouped.
      if (!q && it.sec !== sec) { sec = it.sec; html += `<div class="pal-sec">${esc(sec)}</div>`; }
      html += `<div class="pal-row ${i === this.kb.sel ? 'on' : ''}" data-i="${i}"
                    onmousemove="App.palHover(${i})" onclick="App.palRun(${i})">
        <span class="pal-ico">${esc(it.ico)}</span>
        <span class="pal-body">
          <span class="pal-t">${esc(it.title)}</span>
          <span class="pal-d">${q ? esc(it.sec) + ' · ' : ''}${esc(it.desc || '')}</span>
        </span>
        ${it.hint ? `<kbd class="pal-hint">${esc(it.hint)}</kbd>` : ''}
      </div>`;
    });
    list.innerHTML = html;
    list.querySelector('.pal-row.on')?.scrollIntoView({ block: 'nearest' });
  },

  palHover(i) {
    if (i === this.kb.sel) return;
    this.kb.sel = i; this.palPaint();
  },

  palKey(e) {
    const n = (this.kb.shown || []).length;
    if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) {
      e.preventDefault(); this.kb.sel = n ? (this.kb.sel + 1) % n : 0; this.palPaint();
    } else if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) {
      e.preventDefault(); this.kb.sel = n ? (this.kb.sel - 1 + n) % n : 0; this.palPaint();
    } else if (e.key === 'Enter') {
      e.preventDefault(); this.palRun(this.kb.sel);
    }
  },

  palRun(i) {
    const it = (this.kb.shown || [])[i];
    if (!it) return;
    this.closeOverlay();
    it.run();
  },

  /* -------------------------------------------------------- shortcut sheet */
  shortcuts() {
    this.kb.open = 'sheet';
    const mod = this.isMac ? '⌘' : 'Ctrl';
    const row = (label, keys) =>
      `<div class="sheet-row"><span>${esc(label)}</span><div>${keys.map(k => `<kbd>${esc(k)}</kbd>`).join('')}</div></div>`;
    document.getElementById('overlay').innerHTML = `
      <div class="scrim" onclick="App.closeOverlay()">
        <div class="sheet" onclick="event.stopPropagation()">
          <div class="sheet-h"><b>Keyboard shortcuts</b><span>press <kbd>esc</kbd> to close</span></div>
          <div class="sheet-grid">
            <div>
              <div class="sheet-sec">Anywhere</div>
              ${row('Command palette', [mod, 'K'])}
              ${row('Command palette', ['/'])}
              ${row('Assign work', ['n'])}
              ${row('Light / dark', ['t'])}
              ${row('This sheet', ['?'])}
              ${row('Close palette or drawer', ['esc'])}
            </div>
            <div>
              <div class="sheet-sec">Go to</div>
              ${row('Command', ['g', 'c'])}
              ${row('Agents', ['g', 'a'])}
              ${row('Work', ['g', 'w'])}
              ${row('Inbox', ['g', 'i'])}
              ${row('Performance', ['g', 'p'])}
              ${row('Runs', ['g', 'r'])}
              ${row('Security', ['g', 's'])}
              ${row('Settings', ['g', ','])}
            </div>
          </div>
        </div>
      </div>`;
  },

  /* ================================================================ theme */
  theme() { return document.documentElement.getAttribute('data-theme') || 'dark'; },

  applyTheme(next) {
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('hermes.theme', next); } catch (e) { /* private mode */ }
    const btn = document.getElementById('themeBtn');
    if (btn) {
      btn.textContent = next === 'dark' ? '☀' : '☾';
      btn.title = next === 'dark' ? 'Switch to light' : 'Switch to dark';
    }
  },

  toggleTheme() { this.applyTheme(this.theme() === 'dark' ? 'light' : 'dark'); },

  /* ======================================================== notifications
     An agent console is something you leave running. A run that finishes or an
     approval that lands while the tab is in the background is exactly the thing
     you wanted to be told about — but only ever after you ask for it.
     ===================================================================== */
  notifyOn() {
    try { return localStorage.getItem('hermes.notify') === '1'; } catch (e) { return false; }
  },

  async askNotify() {
    if (!('Notification' in window)) {
      return this.toast('This browser has no notification support.', 'err');
    }
    if (this.notifyOn()) {
      localStorage.setItem('hermes.notify', '0');
      this.toast('Desktop notifications off.');
      return this.render();
    }
    const ok = await Notification.requestPermission();
    if (ok !== 'granted') return this.toast('Your browser refused notification permission.', 'err');
    localStorage.setItem('hermes.notify', '1');
    this.toast('Desktop notifications on — approvals and finished runs.', 'ok');
    this.render();
  },

  notify(title, body) {
    if (!this.notifyOn() || !('Notification' in window)) return;
    if (Notification.permission !== 'granted') return;
    if (!document.hidden) return;      // you are looking at it already
    try {
      const n = new Notification(title, { body, tag: 'hermes', icon: BRAND_ICON });
      n.onclick = () => { window.focus(); n.close(); };
    } catch (e) { /* some browsers refuse from a non-worker context */ }
  },

  /* ---------------------------------------------------------- run export */
  exportRun(run) {
    const lines = [
      `# ${run.task_title || 'Hermes run'}`, '',
      `- **Agent:** ${run.agent_name || run.agent_id}`,
      `- **Model:** ${run.provider}/${run.model}`,
      `- **Status:** ${run.status}`,
      `- **Steps:** ${run.steps}`,
      `- **Cost:** $${Number(run.cost || 0).toFixed(4)}`,
      `- **Started:** ${new Date((run.started_at || 0) * 1000).toISOString()}`,
      '', '## Transcript', '',
    ];
    for (const e of run.transcript || []) {
      if (e.role === 'tool') {
        lines.push(`### ${e.ok === false ? '✗' : '→'} ${e.tool}`, '', '```', String(e.content ?? '').trim(), '```', '');
      } else {
        lines.push(`### ${e.role}`, '', String(e.content ?? '').trim(), '');
      }
    }
    if (run.result) lines.push('## Result', '', run.result, '');
    const name = `hermes-run-${(run.id || 'export').slice(0, 8)}.md`;
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    this.toast(`Saved ${name}`, 'ok');
  },

  drawer(title, body, footer = '') {
    this.closeDrawer();
    document.body.insertAdjacentHTML('beforeend', `
      <div class="scrim" id="scrim" onclick="App.closeDrawer()"></div>
      <div class="drawer" id="drawer">
        <div class="drawer-h"><h2>${title}</h2>
          <div style="margin-left:auto"><button class="btn btn-ghost btn-sm" onclick="App.closeDrawer()">✕</button></div></div>
        <div class="drawer-b">${body}</div>
        ${footer ? `<div class="drawer-f">${footer}</div>` : ''}
      </div>`);
  },

  closeDrawer() {
    document.getElementById('scrim')?.remove();
    document.getElementById('drawer')?.remove();
  },

  toast(msg, kind = '') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `<span>${({ ok: '✓', err: '✕', gold: '⏸' })[kind] || '•'}</span><span>${esc(msg)}</span>`;
    document.getElementById('toasts').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300); }, 4200);
  },
};

/* ------------------------------------------------------------- helpers */
const BRAND_ICON = document.querySelector('link[rel="icon"]')?.getAttribute('href') || '';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function val(id) { return document.getElementById(id)?.value.trim() ?? ''; }
function fmtN(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }
function shortModel(m) { return String(m || '').length > 22 ? m.slice(0, 20) + '…' : m; }
function time(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
function rel(ts) {
  if (!ts) return '—';
  const d = Date.now() / 1000 - ts;
  if (Math.abs(d) < 60) return d >= 0 ? 'just now' : 'in <1m';
  if (d < 0) return 'in ' + (Math.abs(d) < 3600 ? Math.round(-d / 60) + 'm' : Math.round(-d / 3600) + 'h');
  if (d < 3600) return Math.round(d / 60) + 'm ago';
  if (d < 86400) return Math.round(d / 3600) + 'h ago';
  return Math.round(d / 86400) + 'd ago';
}
function cadence(m) {
  if (m < 60) return m + ' min';
  if (m < 1440) return (m / 60) + ' hour' + (m === 60 ? '' : 's');
  if (m < 10080) return (m / 1440) + ' day' + (m === 1440 ? '' : 's');
  return (m / 10080) + ' week' + (m === 10080 ? '' : 's');
}
function tile(k, v, note, cls = '') {
  return `<div class="stat ${cls}"><div class="stat-k">${esc(k)}</div>
    <div class="stat-v">${esc(v)}</div><div class="stat-n">${esc(note || '')}</div></div>`;
}
function step(n, title, body, cta, action) {
  return `<div class="step">
    <div class="step-n">${n}</div>
    <div class="step-t">${esc(title)}</div>
    <div class="step-b">${esc(body)}</div>
    <button class="btn btn-sm" onclick="${action}">${esc(cta)}</button></div>`;
}
function miniStat(k, v) {
  return `<div><div class="stat-k">${esc(k)}</div>
    <div style="font-size:17px;font-weight:650;margin-top:3px;font-variant-numeric:tabular-nums">${esc(v)}</div></div>`;
}

window.addEventListener('DOMContentLoaded', () => App.init());
