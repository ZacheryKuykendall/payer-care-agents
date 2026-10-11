/* Demo console. No framework and no build step on purpose: this repository is a
   Python reference architecture, and a reviewer should be able to read every line
   of the front end in one sitting. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { system: null, scenarios: [] };

const pct = (n) => `${(n * 100).toFixed(1)}%`;
const fixed = (n, d = 2) => Number(n).toFixed(d);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------------ tabs */

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.toggle('is-active', t === tab));
    $$('.panel').forEach((p) =>
      p.classList.toggle('is-active', p.id === `tab-${tab.dataset.tab}`)
    );
    if (tab.dataset.tab === 'audit') loadAudit();
  });
});

/* ----------------------------------------------------------------- boot */

async function boot() {
  state.system = await api('/api/system');
  const { mode, decision_client: client, indexes } = state.system;
  const chunks = Object.values(indexes).reduce((a, b) => a + b, 0);
  const pill = $('#mode-pill');
  pill.textContent = `${mode} mode · ${client} · ${chunks} chunks indexed`;
  pill.classList.add(mode === 'mock' ? 'pill' : 'pill-good');

  const { scenarios } = await api('/api/scenarios');
  state.scenarios = scenarios;
  renderScenarios();
  renderTools();
  renderEgressPolicy();
  openDeepLink();
}

/* Deep links so a walkthrough can jump straight to a turn:
   /?scenario=injection   /?ask=does%20my%20plan%20cover%20hearing%20aids   /?tab=evals */
function openDeepLink() {
  const params = new URLSearchParams(window.location.search);

  const tab = params.get('tab');
  if (tab) $$('.tab').find((t) => t.dataset.tab === tab)?.click();

  const key = params.get('scenario');
  if (key) {
    const index = state.scenarios.findIndex((s) => s.key === key);
    if (index >= 0) $$('#scenario-list button')[index].click();
    return;
  }

  const ask = params.get('ask');
  if (ask) {
    $('#ask-text').value = ask;
    send(ask, $('#ask-channel').value, null);
  }
}

/* ------------------------------------------------------------ scenarios */

function renderScenarios() {
  const list = $('#scenario-list');
  list.innerHTML = '';
  state.scenarios.forEach((scenario) => {
    const button = el('button');
    button.append(el('div', 's-title', scenario.title));
    button.append(el('div', 's-expect', scenario.expectation));
    button.addEventListener('click', () => {
      $$('#scenario-list button').forEach((b) =>
        b.classList.toggle('is-active', b === button)
      );
      send(scenario.text, scenario.channel, scenario);
    });
    const item = el('li');
    item.append(button);
    list.append(item);
  });
}

$('#ask-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const text = $('#ask-text').value.trim();
  if (!text) return;
  $$('#scenario-list button').forEach((b) => b.classList.remove('is-active'));
  send(text, $('#ask-channel').value, null);
});

/* --------------------------------------------------------------- a turn */

async function send(text, channel, scenario) {
  const stage = $('#stage');
  const empty = $('#empty-state');
  if (empty) empty.remove();

  const pending = el('div', 'card');
  pending.append(el('div', 'card-body', 'Routing…'));
  stage.prepend(pending);

  try {
    const data = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ text, channel }),
    });
    pending.replaceWith(renderTurn(text, data, scenario));
  } catch (error) {
    pending.replaceWith(errorCard(error));
  }
}

function errorCard(error) {
  const card = el('div', 'card');
  const body = el('div', 'card-body');
  body.append(el('strong', null, 'Request failed'));
  body.append(el('p', 'hint', String(error.message || error)));
  card.append(body);
  return card;
}

