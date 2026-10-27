/*
 * Cohort Lens — the guided five-step review.
 *
 *   1 Upload         mark which sources inside each document matter most
 *   2 Primary agent  row-by-row PASS / REVIEW / FAIL with citations
 *   3 Swarm          10 adversarial reviewers score each verdict
 *   4 Consensus      mean reviewer confidence; threshold decides routing
 *   5 Worklist       research associate resolves routed rows, exports packet
 *
 * Plain JavaScript, no build step. All state lives in `state`; every change
 * calls render(), which rebuilds #app from template strings. Clicks are handled
 * by one delegated listener that reads data-action attributes.
 */
(function () {
  'use strict';

  const { agents, docs, patients, rules, defaultFocus } = window.COHORT_LENS_DATA;
  const STEP_LABELS = ['Upload', 'Primary agent', 'Swarm', 'Consensus', 'Worklist'];
  const DISSENT_BELOW = 0.5; // a reviewer "dissents" when its confidence is below this

  const state = {
    step: 1,
    maxStep: 1,               // furthest step reached; later steps stay locked
    busy: false,              // true while a simulated agent run is in progress
    selectedId: 'P-1007',     // participant highlighted across steps 2–5
    cite: { pid: 'P-1007', i: 0 },
    focus: new Set(defaultFocus),
    uploads: [],              // files added in step 1 (queued, not analysed)
    threshold: 0.8,
    routeCitationFaults: true,
    sort: 'severity',         // 'severity' | 'consensus'
    decisions: {},            // { [participantId]: 'uphold' | 'override' | 'escalate' }
    exported: false,
  };

  const app = document.getElementById('app');
  const fileInput = document.getElementById('file-input');

  /* ── helpers ─────────────────────────────────────────── */

  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const f2 = n => n.toFixed(2);
  const pct = n => (n * 100).toFixed(1) + '%';
  const verdictTag = v => (v === 'PASS' ? 'tag tag-accent' : v === 'FAIL' ? 'tag tag-accent-2' : 'tag tag-neutral');

  // Misregistered process-plate numeral from the Broadsheet design system
  const plateNumber = text => `
    <div class="cmyk-num"><span class="paper">${esc(text)}</span><span class="plate plate-c" aria-hidden="true">${esc(text)}</span><span class="plate plate-m" aria-hidden="true">${esc(text)}</span><span class="plate plate-y" aria-hidden="true">${esc(text)}</span></div>`;

  // Heatmap cell color: ink for agreement, magenta for dissent; depth = strength
  const heatColor = c => c >= DISSENT_BELOW
    ? `color-mix(in srgb, var(--color-text) ${Math.round(10 + (c - 0.5) * 120)}%, var(--color-bg))`
    : `color-mix(in srgb, var(--color-accent-2) ${Math.round(45 + (0.5 - c) * 110)}%, var(--color-bg))`;

  /* ── analysis: turn raw data into scored participants ── */

  function analyse() {
    return patients.map((p, pi) => {
      const votes = agents.map((a, i) => {
        const o = p.ov && p.ov[a.key];
        // Reviewers without an explicit override get a small deterministic jitter around `base`
        const conf = o ? o[0] : Math.min(0.99, p.base + (((i * 7 + pi * 3) % 5) - 2) * 0.015);
        let note = o ? o[1] : (p.verdict === 'PASS' ? a.ok : `Concurs with ${p.verdict}.`);
        if (!o && a.key === 'cite') note = `${p.cites.length}/${p.cites.length} citations resolve and match the source line.`;
        return { agent: a, conf, note, dissent: conf < DISSENT_BELOW };
      });
      const consensus = votes.reduce((s, v) => s + v.conf, 0) / votes.length;
      const sd = Math.sqrt(votes.reduce((s, v) => s + (v.conf - consensus) ** 2, 0) / votes.length);
      const fault = p.cites.some(c => !c.ok);
      const routed = consensus < state.threshold || (state.routeCitationFaults && fault);
      const byConf = votes.slice().sort((a, b) => a.conf - b.conf);
      return Object.assign({}, p, { votes, byConf, consensus, sd, fault, routed, dissent: votes.filter(v => v.dissent) });
    });
  }

  function worklist(P) {
    const list = P.filter(p => p.routed);
    if (state.sort === 'severity') list.sort((a, b) => (b.sev || 0) - (a.sev || 0) || a.consensus - b.consensus);
    else list.sort((a, b) => a.consensus - b.consensus);
    return list;
  }

  /* ── shared pieces ───────────────────────────────────── */

  function stepNav() {
    return `<nav class="steps" aria-label="Review steps">${STEP_LABELS.map((label, i) => {
      const n = i + 1;
      const current = n === state.step;
      const locked = n > state.maxStep;
      const done = !current && n < state.maxStep;
      return `<button class="step-link" data-action="go" data-step="${n}" ${locked ? 'disabled' : ''} ${current ? 'aria-current="step"' : ''}>
        <span class="step-num">0${n}</span><span class="step-label">${label}</span>${done ? '<span class="step-done">done</span>' : ''}
      </button>`;
    }).join('')}</nav>`;
  }

  function stepHead(n, kicker, title, lede) {
    return `<div class="step-head">${plateNumber(n)}
      <div>
        <div class="kicker">Step ${n} of 5 · ${kicker}</div>
        <h2 class="step-title">${title}</h2>
        <p class="lede">${lede}</p>
      </div>
    </div>`;
  }

  function footerNav(P) {
    if (state.step === 5) return `<div class="footer-nav"><button class="btn btn-secondary" data-action="back">Back</button></div>`;
    const labels = ['Run primary agent', 'Unleash the swarm', 'Score consensus', 'Build my worklist'];
    const busy = ['Agent reading 7 documents…', 'Ten reviewers challenging 12 verdicts…', 'Scoring consensus…', 'Building worklist…'];
    const routed = P.filter(p => p.routed).length;
    const hints = [
      `7 documents parsed · ${state.focus.size} sources marked for close attention.`,
      'Next: ten adversarial reviewers check every verdict and citation.',
      'Next: aggregate the swarm into a consensus score per participant.',
      `${routed} participants will be routed to you at threshold ${f2(state.threshold)}.`,
    ];
    const i = state.step - 1;
    return `<div class="footer-nav">
      ${state.step > 1 ? '<button class="btn btn-secondary" data-action="back">Back</button>' : ''}
      <button class="btn btn-primary" data-action="next" ${state.busy ? 'disabled' : ''}>${state.busy ? busy[i] : labels[i]}</button>
      <span class="small muted">${state.busy ? '' : hints[i]}</span>
    </div>`;
  }

  /* ── step 1: upload + mark important sources ─────────── */

  function step1() {
    const docRows = docs.map(d => `
      <div class="doc row-rule">
        <span class="doc-code">${d.code}</span>
        <div>
          <div class="doc-title"><span class="doc-name">${esc(d.name)}</span><span class="small muted">${esc(d.meta)}</span><span class="tag tag-accent">Parsed</span></div>
          <div class="chips">${d.sections.map((s, i) => {
            const key = `${d.code}:${i}`;
            return `<button class="chip" data-action="toggle-focus" data-key="${key}" aria-pressed="${state.focus.has(key)}">${esc(s)}</button>`;
          }).join('')}</div>
        </div>
      </div>`).join('');

    const uploadRows = state.uploads.map(u => `
      <div class="doc row-rule">
        <span class="doc-code">NEW</span>
        <div class="doc-title"><span class="doc-name">${esc(u.name)}</span><span class="small muted">${esc(u.size)}</span><span class="tag tag-neutral">Queued</span></div>
      </div>`).join('');

    return stepHead(1, 'Upload', 'Assemble the document batch',
      'Bring in the protocol, the DSMB charter and the data exports for this data cut. Every file is split into addressable rows and clauses, so any claim can be traced to a single line. Then mark the sources inside each document that deserve the closest attention — agents and reviewers weigh those first.') + `
      <div class="split">
        <div class="split-main">
          <div class="dropzone" data-dropzone>
            <div><div class="dropzone-title">Drop files here</div><div class="small muted">PDF, CSV or XLSX · up to 200 MB per batch</div></div>
            <button class="btn btn-secondary" data-action="browse">Browse files</button>
          </div>
          <div>
            <div class="section-head"><h4>Mark what matters</h4><span class="muted"><strong>${state.focus.size}</strong> sources marked for close attention</span></div>
            ${docRows}${uploadRows}
          </div>
        </div>
        <aside class="split-aside">
          <h4>Stopping rules the agent will apply</h4>
          <p class="small muted">Extracted from DSMB charter v2.0, section 6.</p>
          ${rules.map(r => `<div class="rule-row"><strong>${r.loc}</strong><span>${esc(r.quote)}</span></div>`).join('')}
        </aside>
      </div>`;
  }

  /* ── step 2: primary agent verdicts with citations ───── */

  function step2(P) {
    const swarmRan = state.maxStep >= 3; // mismatches are only known after the citation verifier runs
    const count = v => P.filter(p => p.verdict === v).length;

    const rows = P.map(p => `
      <tr class="${p.id === state.selectedId ? 'is-selected' : ''}" data-action="select" data-id="${p.id}">
        <td><div class="pid">${p.id}</div><div class="small muted">${p.cohort}</div></td>
        <td>
          <div class="signals">${esc(p.signals)}</div>
          <div class="rationale">${esc(p.rationale)}</div>
          <div class="cites">${p.cites.map((c, i) => {
            const bad = !c.ok && swarmRan;
            const active = state.cite && state.cite.pid === p.id && state.cite.i === i;
            return `<button class="cite${bad ? ' is-mismatch' : ''}${active ? ' is-active' : ''}" data-action="open-cite" data-id="${p.id}" data-i="${i}">${c.doc} ${esc(c.loc)}${bad ? ' · mismatch' : ''}</button>`;
          }).join('')}</div>
        </td>
        <td><span class="${verdictTag(p.verdict)}">${p.verdict}</span></td>
      </tr>`).join('');

    let source = '<p class="muted">Open any citation in the table to see the line it points to.</p>';
    const cp = state.cite && P.find(p => p.id === state.cite.pid);
    const cc = cp && cp.cites[state.cite.i];
    if (cc) {
      const doc = docs.find(d => d.code === cc.doc);
      let status = 'Not yet verified — the citation verifier runs in step 3.', cls = '';
      if (swarmRan && cc.ok) { status = 'Citation verifier: the source line supports the agent’s claim.'; cls = 'ok'; }
      if (swarmRan && !cc.ok) { status = `Citation verifier: mismatch. The agent claimed ${esc(cc.claimed)}; this line does not support it.`; cls = 'bad'; }
      source = `<div class="source-doc">${esc(doc ? doc.name : cc.doc)}</div>
        <div class="source-loc">${cc.doc} ${esc(cc.loc)}</div>
        <div class="source-quote">“${esc(cc.quote)}”</div>
        <div class="source-status ${cls}">${status}</div>`;
    }

    return stepHead(2, 'Primary agent', 'Read the row-by-row verdicts',
      'The primary agent ruled on each participant against the charter’s stopping rules. Every number in a rationale carries a citation — open one to see the exact source line before anything is trusted.') + `
      <div class="summary">
        <span><strong>${count('PASS')}</strong> pass</span><span><strong>${count('REVIEW')}</strong> review</span><span><strong>${count('FAIL')}</strong> fail</span>
        <span class="muted">${P.reduce((s, p) => s + p.cites.length, 0)} citations</span>
        <span class="muted">weighted toward ${state.focus.size} marked sources</span>
      </div>
      <div class="split">
        <div class="split-main table-wrap">
          <table class="table verdicts">
            <thead><tr><th style="width:90px">Participant</th><th>Signals &amp; rationale</th><th style="width:80px">Verdict</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <aside class="split-aside sticky"><div class="kicker neutral">Source line</div>${source}</aside>
      </div>`;
  }

  /* ── step 3: swarm heatmap + reviewer arguments ──────── */

  function step3(P) {
    const sel = P.find(p => p.id === state.selectedId) || P[0];
    const header = `<div class="heat-row"><span></span>${P.map(p =>
      `<button class="heat-col${p.id === sel.id ? ' is-selected' : ''}" data-action="select" data-id="${p.id}">${p.id.slice(-2)}</button>`).join('')}</div>`;
    const rows = agents.map((a, i) => `<div class="heat-row"><span class="heat-name">${a.name}</span>${P.map(p => {
      const c = p.votes[i].conf;
      return `<button class="heat-cell${p.id === sel.id ? ' is-selected' : ''}" style="background:${heatColor(c)}" title="${a.name} on ${p.id}: ${f2(c)}" aria-label="${a.name} on ${p.id}: ${f2(c)}" data-action="select" data-id="${p.id}"></button>`;
    }).join('')}</div>`).join('');

    const reviewers = sel.byConf.map(v => `
      <div>
        <div><span class="reviewer-name">${v.agent.name}</span> <span class="small muted">${v.agent.focus}</span></div>
        <div class="reviewer-vote">
          <span class="${v.dissent ? 'tag tag-accent-2' : 'tag tag-neutral'}">${v.dissent ? 'Dissents' : 'Concurs'}</span>
          <div class="bar${v.dissent ? ' dissent' : ''}"><i style="width:${Math.round(v.conf * 100)}%"></i></div>
          <span class="small num">${f2(v.conf)}</span>
        </div>
        <div class="reviewer-note">${esc(v.note)}</div>
      </div>`).join('');

    return stepHead(3, 'Swarm', 'Ten reviewers try to break each verdict',
      'Each persona re-reads the source documents independently and scores its confidence that the primary verdict holds. Ink depth is confidence; magenta is dissent. Pick a participant to read the arguments.') + `
      <div class="heatmap-wrap"><div class="heatmap">${header}${rows}</div></div>
      <div class="heat-legend">Columns are participants P-1001 to P-1012.</div>
      <div class="selected-head">
        <h3>${sel.id}</h3><span class="muted">${sel.cohort} · primary ruled</span>
        <span class="${verdictTag(sel.verdict)}">${sel.verdict}</span>
        <span class="muted">· ${sel.dissent.length} of 10 dissent</span>
      </div>
      <div class="reviewers">${reviewers}</div>`;
  }

  /* ── step 4: consensus + threshold ───────────────────── */

  function step4(P) {
    const sel = P.find(p => p.id === state.selectedId) || P[0];
    const low = sel.byConf[0];
    const dots = sel.votes.map((v, i) =>
      `<span class="strip-dot${v.dissent ? ' dissent' : ''}" style="left:${pct(v.conf)};top:${4 + (i % 3) * 9}px" title="${v.agent.name} · ${f2(v.conf)}"></span>`).join('');

    // 12 bins of 0.05 from 0.40 to 1.00
    const bins = Array.from({ length: 12 }, () => []);
    P.forEach(p => bins[Math.max(0, Math.min(11, Math.floor((p.consensus - 0.4) / 0.05)))].push(p));
    const hist = bins.map(b => `<div class="hist-bin">${b.map(p =>
      `<button class="hist-item${p.routed ? ' is-routed' : ''}${p.id === sel.id ? ' is-selected' : ''}" title="${p.id} · ${f2(p.consensus)}" data-action="select" data-id="${p.id}">${p.id.slice(-2)}</button>`).join('')}</div>`).join('');

    const routedCount = P.filter(p => p.routed).length;
    const route = !sel.routed ? 'Auto-confirmed' : (sel.fault && sel.consensus >= state.threshold ? 'Routed · citation fault' : 'Routed to you');

    return stepHead(4, 'Consensus', 'Decide what needs a human',
      'Consensus is the swarm’s mean confidence in the primary verdict. Set the threshold: anything below it, and any verdict resting on a faulty citation, goes to your worklist.') + `
      <div class="split">
        <div class="consensus-left">
          <div class="muted">${sel.id} · ${sel.cohort} · agreement with <span class="${verdictTag(sel.verdict)}">${sel.verdict}</span></div>
          ${plateNumber(f2(sel.consensus))}
          <div class="strip"><div class="strip-axis"></div><div class="threshold-line" style="left:${pct(state.threshold)}"></div>${dots}</div>
          <div class="axis-labels"><span>0</span><span>0.5</span><span>1.0</span></div>
          <div class="stats"><span>${sel.dissent.length} of 10 dissent</span><span>spread σ ${f2(sel.sd)}</span><span class="${sel.routed ? 'tag tag-accent-2' : 'tag tag-accent'}">${route}</span></div>
          <p class="low-note">“${esc(low.note)}” — ${low.agent.name}</p>
        </div>
        <div class="consensus-right">
          <h4 style="margin:0">Batch distribution</h4>
          <div class="hist">${hist}<div class="threshold-line" style="left:${pct(Math.max(0, (state.threshold - 0.4) / 0.6))}"></div></div>
          <div class="axis-labels"><span>0.40</span><span>0.70</span><span>1.00</span></div>
          <div class="field" style="margin-top:15px">
            <label for="threshold">Consensus threshold · <span id="threshold-value">${f2(state.threshold)}</span></label>
            <input type="range" id="threshold" min="0.5" max="0.95" step="0.05" value="${state.threshold}">
          </div>
          <div><strong>${P.length - routedCount}</strong> auto-confirmed · <strong>${routedCount}</strong> routed to your worklist</div>
        </div>
      </div>`;
  }

  /* ── step 5: research associate worklist ─────────────── */

  function step5(P) {
    const SEV = ['Low severity', 'Data integrity', 'Moderate severity', 'High severity', 'Possible stopping-rule breach'];
    const list = worklist(P);
    const resolved = list.filter(p => state.decisions[p.id]).length;
    const left = list.length - resolved;
    const auto = P.filter(p => !p.routed);

    const items = list.map((p, k) => {
      const d = state.decisions[p.id];
      const overrideLabel = p.overrideLabel || 'Override verdict';
      const reason = p.reason || `Consensus below ${f2(state.threshold)} — ${p.byConf[0].agent.name}: ${p.byConf[0].note}`;
      const decisionLabel = { uphold: 'Agent verdict upheld', override: `${overrideLabel} · logged`, escalate: 'Escalated to DSMB' }[d];
      const actions = d
        ? `<span class="${d === 'escalate' ? 'tag tag-accent-2' : 'tag tag-accent'}">${esc(decisionLabel)}</span>
           <span class="small muted">JO · 26 Oct 2026</span>
           <button class="btn btn-ghost" data-action="undo" data-id="${p.id}">Undo</button>`
        : `<button class="btn btn-secondary" data-action="decide" data-decision="uphold" data-id="${p.id}">Uphold agent</button>
           <button class="btn btn-secondary" data-action="decide" data-decision="override" data-id="${p.id}">${esc(overrideLabel)}</button>
           <button class="btn btn-secondary" data-action="decide" data-decision="escalate" data-id="${p.id}">Escalate to DSMB</button>`;
      return `<div class="work-item">
        <span class="work-n">${String(k + 1).padStart(2, '0')}</span>
        <div>
          <div class="work-title">
            <span class="work-id">${p.id}</span><span class="small muted">${p.cohort}</span>
            <span class="${verdictTag(p.verdict)}">${p.verdict}</span>
            <span class="${(p.sev || 0) >= 3 ? 'sev-high' : 'sev-other'}">${SEV[p.sev || 0]}</span>
            <span class="small muted num">consensus ${f2(p.consensus)}</span>
          </div>
          <div class="work-reason">${esc(reason)}</div>
          <div class="work-dissent">Dissent: ${p.dissent.length ? p.dissent.map(v => v.agent.name).join(', ') : 'none — low-confidence concurrence'}</div>
          <div class="work-actions">${actions}</div>
        </div>
      </div>`;
    }).join('');

    const exportBlock = state.exported
      ? `<div>
          <div style="font-size:17px;font-weight:600">Packet signed and exported</div>
          <div class="small" style="margin-top:4px">CVX-214-101 · DSMB batch 1 · 12 verdicts, ${list.length} human decisions, 120 reviewer scores and the full citation trail.</div>
          <button class="btn btn-ghost" data-action="restart" style="margin-top:8px;margin-left:-5px">Start a new batch</button>
        </div>`
      : `<div>
          <button class="btn btn-primary btn-block" data-action="export" ${left > 0 ? 'disabled' : ''}>Sign &amp; export DSMB packet</button>
          <div class="small muted" style="margin-top:8px">${left > 0 ? `${left} item${left === 1 ? '' : 's'} still need a decision.` : 'All routed items resolved. Ready for sign-off.'}</div>
        </div>`;

    return stepHead(5, 'Worklist', 'Resolve what the swarm couldn’t',
      'Order by clinical severity or by how little the swarm agreed. Uphold the agent, override it, or escalate to the board. Every decision is logged with your initials for the DSMB audit trail.') + `
      <div class="split">
        <div class="split-main" style="gap:35px">
          <div class="seg" role="radiogroup" aria-label="Sort worklist" style="align-self:flex-start">
            <label class="seg-opt"><input type="radio" name="wl-sort" value="severity" ${state.sort === 'severity' ? 'checked' : ''}>Most severe first</label>
            <label class="seg-opt"><input type="radio" name="wl-sort" value="consensus" ${state.sort === 'consensus' ? 'checked' : ''}>Lowest consensus first</label>
          </div>
          ${items || '<p class="muted">Nothing routed at this threshold.</p>'}
        </div>
        <aside class="split-aside sticky aside-stack">
          <div><div class="kicker neutral">Resolved</div><div class="big-count">${resolved} / ${list.length}</div></div>
          <div><div class="kicker neutral">Auto-confirmed · consensus ≥ ${f2(state.threshold)}</div>
            <div class="tag-list">${auto.map(p => `<span class="tag tag-neutral">${p.id} · ${f2(p.consensus)}</span>`).join('')}</div></div>
          ${exportBlock}
        </aside>
      </div>`;
  }

  /* ── render + navigation ─────────────────────────────── */

  const STEPS = [step1, step2, step3, step4, step5];

  function render() {
    const P = analyse();
    app.innerHTML = stepNav() + `<section data-step="${state.step}">${STEPS[state.step - 1](P)}</section>` + footerNav(P);
  }

  function goTo(n) {
    state.step = n;
    state.maxStep = Math.max(state.maxStep, n);
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // "Next" simulates an agent run with a short delay, then advances
  function next() {
    if (state.busy || state.step >= 5) return;
    state.busy = true;
    render();
    setTimeout(() => { state.busy = false; goTo(state.step + 1); }, state.step === 4 ? 500 : 1100);
  }

  function addFiles(files) {
    Array.from(files || []).forEach(f => state.uploads.push({ name: f.name, size: Math.max(1, Math.round(f.size / 1024)) + ' KB' }));
    render();
  }

  /* ── events ──────────────────────────────────────────── */

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const { action, id } = el.dataset;
    switch (action) {
      case 'go': goTo(Number(el.dataset.step)); break;
      case 'next': next(); break;
      case 'back': if (state.step > 1) goTo(state.step - 1); break;
      case 'browse': fileInput.click(); break;
      case 'toggle-focus': {
        const k = el.dataset.key;
        state.focus.has(k) ? state.focus.delete(k) : state.focus.add(k);
        render(); break;
      }
      case 'select': state.selectedId = id; render(); break;
      case 'open-cite': state.selectedId = id; state.cite = { pid: id, i: Number(el.dataset.i) }; render(); break;
      case 'decide': state.decisions[id] = el.dataset.decision; render(); break;
      case 'undo': delete state.decisions[id]; render(); break;
      case 'export': state.exported = true; render(); break;
      case 'restart':
        Object.assign(state, { step: 1, maxStep: 1, decisions: {}, exported: false, selectedId: 'P-1007', cite: { pid: 'P-1007', i: 0 } });
        render(); window.scrollTo({ top: 0 }); break;
    }
  });

  // Threshold slider: live label while dragging, full re-render on release
  document.addEventListener('input', e => {
    if (e.target.id === 'threshold') document.getElementById('threshold-value').textContent = f2(parseFloat(e.target.value));
  });
  document.addEventListener('change', e => {
    if (e.target.id === 'threshold') { state.threshold = parseFloat(e.target.value); render(); }
    if (e.target.name === 'wl-sort') { state.sort = e.target.value; render(); }
  });

  fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

  // Drag and drop onto the step-1 drop zone
  document.addEventListener('dragover', e => {
    const zone = e.target.closest && e.target.closest('[data-dropzone]');
    if (zone) { e.preventDefault(); zone.classList.add('is-over'); }
  });
  document.addEventListener('dragleave', e => {
    const zone = e.target.closest && e.target.closest('[data-dropzone]');
    if (zone) zone.classList.remove('is-over');
  });
  document.addEventListener('drop', e => {
    const zone = e.target.closest && e.target.closest('[data-dropzone]');
    if (zone) { e.preventDefault(); addFiles(e.dataTransfer.files); }
  });

  render();
})();
