/*
 * Cohort Lens — hardcoded SYNTHETIC data.
 *
 * Nothing here is real patient data. Replace this file (or load JSON from an API)
 * to wire the UI to real agent output. app.js only reads window.COHORT_LENS_DATA.
 *
 * Shapes
 *   agents[]   – the 10 adversarial reviewers in the swarm
 *   docs[]     – the uploaded document batch; `sections` are the sources inside
 *                each document the research associate can mark as important
 *   patients[] – one row per participant with the primary agent's verdict
 *       cites[]  – { doc, loc, quote, ok, claimed? }  ok:false = hallucinated citation
 *       base     – baseline reviewer confidence used for reviewers without an override
 *       ov       – per-reviewer overrides: { agentKey: [confidence 0..1, note] }
 *       reason   – one-line worklist reason (only for rows likely to be routed)
 *       sev      – severity rank for "most severe first" sorting (0–4)
 *   rules[]    – DSMB charter §6 stopping rules
 */
(function () {
  'use strict';

  const cite = (doc, loc, quote, extra) => Object.assign({ doc, loc, quote, ok: true }, extra || {});

  const R62 = cite('CHTR', '§6.2', 'Stop dosing if SBP < 90 mmHg with symptoms, or SBP falls > 30 mmHg from baseline.');
  const R63 = cite('CHTR', '§6.3', 'Stop dosing if eGFR declines > 25% from baseline.');
  const R64 = cite('CHTR', '§6.4', 'Stop dosing if ALT or AST > 3× ULN, confirmed on repeat within 72 h.');
  const R65 = cite('CHTR', '§6.5', 'Stop dosing if serum K⁺ > 5.5 mmol/L, confirmed on repeat.');
  const R66 = cite('CHTR', '§6.6', 'Stop dosing if QTcF > 500 ms or ΔQTcF > 60 ms from baseline.');

  const agents = [
    { key: 'cardio', name: 'Cardiologist',        focus: 'Hemodynamics, orthostasis',  ok: 'Hemodynamics within charter limits.' },
    { key: 'lipid',  name: 'Lipidologist',        focus: 'Triglycerides, lipid panel', ok: 'Lipid response consistent with dose.' },
    { key: 'hep',    name: 'Hepatologist',        focus: 'ALT, AST, bilirubin',        ok: 'No hepatic signal.' },
    { key: 'neph',   name: 'Nephrologist',        focus: 'eGFR, potassium',            ok: 'Renal function and electrolytes stable.' },
    { key: 'ep',     name: 'Electrophysiologist', focus: 'QTcF, rhythm',               ok: 'QTcF stable across triplicate reads.' },
    { key: 'pv',     name: 'Pharmacovigilance',   focus: 'AE coding, seriousness',     ok: 'No unreviewed adverse events.' },
    { key: 'stat',   name: 'Biostatistician',     focus: 'Baseline, variability',      ok: 'Change within expected variability.' },
    { key: 'audit',  name: 'Protocol auditor',    focus: 'Charter rule mapping',       ok: 'Verdict maps correctly to charter §6.' },
    { key: 'cite',   name: 'Citation verifier',   focus: 'Every cite against source',  ok: '' },
    { key: 'red',    name: 'Red-team skeptic',    focus: 'Tries to break the verdict', ok: 'Could not construct a counter-case.' },
  ];

  const docs = [
    { code: 'PROT', name: 'Protocol CVX-214-101 v3.1',    meta: 'PDF · 84 pp · 412 clauses',       sections: ['§4 Eligibility', '§5 Dose escalation', '§7.3 Prohibited medications'] },
    { code: 'CHTR', name: 'DSMB charter v2.0',            meta: 'PDF · 18 pp · stopping rules §6', sections: ['§6.2 Blood pressure', '§6.3 Renal', '§6.4 Hepatic', '§6.5 Potassium', '§6.6 QTc'] },
    { code: 'VIT',  name: 'vitals_wk0-4.csv',             meta: '1,152 rows',                      sections: ['Seated SBP / DBP', 'Standing SBP (orthostatic)', 'Heart rate'] },
    { code: 'LAB',  name: 'labs_lipid_hepatic_renal.csv', meta: '896 rows',                        sections: ['Triglycerides', 'ALT / AST', 'eGFR / creatinine', 'Potassium'] },
    { code: 'ECG',  name: 'ecg_qtcf.csv',                 meta: '384 rows',                        sections: ['QTcF triplicate', 'Rhythm reads'] },
    { code: 'AE',   name: 'ae_log.xlsx',                  meta: '41 events',                       sections: ['Grade ≥ 2 events', 'Related events', 'Serious AEs'] },
    { code: 'CM',   name: 'concomitant_meds.csv',         meta: '210 rows',                        sections: ['ACE inhibitors / ARBs', 'Statins & fibrates'] },
  ];

  // Sources pre-marked as important, as "DOC:sectionIndex"
  const defaultFocus = ['CHTR:0', 'CHTR:1', 'CHTR:2', 'CHTR:3', 'CHTR:4', 'VIT:1', 'LAB:1', 'LAB:3', 'ECG:0', 'AE:0', 'CM:0'];

  const patients = [
    { id: 'P-1001', cohort: 'Placebo', verdict: 'PASS', base: 0.94,
      signals: 'SBP 148→145 · TG −4% · ALT 0.7× ULN',
      rationale: 'No §6 threshold crossed; BP and TG changes within placebo variability.',
      cites: [cite('VIT', 'r0114', 'P-1001 · wk4 · seated SBP 145 mmHg (baseline 148)'), cite('LAB', 'r0041', 'P-1001 · wk4 · TG 204 mg/dL (baseline 212) · ALT 24 U/L')] },

    { id: 'P-1002', cohort: '5 mg', verdict: 'PASS', base: 0.92,
      signals: 'SBP 152→143 · TG −12%',
      rationale: 'Modest BP and TG reduction; no safety threshold approached.',
      cites: [cite('VIT', 'r0168', 'P-1002 · wk4 · seated SBP 143 mmHg (baseline 152)'), cite('LAB', 'r0077', 'P-1002 · wk4 · TG 211 mg/dL (baseline 240)')] },

    { id: 'P-1003', cohort: '10 mg', verdict: 'PASS', base: 0.9,
      signals: 'SBP 156→141 · TG −23% · ALT 1.4× ULN',
      rationale: 'ALT below the 3× ULN limit; efficacy in line with dose.',
      cites: [cite('LAB', 'r0152', 'P-1003 · wk4 · ALT 49 U/L (ULN 35) · 1.4× ULN'), R64],
      ov: { hep: [0.74, 'ALT rising wk2→wk4 (1.1→1.4× ULN). Concur; recommend recheck at wk6.'] } },

    { id: 'P-1004', cohort: '40 mg', verdict: 'FAIL', base: 0.95,
      signals: 'SBP 161→126 · standing nadir 86 · dizziness G2',
      rationale: 'SBP fell 35 mmHg with a symptomatic standing nadir of 86 mmHg — meets the §6.2 stopping rule.',
      cites: [cite('VIT', 'r0871', 'P-1004 · day 9 · standing SBP 86 mmHg · symptomatic'), cite('AE', '#17', 'AE 17 · P-1004 · dizziness, grade 2, onset day 9, related'), R62] },

    { id: 'P-1005', cohort: '40 mg', verdict: 'FAIL', base: 0.93,
      signals: 'ALT 3.4× ULN wk3 · TG −38%',
      rationale: 'ALT 3.4× ULN exceeds the §6.4 hepatic limit.',
      cites: [cite('LAB', 'r0612', 'P-1005 · wk3 · ALT 119 U/L (ULN 35) · 3.4× ULN'), R64],
      ov: { stat: [0.68, 'Single draw; §6.4 needs a 72 h repeat. LAB r0640 confirms 3.1×. Concur.'] } },

    { id: 'P-1006', cohort: '20 mg', verdict: 'PASS', base: 0.78, sev: 4,
      signals: 'SBP 150→131 · TG −31% · K⁺ 5.7',
      rationale: 'BP and TG reductions within expected range; no charter threshold crossed.',
      cites: [cite('VIT', 'r0502', 'P-1006 · wk4 · seated SBP 131 mmHg (baseline 150)'), cite('LAB', 'r0498', 'P-1006 · wk4 · TG 178 mg/dL (baseline 258)')],
      ov: {
        neph:  [0.08, 'K⁺ 5.7 mmol/L at wk4 (LAB r0533) exceeds §6.5. The primary never evaluated potassium.'],
        pv:    [0.3,  'No hyperkalemia AE logged, yet the lab value meets CTCAE grade 2.'],
        audit: [0.2,  '§6.5 requires a repeat K⁺ — the verdict should be REVIEW, not PASS.'],
        red:   [0.1,  'Concomitant lisinopril (CM r0088) plus K⁺ 5.7: PASS is indefensible.'],
      },
      reason: 'Unevaluated K⁺ 5.7 mmol/L — possible §6.5 breach', overrideLabel: 'Override → REVIEW' },

    { id: 'P-1007', cohort: '10 mg', verdict: 'PASS', base: 0.82, sev: 1,
      signals: 'SBP 154→142 · TG −22%',
      rationale: 'Strong TG response (−22%) with moderate BP reduction; no safety concerns.',
      cites: [cite('LAB', 'r0211', 'P-1007 · wk4 · TG 212 mg/dL (baseline 231) · Δ −8.2%', { ok: false, claimed: 'TG −22%' }), cite('VIT', 'r0640', 'P-1007 · wk4 · seated SBP 142 mmHg (baseline 154)')],
      ov: {
        cite:  [0.05, 'LAB r0211 reads −8.2%, not −22%. The figure appears nowhere in the batch.'],
        lipid: [0.38, '−8.2% is a non-response at 10 mg; the rationale overstates efficacy.'],
        red:   [0.2,  'One fabricated number means the whole rationale must be re-derived.'],
      },
      reason: 'Citation fault — agent claimed TG −22%; source reads −8.2%', overrideLabel: 'Strike & re-run' },

    { id: 'P-1008', cohort: '40 mg', verdict: 'REVIEW', base: 0.74, sev: 3,
      signals: 'QTcF 412→460 ms · Δ +48',
      rationale: 'ΔQTcF +48 ms is under the 60 ms limit but warrants cardiology review.',
      cites: [cite('ECG', 'r0301', 'P-1008 · wk2 · QTcF triplicate mean 460 ms (baseline 412)'), R66],
      ov: {
        ep:     [0.58, 'Triplicate reads are consistent; REVIEW is right but borderline.'],
        cardio: [0.62, 'Concur with REVIEW; request a Holter.'],
        red:    [0.28, 'The 40 mg cohort shows a dose-dependent QTc trend — this should be FAIL.'],
        stat:   [0.5,  'Two of three 40 mg participants exceed Δ +40 ms; unlikely to be chance.'],
        audit:  [0.55, '§6.6 not breached; REVIEW is the correct charter mapping.'],
      },
      reason: 'Swarm split on REVIEW vs FAIL — dose-dependent QTc trend at 40 mg', overrideLabel: 'Override → FAIL' },

    { id: 'P-1009', cohort: 'Placebo', verdict: 'PASS', base: 0.95,
      signals: 'SBP 146→147 · TG +2%',
      rationale: 'No change from baseline; no safety signal.',
      cites: [cite('VIT', 'r0988', 'P-1009 · wk4 · seated SBP 147 mmHg (baseline 146)'), cite('LAB', 'r0702', 'P-1009 · wk4 · TG 227 mg/dL (baseline 222)')] },

    { id: 'P-1010', cohort: '20 mg', verdict: 'PASS', base: 0.88,
      signals: 'SBP 149→134 · eGFR −18%',
      rationale: 'eGFR decline of 18% is below the 25% limit.',
      cites: [cite('LAB', 'r0745', 'P-1010 · wk4 · eGFR 72 mL/min (baseline 88)'), R63],
      ov: { neph: [0.62, 'Below §6.3, but the slope projects −25% by wk8. Concur; monitor.'] } },

    { id: 'P-1011', cohort: '40 mg', verdict: 'PASS', base: 0.82, sev: 2,
      signals: 'SBP 158→130 · TG −41%',
      rationale: 'SBP fall of 28 mmHg is under the 30 mmHg limit; asymptomatic.',
      cites: [cite('VIT', 'r0902', 'P-1011 · wk4 · seated SBP 130 mmHg (baseline 158) · asymptomatic'), R62],
      ov: {
        cardio: [0.42, 'Within 2 mmHg of §6.2, and wk3 standing vitals are blank (VIT r0915).'],
        red:    [0.3,  'Missing orthostatic data cannot support a PASS.'],
        audit:  [0.52, 'Technically compliant; the VIT gap should be queried with the site.'],
        stat:   [0.6,  'Intra-visit SD is ±6 mmHg — the true fall may exceed 30.'],
      },
      reason: 'Missing wk3 orthostatic vitals; SBP fall 2 mmHg from the limit', overrideLabel: 'Override → REVIEW' },

    { id: 'P-1012', cohort: '5 mg', verdict: 'PASS', base: 0.93,
      signals: 'SBP 151→144 · TG −10%',
      rationale: 'Mild response; no safety threshold approached.',
      cites: [cite('VIT', 'r1040', 'P-1012 · wk4 · seated SBP 144 mmHg (baseline 151)'), cite('LAB', 'r0811', 'P-1012 · wk4 · TG 219 mg/dL (baseline 243)')] },
  ];

  window.COHORT_LENS_DATA = { agents, docs, patients, defaultFocus, rules: [R62, R63, R64, R65, R66] };
})();