function renderTurn(text, data, scenario) {
  const result = data.result;
  const routing = result.routing;
  const thresholds = state.system.thresholds;
  const threshold = thresholds.route[routing.intent] ?? thresholds.default_route;

  const card = el('div', 'card');

  /* header */
  const head = el('div', 'card-head');
  head.append(el('h3', null, scenario ? scenario.title : 'Ad hoc request'));
  const pills = el('div', 'row');
  pills.append(pill(`${fixed(result.total_latency_ms, 1)} ms`, ''));
  pills.append(pill(routing.model, ''));
  head.append(pills);
  card.append(head);

  /* what was said */
  const said = el('div', 'card-body');
  const utterance = el('div', 'utterance');
  utterance.append(el('span', 'who', 'Member message'));
  utterance.append(document.createTextNode(text));
  said.append(utterance);
  if (result.redacted_text !== text) {
    said.append(
      el('p', 'hint', `Redacted before logging and model use: "${result.redacted_text}"`)
    );
  }
  card.append(said);

  /* routing distribution */
  const routeBody = el('div', 'card-body');
  routeBody.append(el('h3', null, 'Decision step — probability over a fixed option set'));
  routeBody.append(distribution(routing, threshold));
  routeBody.append(signals(routing, thresholds));
  card.append(routeBody);

  /* verdict */
  const verdictBody = el('div', 'card-body');
  verdictBody.append(verdict(result, data.automated));
  card.append(verdictBody);

  /* answer */
  const answerBody = el('div', 'card-body');
  answerBody.append(el('h3', null, data.automated ? 'Answer' : 'What the member sees'));
  answerBody.append(el('div', 'answer', data.display_text));
  if (result.answer && result.answer.citations.length) {
    const list = el('ul', 'citations');
    result.answer.citations.forEach((citation, index) => {
      const item = el('li');
      item.append(
        el('div', 'c-title', `[${index + 1}] ${citation.title}${citation.section ? ' — ' + citation.section : ''}`)
      );
      item.append(el('div', 'c-snip', citation.snippet));
      list.append(item);
    });
    answerBody.append(list);
  }
  card.append(answerBody);

  /* handoff packet */
  if (result.handoff) {
    const body = el('div', 'card-body');
    body.append(el('h3', null, `Handoff packet → ${result.handoff.queue} (${result.handoff.priority})`));
    const pre = el('pre', 'result', JSON.stringify(result.handoff.context, null, 2));
    body.append(pre);
    card.append(body);
  }

  /* tools */
  const calls = result.answer ? result.answer.tool_calls : [];
  if (calls.length) {
    const body = el('div', 'card-body');
    body.append(el('h3', null, 'Governed tool calls'));
    const wrap = el('div', 'toolcalls');
    calls.forEach((call) => {
      const row = el('div', 'toolcall');
      row.append(el('span', call.ok ? 'ok' : 'bad', call.ok ? '✓' : '✕'));
      row.append(el('span', null, call.tool));
      row.append(el('span', 'dim', `gateway: ${call.gateway_decision}`));
      row.append(el('span', 'dim', `${fixed(call.duration_ms, 1)} ms`));
      row.append(el('span', 'dim', call.correlation_id.slice(0, 10)));
      wrap.append(row);
    });
    body.append(wrap);
    card.append(body);
  }

  /* audit */
  const auditBody = el('div', 'card-body');
  auditBody.append(el('h3', null, `Audit records written (${result.audit_events.length})`));
  const events = el('div', 'events');
  result.audit_events.forEach((event) => {
    events.append(el('span', 'event', `${event.category}.${event.action} → ${event.outcome}`));
  });
  auditBody.append(events);
  if (scenario) auditBody.append(el('div', 'teaches', scenario.teaches));
  card.append(auditBody);

  return card;
}

function pill(text, extra) {
  return el('span', `pill ${extra}`.trim(), text);
}

function distribution(routing, threshold) {
  const wrap = el('div', 'dist');
  Object.entries(routing.probabilities)
    .sort((a, b) => b[1] - a[1])
    .forEach(([name, probability]) => {
      const isTop = name === routing.intent;
      const below = isTop && probability < threshold;
      const row = el('div', `dist-row${isTop ? ' is-top' : ''}${below ? ' is-below' : ''}`);
      row.append(el('span', 'name', name));
      row.append(el('span', 'val', fixed(probability, 3)));
      const track = el('div', 'track');
      const fill = el('div', 'fill');
      fill.style.width = `${probability * 100}%`;
      track.append(fill);
      row.append(track);
      wrap.append(row);
    });
  const note = el('p', 'hint');
  note.textContent =
    `Threshold for ${routing.intent}: ${fixed(threshold, 2)}. ` +
    `Second-place margin: ${fixed(routing.top_margin, 3)}.`;
  wrap.append(note);
  return wrap;
}

function signals(routing, thresholds) {
  const wrap = el('div', 'signals');
  const add = (key, value, hot, suffix = '') => {
    const node = el('div', `signal${hot ? ' is-hot' : ''}`);
    node.append(el('span', 'k', key));
    node.append(el('b', null, `${value}${suffix}`));
    wrap.append(node);
  };
  add('urgency (0–3)', fixed(routing.urgency), routing.urgency >= thresholds.urgency,
      ` ${routing.urgency_label}`);
  add('clinician required', fixed(routing.clinician_required_probability),
      routing.clinician_required_probability >= thresholds.clinician_required);
  add('prompt injection', fixed(routing.injection_probability),
      routing.injection_probability >= thresholds.injection);
  if (routing.degraded) add('degraded', 'yes', true);
  return wrap;
}

