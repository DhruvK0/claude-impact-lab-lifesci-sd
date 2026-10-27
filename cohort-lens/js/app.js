/*
 * Cohort Lens — the guided five-step review.
 *
 *   1 Upload         mark which sources inside each document matter most
 *   2 Primary agent  row-by-row PASS / REVIEW / FAIL with citations
 *   3 Swarm          adversarial reviewer seats score each verdict
 *   4 Consensus      mean reviewer confidence; threshold decides routing
 *   5 Worklist       research associate resolves routed rows, exports packet
 *
 * Plain JavaScript, no build step. All state lives in `state`; every change
 * calls render(), which rebuilds #app from template strings. Clicks are handled
 * by one delegated listener that reads data-action attributes.
 *
 * Data comes from window.COHORT_LENS_DATA (js/loader.js): either the static
 * pipeline export in js/data.js or the live CohortLens API. With the API, step 1
 * starts a real pipeline run and worklist decisions are posted back to it.
 */
(function () {
  'use strict';

  let data = window.COHORT_LENS_DATA;
  let { agents, docs, patients, rules, defaultFocus } = data;
  let meta = data.meta || {};
  const API = window.COHORT_LENS_API;
  const STEP_LABELS = ['Upload', 'Primary agent', 'Swarm', 'Consensus', 'Worklist'];
  const DISSENT_BELOW = 0.5; // a reviewer "dissents" when its confidence is below this
  const ROUTING_LABEL = { R0: 'Continue', R1: 'Monitor', RQ: 'Data query', R2: 'Dose hold', R3: 'Discontinue', R4: 'Urgent review' };

  const firstSelected = () => meta.defaultSelected || (patients[0] && patients[0].id);
  const state = {
    step: 1,
    maxStep: 1,               // furthest step reached; later steps stay locked
    busy: false,              // true while an agent run is in progress
    busyLabel: '',            // live progress text from the pipeline event stream
    notice: window.COHORT_LENS_NOTICE || '',
    selectedId: firstSelected(),
    cite: { pid: firstSelected(), i: 0 },
    focus: new Set(defaultFocus),
    uploads: [],              // files added in step 1 (queued, not analysed)
    threshold: 0.8,
    routeCitationFaults: true,
    sort: 'severity',         // 'severity' | 'consensus'
    decisions: {},            // { [participantId]: 'uphold' | 'override' | 'escalate' }
    exported: false,
  };

  function setData(d) {
    data = d;
    ({ agents, docs, patients, rules, defaultFocus } = d);
    meta = d.meta || {};
    state.selectedId = firstSelected();
    state.cite = { pid: state.selectedId, i: 0 };
    state.decisions = {};
    state.exported = false;
  }

  const app = document.getElementById('app');
  const fileInput = document.getElementById('file-input');

  /* ── helpers ─────────────────────────────────────────── */

  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const f2 = n => n.toFixed(2);
  const pct = n => (n * 100).toFixed(1) + '%';
  const verdictTag = v => (v === 'PASS' ? 'tag tag-accent' : v === 'FAIL' ? 'tag tag-accent-2' : 'tag tag-neutral');
  const routingTag = r => (r === 'R0' ? 'tag tag-accent' : ['R2', 'R3', 'R4'].includes(r) ? 'tag tag-accent-2' : 'tag tag-neutral');
  const routingChip = r => (r ? `<span class="${routingTag(r)}" title="${esc(ROUTING_LABEL[r] || r)}">${r} · ${esc(ROUTING_LABEL[r] || '')}</span>` : '');
  const initials = name => String(name || 'RA').split(/[\s.]+/).filter(Boolean).map(w => w[0].toUpperCase()).join('').slice(0, 3);
  const today = () => new Date().toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  const short = id => id.slice(-2);
  const count = c => (c && c.denominator ? `${c.numerator}/${c.denominator}` : 'n/a');

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
      // The pipeline's own human-review flag (stopping rules, oracle disagreement, dissent) always routes
      const routed = !!p.humanReview || consensus < state.threshold || (state.routeCitationFaults && fault);
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
    const busy = [state.busyLabel || `Agent reading ${docs.length} documents…`, `${agents.length} reviewers challenging ${P.length} verdicts…`, 'Scoring consensus…', 'Building worklist…'];
    const routed = P.filter(p => p.routed).length;
    const hints = [
      `${docs.length} documents parsed · ${state.focus.size} sources marked for close attention.` + (API ? ' Runs the live pipeline.' : ''),
      `Next: ${agents.length} reviewer seats check every verdict and citation.`,
      'Next: aggregate the swarm into a consensus score per participant.',
      `${routed} participants will be routed to you at threshold ${f2(state.threshold)}.`,
    ];
    const i = state.step - 1;
    return `<div class="footer-nav">
      ${state.step > 1 ? '<button class="btn btn-secondary" data-action="back">Back</button>' : ''}
      <button class="btn btn-primary" data-action="next" ${state.busy ? 'disabled' : ''}>${esc(state.busy ? busy[i] : labels[i])}</button>
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
        <div class="doc-title"><span class="doc-name">${esc(u.name)}</span><span class="small muted">${esc(u.size)}</span><span class="${u.status === 'Ingested' ? 'tag tag-accent' : u.status === 'Rejected' ? 'tag tag-accent-2' : 'tag tag-neutral'}">${esc(u.status || 'Queued')}</span></div>
      </div>`).join('');

    const perturbations = (meta.perturbations || []).length
      ? `<div class="notice"><strong>Adversarial lane active.</strong> This batch carries ${meta.perturbations.length} declared perturbation${meta.perturbations.length === 1 ? '' : 's'} from the manifest (written before the run) to show what the checks catch:
          <ul>${meta.perturbations.map(x => `<li><span class="tag tag-neutral">${esc(x.lane)}</span> ${esc(x.participant || '')} — ${esc(x.description)} <span class="small muted">expected: ${esc(x.expected)}</span></li>`).join('')}</ul></div>`
      : '';

    return stepHead(1, 'Upload', 'Assemble the document batch',
      'Bring in the protocol, the DSMB stopping rules and the source exports for this data cut. Every file is split into addressable records, so any claim can be traced to a single line. Then mark the sources inside each document that deserve the closest attention — agents and reviewers weigh those first.') + `
      ${state.notice ? `<div class="notice">${esc(state.notice)}</div>` : ''}
      ${perturbations}
      <div class="split">
        <div class="split-main">
          <div class="dropzone" data-dropzone>
            <div><div class="dropzone-title">Drop files here</div><div class="small muted">CohortLens source CSVs (see <code>synthetic data/</code>) · ingested into the approved-source registry</div></div>
            <button class="btn btn-secondary" data-action="browse">Browse files</button>
          </div>
          <div>
            <div class="section-head"><h4>Mark what matters</h4><span class="muted"><strong>${state.focus.size}</strong> sources marked for close attention</span></div>
            ${docRows}${uploadRows}
          </div>
        </div>
        <aside class="split-aside">
          <h4>Stopping rules the agent will apply</h4>
          <p class="small muted">Protocol ${esc(meta.study || '')} v${esc(meta.protocolVersion || '')}, §6 — rules that hold, discontinue or escalate dosing.</p>
          ${rules.map(r => `<div class="rule-row"><strong>${r.loc}</strong><span>${esc(r.quote)}</span></div>`).join('')}
        </aside>
      </div>`;
  }

  /* ── step 2: primary agent verdicts with citations ───── */

  function step2(P) {
    const swarmRan = state.maxStep >= 3; // mismatches are only known after the citation verifier runs
    const countV = v => P.filter(p => p.verdict === v).length;

    const rows = P.map(p => `
      <tr class="${p.id === state.selectedId ? 'is-selected' : ''}" data-action="select" data-id="${p.id}">
        <td><div class="pid">${p.id}</div><div class="small muted">${p.cohort}</div></td>
        <td>
          <div class="signals">${esc(p.signals)}</div>
          <div class="rationale">${esc(p.rationale)}</div>
          ${p.adversarial && p.adversarial.length ? `<div class="small adversarial-note">Adversarial lane: ${p.adversarial.map(x => `${esc(x.id)} ${esc(x.description)}`).join(' · ')}</div>` : ''}
          <div class="cites">${p.cites.map((c, i) => {
            const bad = !c.ok && swarmRan;
            const active = state.cite && state.cite.pid === p.id && state.cite.i === i;
            return `<button class="cite${bad ? ' is-mismatch' : ''}${active ? ' is-active' : ''}" data-action="open-cite" data-id="${p.id}" data-i="${i}">${c.doc} ${esc(c.loc)}${bad ? ' · mismatch' : ''}</button>`;
          }).join('')}</div>
        </td>
        <td><span class="${verdictTag(p.verdict)}">${p.verdict}</span>${p.routing ? `<div class="small muted" style="margin-top:4px">${p.routing.initial}</div>` : ''}</td>
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
        ${cc.claim ? `<div class="small muted" style="margin-top:10px">Claim: ${esc(cc.claim)}</div>` : ''}
        <div class="source-status ${cls}">${status}</div>`;
    }

    return stepHead(2, 'Primary agent', 'Read the row-by-row verdicts',
      'The DSMB agent ruled on each participant against the protocol’s stopping rules (PASS = R0 continue, REVIEW = R1 monitor or RQ data query, FAIL = R2–R4 hold, discontinue or urgent review). Every number carries a citation — open one to see the exact source line before anything is trusted.') + `
      <div class="summary">
        <span><strong>${countV('PASS')}</strong> pass</span><span><strong>${countV('REVIEW')}</strong> review</span><span><strong>${countV('FAIL')}</strong> fail</span>
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
    const cols = `grid-template-columns: 170px repeat(${P.length}, minmax(14px, 1fr))`;
    const header = `<div class="heat-row" style="${cols}"><span></span>${P.map(p =>
      `<button class="heat-col${p.id === sel.id ? ' is-selected' : ''}" data-action="select" data-id="${p.id}" title="${p.id}">${short(p.id)}</button>`).join('')}</div>`;
    const rows = agents.map((a, i) => `<div class="heat-row" style="${cols}"><span class="heat-name">${a.name}</span>${P.map(p => {
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

    const planted = (sel.plantedCritiques || []).map(c => `
      <div class="notice"><strong>Adversarial lane · planted “Reviewer 2” critique (${esc(c.stage)})</strong><br>“${esc(c.assertion)}”<br>
        <span class="small">Author response: <strong>${esc(c.response)}</strong> — ${esc(c.rationale)}</span></div>`).join('');

    return stepHead(3, 'Swarm', `${agents.length} reviewers try to break each verdict`,
      'Each expert seat re-reads its slice of the evidence independently; the citation verifier and protocol oracle are deterministic code. Ink depth is confidence; magenta is dissent. Pick a participant to read the arguments.') + `
      <div class="heatmap-wrap"><div class="heatmap" style="min-width:${170 + P.length * 18}px">${header}${rows}</div></div>
      <div class="heat-legend">Columns are participants ${P[0].id} to ${P[P.length - 1].id}. ${esc(meta.confidenceNote || '')}</div>
      <div class="selected-head">
        <h3>${sel.id}</h3><span class="muted">${sel.cohort} · primary ruled</span>
        <span class="${verdictTag(sel.verdict)}">${sel.verdict}</span>
        ${sel.routing ? routingChip(sel.routing.initial) : ''}
        <span class="muted">· ${sel.dissent.length} of ${agents.length} dissent</span>
      </div>
      ${planted}
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
    const tallest = Math.max(1, ...bins.map(b => b.length));
    const itemH = Math.max(6, Math.min(30, Math.floor(220 / tallest)));
    const hist = bins.map(b => `<div class="hist-bin">${b.map(p =>
      `<button class="hist-item${p.routed ? ' is-routed' : ''}${p.id === sel.id ? ' is-selected' : ''}" style="height:${itemH}px${itemH < 16 ? ';font-size:0' : ''}" title="${p.id} · ${f2(p.consensus)}" data-action="select" data-id="${p.id}">${short(p.id)}</button>`).join('')}</div>`).join('');

    const routedCount = P.filter(p => p.routed).length;
    const route = !sel.routed ? 'Auto-confirmed'
      : sel.humanReview ? 'Routed · pipeline flag'
      : (sel.fault && sel.consensus >= state.threshold ? 'Routed · citation fault' : 'Routed to you');

    return stepHead(4, 'Consensus', 'Decide what needs a human',
      'Consensus is the swarm’s mean confidence in the primary verdict. Set the threshold: anything below it, any verdict resting on a faulty citation, and anything the pipeline itself flagged (stopping rule met, oracle disagreement, unresolved dissent) goes to your worklist.') + `
      <div class="split">
        <div class="consensus-left">
          <div class="muted">${sel.id} · ${sel.cohort} · agreement with <span class="${verdictTag(sel.verdict)}">${sel.verdict}</span></div>
          ${plateNumber(f2(sel.consensus))}
          <div class="strip"><div class="strip-axis"></div><div class="threshold-line" style="left:${pct(state.threshold)}"></div>${dots}</div>
          <div class="axis-labels"><span>0</span><span>0.5</span><span>1.0</span></div>
          <div class="stats"><span>${sel.dissent.length} of ${agents.length} dissent</span><span>spread σ ${f2(sel.sd)}</span><span class="${sel.routed ? 'tag tag-accent-2' : 'tag tag-accent'}">${route}</span></div>
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

  function assurance() {
    const ev = meta.evaluation;
    const cohorts = (data.cohorts || []).map(c => `<div class="rule-row"><strong>Cohort ${c.cohort} · ${esc(c.dose)}</strong>
      <span><span class="${c.decision === 'ESCALATE' ? 'tag tag-accent' : 'tag tag-accent-2'}">${esc(c.decision.replace('_', ' '))}</span>
      ${c.agent && c.agent !== c.oracle ? ` <span class="small">agent ${esc(c.agent)} vs oracle ${esc(c.oracle)}</span>` : ''}
      ${(c.efficacy || []).map(e => e.mean == null ? '' : `<span class="small muted"> · ${esc(e.label)} ${e.mean > 0 ? '+' : ''}${e.mean.toFixed(1)} ${esc(e.unit)} (n=${e.n})</span>`).join('')}</span></div>`).join('');
    if (!ev) return cohorts ? `<div><div class="kicker neutral">Cohort escalation (S5)</div>${cohorts}</div>` : '';
    const adv = ev.modes.adversarial || {}, base = ev.modes.single_agent || {};
    const row = (label, k) => `<tr><td>${label}</td><td class="num">${count(adv[k])}</td><td class="num">${count(base[k])}</td></tr>`;
    const gates = ev.gates.map(g => `<span class="${g.passed === null ? 'tag tag-neutral' : g.passed ? 'tag tag-accent' : 'tag tag-accent-2'}" title="${esc(g.description)} ${esc(g.note || '')}">${g.gate_id} ${g.passed === null ? 'n/a' : g.passed ? 'pass' : 'fail'}</span>`).join(' ');
    const claims = ev.claim_eval.map(c => `<tr><td>${esc(c.verifier.replace(/_/g, ' '))}</td><td class="num">${count(c.false_approvals)}</td><td class="num">${count(c.detection_recall)}</td></tr>`).join('');
    return `<div><div class="kicker neutral">Cohort escalation (S5)</div>${cohorts}</div>
      <div><div class="kicker neutral">Assurance evidence · ${esc(ev.evaluation_id)}</div>
        <div class="tag-list" style="margin:8px 0">${gates}</div>
        <table class="table small"><thead><tr><th>Measured on identical inputs</th><th>Peer review</th><th>Single agent</th></tr></thead><tbody>
          ${row('Routing = oracle (clean)', 'routing_accuracy')}${row('Pollution detected', 'pollution_detection_rate')}
          ${row('MUST-DETECT passed', 'must_detect_passed')}${row('Planted critiques accepted', 'capitulation_rate')}
        </tbody></table>
        <table class="table small" style="margin-top:10px"><thead><tr><th>Gold claims</th><th>False approvals</th><th>Hallucinations caught</th></tr></thead><tbody>${claims}</tbody></table>
        <div class="small muted" style="margin-top:6px">${esc((ev.notes || []).join(' '))}</div></div>`;
  }

  function step5(P) {
    const SEV = ['Low severity', 'Data integrity', 'Moderate severity', 'High severity', 'Possible stopping-rule breach'];
    const list = worklist(P);
    const resolved = list.filter(p => state.decisions[p.id]).length;
    const left = list.length - resolved;
    const auto = P.filter(p => !p.routed);
    const who = initials(meta.researchAssociate);

    const items = list.map((p, k) => {
      const d = state.decisions[p.id];
      const overrideLabel = p.overrideLabel || 'Override verdict';
      const reason = p.reason || `Consensus below ${f2(state.threshold)} — ${p.byConf[0].agent.name}: ${p.byConf[0].note}`;
      const decisionLabel = { uphold: 'Agent verdict upheld', override: `${overrideLabel} · logged`, escalate: 'Escalated to DSMB' }[d];
      const actions = d
        ? `<span class="${d === 'escalate' ? 'tag tag-accent-2' : 'tag tag-accent'}">${esc(decisionLabel)}</span>
           <span class="small muted">${esc(who)} · ${today()}</span>
           <button class="btn btn-ghost" data-action="undo" data-id="${p.id}">Undo</button>`
        : `<button class="btn btn-secondary" data-action="decide" data-decision="uphold" data-id="${p.id}">Uphold agent</button>
           <button class="btn btn-secondary" data-action="decide" data-decision="override" data-id="${p.id}">${esc(overrideLabel)}</button>
           <button class="btn btn-secondary" data-action="decide" data-decision="escalate" data-id="${p.id}">Escalate to DSMB</button>`;
      const routing = p.routing ? `<div class="small">Primary ${routingChip(p.routing.initial)} → final ${routingChip(p.routing.final)}${p.routing.oracle ? ` <span class="muted">· oracle ${p.routing.oracle}</span>` : ''}</div>` : '';
      return `<div class="work-item">
        <span class="work-n">${String(k + 1).padStart(2, '0')}</span>
        <div>
          <div class="work-title">
            <span class="work-id">${p.id}</span><span class="small muted">${p.cohort}</span>
            <span class="${verdictTag(p.verdict)}">${p.verdict}</span>
            <span class="${(p.sev || 0) >= 3 ? 'sev-high' : 'sev-other'}">${SEV[p.sev || 0]}</span>
            <span class="small muted num">consensus ${f2(p.consensus)}</span>
          </div>
          ${routing}
          <div class="work-reason">${esc(reason)}</div>
          ${(p.dataQueries || []).length ? `<div class="small muted">Data queries: ${p.dataQueries.map(q => esc(`${q.record || ''} ${q.field || ''}`.trim() || q.issue)).join('; ')}</div>` : ''}
          <div class="work-dissent">Dissent: ${p.dissent.length ? p.dissent.map(v => v.agent.name).join(', ') : 'none — low-confidence concurrence'}</div>
          <div class="work-actions">${actions}</div>
        </div>
      </div>`;
    }).join('');

    const exportBlock = state.exported
      ? `<div>
          <div style="font-size:17px;font-weight:600">Packet signed and exported</div>
          <div class="small" style="margin-top:4px">${esc(meta.study || '')} · run ${esc(meta.runId || '')} · ${P.length} verdicts, ${list.length} human decisions, ${P.length * agents.length} reviewer scores and the full citation trail.</div>
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
          ${exportBlock}
          <div><div class="kicker neutral">Auto-confirmed · consensus ≥ ${f2(state.threshold)}</div>
            <div class="tag-list">${auto.map(p => `<span class="tag tag-neutral">${p.id} · ${f2(p.consensus)}</span>`).join('')}</div></div>
          ${assurance()}
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

  // Step 1 with the live API runs the real pipeline; other steps reveal results already computed
  function next() {
    if (state.busy || state.step >= 5) return;
    state.busy = true;
    render();
    if (state.step === 1 && API) { liveRun(); return; }
    setTimeout(() => { state.busy = false; goTo(state.step + 1); }, state.step === 4 ? 500 : 1100);
  }

  function liveRun() {
    const body = { mode: 'adversarial', perturbation_ids: (meta.perturbations || []).map(x => x.id), label: 'cohort-lens ui' };
    fetch(`${API}/api/runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(({ run_id }) => new Promise((resolve, reject) => {
        const es = new EventSource(`${API}/api/runs/${run_id}/events`);
        let routed = 0;
        es.onmessage = () => {};
        es.addEventListener('stage_started', e => { const d = JSON.parse(e.data); state.busyLabel = `${d.stage} ${d.subject || ''} · ${routed} routed…`; render(); });
        es.addEventListener('participant_routed', () => { routed += 1; });
        es.addEventListener('run_failed', e => { es.close(); reject(new Error(JSON.parse(e.data).data.error)); });
        es.addEventListener('end', () => { es.close(); resolve(run_id); });
        es.onerror = () => { es.close(); resolve(run_id); };
      }))
      .then(run_id => fetch(`${API}/api/runs/${run_id}/frontend?research_associate=${encodeURIComponent(meta.researchAssociate || '')}`))
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(d => { setData(d); state.notice = ''; state.busy = false; state.busyLabel = ''; goTo(2); })
      .catch(err => { state.busy = false; state.busyLabel = ''; state.notice = `Live run failed: ${err.message}`; render(); });
  }

  function postDecision(id, decision) {
    if (!API || !meta.runId) return;
    const p = patients.find(x => x.id === id);
    fetch(`${API}/api/review-queue/${encodeURIComponent(`${meta.runId}:${id}`)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reviewer: meta.researchAssociate || 'research-associate',
        override_routing: decision === 'override' && p && p.routing ? p.routing.oracle : null }),
    }).catch(() => { state.notice = 'Decision recorded locally; the API did not accept it.'; });
  }

  function exportPacket() {
    const P = analyse();
    const packet = {
      study: meta.study, protocolVersion: meta.protocolVersion, runId: meta.runId, backend: meta.backend,
      versions: meta.versions, signedBy: meta.researchAssociate, signedAt: new Date().toISOString(),
      threshold: state.threshold, markedSources: [...state.focus], cohorts: data.cohorts || [],
      participants: P.map(p => ({ id: p.id, cohort: p.cohort, verdict: p.verdict, routing: p.routing, routed: p.routed,
        consensus: +p.consensus.toFixed(3), decision: state.decisions[p.id] || null, reason: p.reason, cites: p.cites,
        reviewers: p.votes.map(v => ({ seat: v.agent.key, confidence: v.conf, note: v.note })), lockHash: p.lockHash })),
      perturbations: meta.perturbations || [], evaluation: meta.evaluation ? { id: meta.evaluation.evaluation_id, gates: meta.evaluation.gates } : null,
      synthetic: true,
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(packet, null, 1)], { type: 'application/json' }));
    const a = Object.assign(document.createElement('a'), { href: url, download: `dsmb-packet-${meta.runId || 'batch'}.json` });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // With the API, uploaded CohortLens CSVs are ingested and become the source of truth for the next run
  function addFiles(files) {
    const list = Array.from(files || []);
    const added = list.map(f => ({ name: f.name, size: Math.max(1, Math.round(f.size / 1024)) + ' KB', status: API ? 'Reading' : 'Queued' }));
    state.uploads.push(...added);
    if (!API) state.notice = 'Uploads are ingested by the CohortLens API: run `python -m cohortlens serve` and open http://localhost:8000.';
    render();
    if (!API || !list.length) return;
    const mark = s => { added.forEach(u => { u.status = s; }); };
    Promise.all(list.map(f => f.text().then(text => ({ name: f.name, text }))))
      .then(payload => fetch(`${API}/api/datasets/upload`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: payload }) }))
      .then(r => r.json().then(j => (r.ok ? j : Promise.reject(j.detail || j))))
      .then(j => {
        mark('Ingested');
        state.notice = `Dataset ${j.dataset_id} is now the source of truth: ${j.n_records} records, ${j.n_participants} participants` +
          (j.matches_reference ? ' (hash matches the reference synthetic dataset).' : ' (new dataset; no planted ground truth, so no perturbations are applied).') +
          (j.errors.length ? ` ${j.errors.length} file issue(s): ${j.errors.slice(0, 3).join('; ')}` : '') + ' Run the primary agent to review it.';
        render();
      })
      .catch(e => { mark('Rejected'); state.notice = `Upload rejected: ${((e && e.errors) || [e && e.message ? e.message : String(e)]).join('; ')}`; render(); });
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
      case 'decide': state.decisions[id] = el.dataset.decision; postDecision(id, el.dataset.decision); render(); break;
      case 'undo': delete state.decisions[id]; render(); break;
      case 'export': exportPacket(); state.exported = true; render(); break;
      case 'restart':
        Object.assign(state, { step: 1, maxStep: 1, decisions: {}, exported: false, selectedId: firstSelected(), cite: { pid: firstSelected(), i: 0 } });
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