function verdict(result, automated) {
  const node = el('div', `verdict ${automated ? 'auto' : 'human'}`);
  node.append(el('span', 'badge', automated ? 'Automated' : 'Human in the loop'));
  const why = el('div', 'why');
  const gate = result.audit_events.find((e) => e.category === 'gate');
  const grounding = result.audit_events.find((e) => e.category === 'grounding');
  if (automated) {
    why.append(document.createTextNode(`Gate passed: ${gate ? gate.detail.explanation : ''}.`));
  } else {
    const handoff = result.handoff;
    why.append(
      document.createTextNode(
        `Gate "${handoff.reason}" fired → ${handoff.queue} queue, ${handoff.priority} priority. ` +
        `${handoff.context.gate_explanation || ''}`
      )
    );
  }
  if (grounding) {
    why.append(el('br'));
    why.append(
      document.createTextNode(
        `Grounding judge: ${fixed(grounding.detail.score)} / 3.00 (${grounding.detail.label}), ` +
        `threshold ${grounding.detail.threshold}; unsafe-content probability ` +
        `${fixed(grounding.detail.unsafe_probability)}.`
      )
    );
  }
  node.append(why);
  return node;
}

/* ---------------------------------------------------------------- audit */

async function loadAudit() {
  const data = await api('/api/audit?limit=300');
  const pill = $('#chain-pill');
  pill.textContent = data.chain_valid
    ? `hash chain valid · ${data.total} records`
    : 'HASH CHAIN BROKEN';
  pill.className = `pill ${data.chain_valid ? 'pill-good' : 'pill-bad'}`;

  const body = $('#audit-table tbody');
  body.innerHTML = '';
  data.events
    .slice()
    .reverse()
    .forEach((event) => {
      const row = el('tr');
      row.append(el('td', 'mono', String(event.sequence)));
      row.append(el('td', null, event.category));
      row.append(el('td', 'mono', event.action));
      row.append(el('td', null, event.outcome));
      row.append(el('td', 'detail', summarise(event.detail)));
      row.append(el('td', 'mono', event.hash.slice(0, 10)));
      body.append(row);
    });
}

function summarise(detail) {
  return Object.entries(detail)
    .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && !v.length))
    .slice(0, 6)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join('  ');
}

$('#refresh-audit').addEventListener('click', loadAudit);

/* ---------------------------------------------------------------- tools */

function renderTools() {
  const wrap = $('#tool-cards');
  wrap.innerHTML = '';
  state.system.tools.forEach((tool) => {
    const annotations = tool.annotations || {};
    const blocked = tool.name === 'submit_prior_authorization';
    const card = el('div', `tool-card${blocked ? ' blocked' : ''}`);
    card.append(el('h4', null, tool.name));
    card.append(el('p', null, tool.description));
    const meta = el('div', 'tool-meta');
    meta.append(pill(annotations.riskTier || 'unknown', ''));
    meta.append(pill(annotations.readOnlyHint ? 'read only' : 'writes', annotations.readOnlyHint ? '' : 'pill-warn'));
    meta.append(pill(blocked ? 'gateway: blocked' : 'gateway: allowed', blocked ? 'pill-bad' : 'pill-good'));
    card.append(meta);
    wrap.append(card);
  });
}

$('#try-blocked').addEventListener('click', async () => {
  const output = $('#blocked-result');
  output.hidden = false;
  output.textContent = 'calling…';
  const response = await fetch('/mcp', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-agent-id': 'member-assistant',
      'x-correlation-id': crypto.randomUUID(),
      'ocp-apim-subscription-key': 'local-demo-key',
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: {
        name: 'submit_prior_authorization',
        arguments: { member_id: 'NW-100002', service_code: '72148', clinical_summary: 'synthetic' },
      },
    }),
  });
  output.textContent = JSON.stringify(await response.json(), null, 2);
});

/* --------------------------------------------------------------- egress */

const EGRESS_SAMPLES = [
  'https://apim-payer-agents.azure-api.net/mcp',
  'https://myaccount.fhir.azurehealthcareapis.com/Patient',
  'https://payer-search.search.windows.net/indexes/plan-benefits/docs',
  'https://api.openai.com/v1/chat/completions',
  'https://notes.pastebin.com/raw/abc123',
  'https://random-vendor.example.net/collect',
];

function renderEgressPolicy() {
  const policy = state.system.egress_policy;
  const body = $('#egress-table tbody');
  body.innerHTML = '';
  policy.rules.forEach((rule, index) => {
    const row = el('tr');
    row.append(el('td', 'mono', String(index + 1)));
    row.append(el('td', null, rule.name));
    row.append(el('td', 'mono', rule.host));
    const action = el('td');
    action.append(pill(rule.action, rule.action === 'Deny' ? 'pill-bad' : 'pill-good'));
    row.append(action);
    body.append(row);
  });
  const fallback = el('tr');
  fallback.append(el('td', 'mono', '—'));
  fallback.append(el('td', null, 'default action'));
  fallback.append(el('td', 'mono', '*'));
  const action = el('td');
  action.append(pill(policy.default_action, policy.default_action === 'Deny' ? 'pill-bad' : 'pill-good'));
  fallback.append(action);
  body.append(fallback);

  const chips = $('#egress-samples');
  chips.innerHTML = '';
  EGRESS_SAMPLES.forEach((url) => {
    const button = el('button', null, new URL(url).host);
    button.type = 'button';
    button.addEventListener('click', () => {
      $('#egress-url').value = url;
      evaluateEgress(url);
    });
    chips.append(button);
  });
}

async function evaluateEgress(url) {
  const output = $('#egress-result');
  output.hidden = false;
  const decision = await api('/api/egress/evaluate', {
    method: 'POST',
    body: JSON.stringify({ url }),
  });
  output.textContent = JSON.stringify(decision, null, 2);
}

$('#egress-form').addEventListener('submit', (event) => {
  event.preventDefault();
  evaluateEgress($('#egress-url').value.trim());
});

/* ---------------------------------------------------------------- evals */

$('#run-evals').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = 'Running…';
  const output = $('#eval-output');
  output.innerHTML = '<p class="dim">Scoring labelled cases…</p>';
  try {
    renderEvals(await api('/api/evals'));
  } finally {
    button.disabled = false;
    button.textContent = 'Run suite';
  }
});

function renderEvals(report) {
  const output = $('#eval-output');
  output.innerHTML = '';

  const metrics = el('div', 'metrics');
  const addMetric = (value, key) => {
    const node = el('div', 'metric');
    node.append(el('div', 'm-val', value));
    node.append(el('div', 'm-key', key));
    metrics.append(node);
  };
  addMetric(String(report.sample_size), 'labelled cases');
  addMetric(pct(report.routing_accuracy), 'routing accuracy');
  addMetric(pct(report.human_routing_accuracy), 'human-referral recall');
  addMetric(pct(report.automation_rate), 'automation rate');
  addMetric(fixed(report.calibration.expected_calibration_error, 3), 'expected calibration error');
  addMetric(
    report.mean_grounding_score === null ? '—' : `${fixed(report.mean_grounding_score)} / 3`,
    'mean grounding score'
  );
  output.append(metrics);

  output.append(el('h3', null, 'Threshold sweep — what each operating point buys and costs'));
  const wrap = el('div', 'table-wrap');
  const table = el('table', 'grid');
  table.innerHTML =
    '<thead><tr><th>threshold</th><th>automated</th><th>deferred</th>' +
    '<th>automation rate</th><th>error rate on automated slice</th></tr></thead>';
  const body = el('tbody');
  report.threshold_points.forEach((point) => {
    const row = el('tr');
    row.append(el('td', 'mono', fixed(point.threshold)));
    row.append(el('td', 'mono', String(point.automated)));
    row.append(el('td', 'mono', String(point.deferred)));
    row.append(el('td', 'mono', pct(point.automated / (point.automated + point.deferred || 1))));
    row.append(el('td', 'mono', pct(point.automated ? point.automated_errors / point.automated : 0)));
    body.append(row);
  });
  table.append(body);
  wrap.append(table);
  output.append(wrap);

  const misses = report.outcomes.filter((o) => !o.correct);
  if (misses.length) {
    output.append(el('h3', 'mt', `Routing misses (${misses.length})`));
    const list = el('ul', 'notes');
    misses.forEach((miss) => {
      list.append(
        el(
          'li',
          null,
          `${miss.id}: expected ${miss.expected}, got ${miss.predicted} at ` +
          `${fixed(miss.confidence)} — handed to a human: ${!miss.automated}`
        )
      );
    });
    output.append(list);
  }

  const notes = el('ul', 'notes');
  report.notes.forEach((note) => notes.append(el('li', null, note)));
  output.append(el('h3', 'mt', 'Reading these numbers'));
  output.append(notes);
}

boot().catch((error) => {
  document.querySelector('#stage').replaceChildren(errorCard(error));
});
