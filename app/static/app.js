'use strict';
// Portfolio Trade Execution Engine UI: plain fetch and WebSocket, no framework; all state lives in `state`.

const $ = (id) => document.getElementById(id);
const state = { brokers: [], broker: null, session: null, holdings: null, portfolio: null, portfolioError: 'empty', note: null,
  lastRequest: null, report: null, httpStatus: null, replay: false, ws: null, pollTimer: null };
const TERMINAL_RUN = new Set(['PLANNED', 'COMPLETED', 'PARTIALLY_FAILED', 'FAILED']);
const TERMINAL_ORDER = new Set(['FILLED', 'PARTIALLY_FILLED', 'REJECTED', 'CANCELLED', 'FAILED', 'TIMED_OUT', 'UNKNOWN', 'SKIPPED']);
const SUMMARY_KEYS = ['total', 'filled', 'partially_filled', 'rejected', 'cancelled', 'failed', 'timed_out', 'unknown', 'skipped'];
const PLAIN_CRED_RE = /redirect|client_code|client_id|user_id|seed|rate|latency|polls|ambiguous|partial/; // never masked


const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v) => (v === null || v === undefined || v === '' ? '-' : String(v));
const td = (v) => esc(fmt(v));
const when = (iso) => (iso ? new Date(iso).toLocaleString() : '-');
const genKey = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
const debounce = (fn, ms) => { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; };
const statusPill = (s) => `<span class="pill s-${esc(s)}"${s === 'UNKNOWN' ? ' title="verify at broker; not resent"' : ''}>${esc(s)}</span>`;
const table = (headers, rows) => `<table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${
  rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: { Accept: 'application/json' } };
  if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (res.ok) return { data, status: res.status, headers: res.headers };
  const e = (data && data.error) || { code: `http_${res.status}`, message: (data && data.detail) || text || res.statusText, details: null };
  throw Object.assign(new Error(e.message), { code: e.code, details: e.details, status: res.status });
}

function showError(id, err) {
  const el = $(id);
  if (!err) { el.hidden = true; el.innerHTML = ''; return; }
  const d = err.details;
  const item = (x) => (typeof x === 'string' ? x : (x.symbol ? `${x.symbol}: ` : Array.isArray(x.loc) ? `${x.loc.join('.')}: ` : '') + (x.message || x.msg || JSON.stringify(x)));
  el.innerHTML = `<strong>${esc(err.code || 'error')}</strong> ${esc(err.message || String(err))}` +
    (Array.isArray(d) && d.length ? `<ul>${d.map((x) => `<li>${esc(item(x))}</li>`).join('')}</ul>` : d && typeof d === 'object' ? `<pre>${esc(JSON.stringify(d, null, 1))}</pre>` : '');
  el.hidden = false;
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).catch(() => {}); return; }
  const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } catch {} ta.remove();
}


function parsePortfolio() {
  const text = $('portfolio').value.trim();
  state.portfolio = null; state.portfolioError = text ? null : 'empty';
  if (!text) return;
  try {
    const p = JSON.parse(text);
    if (!p || typeof p !== 'object' || Array.isArray(p)) throw new Error('must be a JSON object');
    const list = p.mode === 'first_time' ? 'lines' : p.mode === 'rebalance' ? 'instructions' : null;
    if (!list) throw new Error('"mode" must be "first_time" or "rebalance"');
    if (!Array.isArray(p[list]) || !p[list].length) throw new Error(`${p.mode} needs a non-empty "${list}" array`);
    state.portfolio = p;
  } catch (e) { state.portfolioError = e.message; }
}

function renderPortfolio() {
  const st = $('json-status'); const err = state.portfolioError; const p = state.portfolio;
  if (err) {
    st.className = err === 'empty' ? 'status muted' : 'status bad';
    st.textContent = err === 'empty' ? 'Paste a portfolio, upload a file or load a sample.' : `Invalid JSON: ${err}`;
    $('preview').innerHTML = '';
  } else {
    const rows = p.lines || p.instructions;
    st.className = 'status';
    st.innerHTML = `<span class="pill ok">valid</span> mode <code>${esc(p.mode)}</code> · ${rows.length} ${p.lines ? 'lines' : 'instructions'}`
      + (state.note ? ` · <span class="muted">${esc(state.note)}</span>` : '');
    $('preview').innerHTML = table(['symbol', 'exchange', 'action', 'qty / delta'], rows.map((r) => [td(r.symbol), td(r.exchange || 'NSE'), td(r.action || 'BUY'),
      td(r.quantity_delta != null ? (r.quantity_delta > 0 ? '+' : '') + r.quantity_delta : r.quantity)]));
  }
  renderExecuteGate();
}

function loadPortfolio(obj) {
  // a full request body is unwrapped, but dry_run and idempotency_key are never taken from a file
  const full = obj && obj.portfolio && typeof obj.portfolio === 'object';
  if (full) {
    if (typeof obj.allow_existing_holdings === 'boolean') $('allow-existing').checked = obj.allow_existing_holdings;
    if (obj.on_sell_shortfall) $('on-sell-shortfall').value = obj.on_sell_shortfall;
  }
  state.note = full && typeof obj._note === 'string' ? obj._note : null;
  $('portfolio').value = JSON.stringify(full ? obj.portfolio : obj, null, 2);
  parsePortfolio(); renderPortfolio();
}

async function loadSample(name) {
  try { loadPortfolio((await api(`/static/samples/${name}.json`)).data); }
  catch (e) { $('json-status').className = 'status bad'; $('json-status').textContent = `Could not load sample: ${e.message}`; }
}

function readFile(file) {
  if (!file) return;
  const r = new FileReader();
  r.onload = () => { try { loadPortfolio(JSON.parse(r.result)); } catch { $('portfolio').value = String(r.result); parsePortfolio(); renderPortfolio(); } };
  r.readAsText(file);
}


async function loadBrokers() {
  try { state.brokers = (await api('/brokers')).data || []; }
  catch (e) { showError('broker-error', e); $('broker').innerHTML = '<option value="">unavailable</option>'; return; }
  $('broker').innerHTML = state.brokers.map((b) => `<option value="${esc(b.name)}">${esc(b.display_name || b.name)}${b.live_tested ? '' : ' (not live-tested)'}</option>`).join('');
  if (state.brokers.some((b) => b.name === 'paper')) $('broker').value = 'paper';
  selectBroker();
}

function selectBroker() { state.broker = state.brokers.find((b) => b.name === $('broker').value) || null; renderBroker(); }

function credField(name, required) {
  const secret = state.broker.name !== 'paper' && !PLAIN_CRED_RE.test(name); const id = `cred-${name}`;
  const input = name === 'seed_holdings'
    ? `<textarea id="${id}" name="${esc(name)}" rows="2" placeholder="INFY:10,TCS:5, or a JSON list of {symbol, quantity}"></textarea>`
    : `<input id="${id}" name="${esc(name)}" type="${secret ? 'password' : 'text'}" autocomplete="off" spellcheck="false"${required ? ' required' : ''}>`;
  return `<div class="field"><label for="${id}">${esc(name)} ${required ? '<span class="req" title="required">*</span>' : '<span class="muted">(optional)</span>'}</label>` +
    `<div class="row">${input}${secret ? `<button type="button" class="toggle small" data-for="${id}" aria-label="show ${esc(name)}">show</button>` : ''}</div></div>`;
}

function renderBroker() {
  const b = state.broker; const badge = $('broker-badge');
  if (!b) { $('creds').innerHTML = ''; badge.textContent = ''; return; }
  badge.textContent = b.live_tested ? 'live-tested' : 'not live-tested'; badge.className = `pill ${b.live_tested ? 'ok' : 'warn'}`;
  $('cred-help').textContent = b.credential_help || ((b.required_credentials || []).length ? '' : 'No credentials required.');
  $('creds').innerHTML = (b.required_credentials || []).map((n) => credField(n, true)).concat((b.optional_credentials || []).map((n) => credField(n, false))).join('');
  $('btn-login-url').hidden = !b.login_via_redirect; $('login-url').hidden = true;
  showError('broker-error', null);
}

function collectCredentials() {
  const creds = {};
  $('creds').querySelectorAll('input, textarea').forEach((el) => {
    let v = el.value.trim();
    if (!v) return;
    if (el.name === 'seed_holdings' && v.startsWith('[')) { try { v = JSON.parse(v).map((h) => `${h.symbol}:${h.quantity}`).join(','); } catch {} }
    creds[el.name] = v;
  });
  return creds;
}

async function getLoginUrl() {
  showError('broker-error', null);
  try {
    const { data } = await api(`/brokers/${encodeURIComponent(state.broker.name)}/login-url`, { method: 'POST', body: { credentials: collectCredentials() } });
    $('login-url').innerHTML = data && data.url
      ? `Open <a href="${esc(data.url)}" target="_blank" rel="noopener">${esc(data.url)}</a>, log in, then paste the callback token (request_token / auth_code / code) into the matching field above and click Connect.`
      : 'This broker logs in with direct credentials (no redirect needed); fill the fields and click Connect.';
    $('login-url').hidden = false;
  } catch (e) { showError('broker-error', e); }
}

async function connect() {
  if (!state.broker) return;
  showError('broker-error', null); $('btn-connect').disabled = true;
  try {
    state.session = (await api(`/brokers/${encodeURIComponent(state.broker.name)}/sessions`, { method: 'POST', body: { credentials: collectCredentials() } })).data;
    state.holdings = null;
    $('creds').querySelectorAll('input').forEach((el) => { el.value = ''; }); // secrets never persist in the page
    renderSession(); await loadHoldings();
  } catch (e) { showError('broker-error', e); }
  $('btn-connect').disabled = false;
}

async function disconnect() {
  if (!state.session) return;
  try { await api(`/sessions/${encodeURIComponent(state.session.session_id)}`, { method: 'DELETE' }); }
  catch (e) { if (e.status !== 401) showError('broker-error', e); }
  state.session = null; state.holdings = null;
  renderSession(); renderHoldings();
}

function renderSession() {
  const s = state.session;
  $('session-info').innerHTML = s
    ? `<span class="pill ok">connected</span> <code>${esc(s.broker)}</code> session <code>${esc(s.session_id)}</code> <button type="button" class="small" data-copy="${esc(s.session_id)}">copy</button>` +
      ` · user ${esc(fmt(s.user_id))} · expires ${esc(when(s.expires_at))}`
    : '<span class="pill">not connected</span>';
  $('btn-disconnect').disabled = !s; $('btn-holdings').disabled = !s;
  renderExecuteGate();
}

async function loadHoldings() {
  if (!state.session) return;
  try { state.holdings = (await api(`/sessions/${encodeURIComponent(state.session.session_id)}/holdings`)).data.holdings || []; }
  catch (e) { showError('broker-error', e); state.holdings = null; }
  renderHoldings();
}

function renderHoldings() {
  const h = state.holdings;
  $('holdings').innerHTML = !h ? '' : !h.length ? '<p class="muted">No holdings in this account.</p>'
    : `<h3>Holdings (${h.length})</h3>` + table(['symbol', 'exchange', 'free qty', 'avg price', 'last price', 'isin'],
      h.map((x) => [td(x.symbol), td(x.exchange), td(x.quantity), td(x.average_price), td(x.last_price), td(x.isin)]));
}


function buildRequest() {
  const body = { session_id: state.session.session_id, portfolio: state.portfolio, dry_run: $('dry-run').checked,
    allow_existing_holdings: $('allow-existing').checked, on_sell_shortfall: $('on-sell-shortfall').value };
  const key = $('idempotency-key').value.trim();
  if (key) body.idempotency_key = key;
  return body;
}

function renderExecuteGate() {
  const ok = Boolean(state.session && state.portfolio);
  $('btn-execute').disabled = !ok; $('btn-resend').disabled = !(ok && state.lastRequest);
  $('exec-gate').textContent = ok ? '' : !state.session ? 'Connect a broker (panel 2) to enable.' : 'Fix the portfolio JSON (panel 1) to enable.';
}

async function execute(body) {
  if (!body) return;
  showError('exec-error', null); stopLive();
  $('btn-execute').disabled = true; $('btn-resend').disabled = true;
  try {
    const { data, status, headers } = await api('/executions', { method: 'POST', body });
    state.lastRequest = body; state.httpStatus = status; state.replay = headers.get('Idempotent-Replay') === 'true';
    $('idempotency-key').value = genKey();
    applyReport(data);
    if (data && !TERMINAL_RUN.has(data.status)) connectWs(data.run_id); else loadWebhooks();
    loadRecent();
  } catch (e) { showError('exec-error', e); }
  renderExecuteGate();
}

function renderExecInfo() {
  const r = state.report;
  $('exec-info').innerHTML = !r ? '' : `run <code>${esc(r.run_id)}</code> <button type="button" class="small" data-copy="${esc(r.run_id)}">copy</button>` +
    ` · HTTP ${esc(fmt(state.httpStatus))} · ${statusPill(r.status)}` +
    (state.replay ? ' <span class="pill warn">Idempotent-Replay: true</span> <span class="muted">same key + same payload: the stored run was returned, nothing was re-sent</span>' : '');
}


function setLive(mode) { const b = $('live-badge'); b.textContent = mode ? `live: ${mode}` : ''; b.className = `pill ${mode === 'websocket' ? 'live' : mode ? 'warn' : ''}`; }

function applyReport(report) { if (report && report.run_id) { state.report = report; renderExecInfo(); renderReport(); } }

function connectWs(runId) {
  let ws;
  try { ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/executions/${encodeURIComponent(runId)}`); }
  catch { startPolling(runId); return; }
  state.ws = ws; setLive('websocket');
  ws.onmessage = (ev) => {
    let frame; try { frame = JSON.parse(ev.data); } catch { return; }
    if (frame && frame.report) applyReport(frame.report);
    if (frame && frame.type === 'run.completed') { loadWebhooks(); loadRecent(); loadHoldings(); }
  };
  ws.onerror = () => { if (state.ws === ws) startPolling(runId); };
  ws.onclose = (ev) => {
    if (state.ws !== ws) return;
    state.ws = null;
    if (ev.code === 4404) { setLive(''); showError('exec-error', { code: 'run_not_found', message: `run ${runId} is unknown to the server` }); return; }
    if (state.report && !TERMINAL_RUN.has(state.report.status)) startPolling(runId); else setLive('');
  };
}

function startPolling(runId) {
  if (state.ws) { const w = state.ws; state.ws = null; try { w.close(); } catch {} }
  if (state.pollTimer) return;
  setLive('polling');
  const tick = async () => {
    try { applyReport((await api(`/executions/${encodeURIComponent(runId)}`)).data); }
    catch (e) { if (e.status === 404) { stopLive(); showError('exec-error', e); } return; }
    if (state.report && TERMINAL_RUN.has(state.report.status)) { stopLive(); loadWebhooks(); loadRecent(); loadHoldings(); }
  };
  state.pollTimer = setInterval(tick, 2000); tick();
}

function stopLive() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  if (state.ws) { const w = state.ws; state.ws = null; try { w.close(); } catch {} }
  setLive('');
}


function renderReport() {
  const r = state.report;
  if (!r) { ['summary', 'phase', 'warnings', 'orders', 'recon', 'raw'].forEach((id) => { $(id).innerHTML = ''; }); $('banner').hidden = true; return; }
  $('banner').hidden = !r.dry_run;
  $('banner').textContent = r.dry_run ? 'Dry run: this is the plan only. Nothing was placed at the broker.' : '';
  const s = r.summary || {}; const orders = r.orders || []; const running = r.status === 'RUNNING';
  $('summary').innerHTML = SUMMARY_KEYS.map((k) => `<div class="tile t-${k}${s[k] ? ' has' : ''}"><span class="n">${esc(s[k] ?? 0)}</span><span class="k">${k.replace('_', ' ')}</span></div>`).join('');
  const sells = orders.filter((o) => o.phase === 'SELL'); const buys = orders.filter((o) => o.phase === 'BUY');
  const sellsDone = sells.every((o) => TERMINAL_ORDER.has(o.status));
  $('phase').innerHTML = `Phase <span class="pill ${sellsDone ? 'ok' : running ? 'live' : ''}">SELL ×${sells.length}</span> → ` +
    `<span class="pill ${sellsDone && !running ? 'ok' : sellsDone ? 'live' : ''}">BUY ×${buys.length}</span>` +
    ` · broker <code>${esc(r.broker)}</code> · mode <code>${esc(r.mode)}</code> · on_sell_shortfall <code>${esc(r.on_sell_shortfall)}</code>` +
    ` · started ${esc(when(r.started_at))}${r.finished_at ? ` · finished ${esc(when(r.finished_at))}` : ''}`;
  $('warnings').innerHTML = ((r.plan && r.plan.warnings) || []).map((w) => `<li>${esc(w)}</li>`).join('');
  $('orders').innerHTML = !orders.length ? '<p class="muted">No orders in this run.</p>'
    : table(['#', 'phase', 'side', 'symbol', 'qty', 'filled', 'avg', 'status', 'attempts', 'broker order id', 'tag', 'error'],
      orders.map((o) => [td(o.seq), td(o.phase), td(o.side), `${td(o.symbol)} <small class="muted">${td(o.exchange)}</small>`, td(o.quantity), td(o.filled_qty),
        td(o.average_price), statusPill(o.status), td(o.attempts), td(o.broker_order_id), `<code>${td(o.tag)}</code>`,
        `${o.error_code ? `<strong>${esc(o.error_code)}</strong> ` : ''}${esc(o.error_message || '')}`]));
  const rc = r.reconciliation;
  $('recon').innerHTML = !rc ? `Reconciliation <span class="pill">${running ? 'after the run' : 'not run'}</span>`
    : `Reconciliation <span class="pill r-${esc(rc.status)}">${esc(rc.status)}</span> ${esc(rc.note || '')}` + (!(rc.diffs || []).length ? ''
      : `<ul>${rc.diffs.map((d) => `<li><code>${esc(d.symbol)}</code> ${esc(d.exchange)}: expected ${esc(d.expected)}, actual ${esc(d.actual)}${d.explanation ? ` (${esc(d.explanation)})` : ''}</li>`).join('')}</ul>`);
  $('raw').textContent = JSON.stringify(r, null, 2);
}

async function loadRecent() {
  try {
    const { data } = await api('/executions?limit=50'); const sel = $('recent'); const current = state.report ? state.report.run_id : sel.value;
    sel.innerHTML = '<option value="">select a run</option>' + (data || []).map((r) =>
      `<option value="${esc(r.run_id)}">${esc(String(r.run_id).slice(0, 8))} · ${esc(r.mode)} · ${esc(r.status)}${r.dry_run ? ' · dry' : ''} · ${esc(when(r.started_at))}</option>`).join('');
    sel.value = current || '';
  } catch {}
}

async function openRun(runId) {
  if (!runId) return;
  stopLive(); showError('exec-error', null);
  try {
    const { data } = await api(`/executions/${encodeURIComponent(runId)}`);
    state.httpStatus = 200; state.replay = false; applyReport(data);
    if (!TERMINAL_RUN.has(data.status)) connectWs(runId); else loadWebhooks();
  } catch (e) { showError('exec-error', e); }
}

async function loadWebhooks() {
  try {
    const d = (await api('/mock/webhook')).data.deliveries || [];
    $('webhook-count').textContent = d.length;
    $('webhook').innerHTML = !d.length ? '<p class="muted">No deliveries yet.</p>' : table(['received', 'type', 'run', 'status', 'filled', 'failed', 'skipped'], d.map((x) => {
      const rep = x.report || {}; const s = rep.summary || {};
      return [td(when(x.ts)), td(x.type), `<code>${td(String(x.run_id || '').slice(0, 8))}</code>`, rep.status ? statusPill(rep.status) : '-', td(s.filled), td(s.failed), td(s.skipped)];
    }));
  } catch { $('webhook').innerHTML = '<p class="muted">Webhook endpoint unavailable.</p>'; }
}


function wire() {
  $('btn-sample-first').addEventListener('click', () => loadSample('first_time'));
  $('btn-sample-rebal').addEventListener('click', () => loadSample('rebalance'));
  $('file').addEventListener('change', (e) => { readFile(e.target.files[0]); e.target.value = ''; });
  $('btn-validate').addEventListener('click', () => { parsePortfolio(); renderPortfolio(); });
  $('portfolio').addEventListener('input', debounce(() => { state.note = null; parsePortfolio(); renderPortfolio(); }, 250));
  $('btn-gen-key').addEventListener('click', () => { $('idempotency-key').value = genKey(); });
  $('broker').addEventListener('change', selectBroker);
  $('btn-login-url').addEventListener('click', getLoginUrl);
  $('btn-connect').addEventListener('click', connect);
  $('btn-disconnect').addEventListener('click', disconnect);
  $('btn-holdings').addEventListener('click', loadHoldings);
  $('btn-execute').addEventListener('click', () => execute(buildRequest()));
  $('btn-resend').addEventListener('click', () => execute(state.lastRequest));
  $('recent').addEventListener('change', (e) => openRun(e.target.value));
  $('btn-refresh-recent').addEventListener('click', loadRecent);
  $('btn-copy').addEventListener('click', () => copyText($('raw').textContent));
  $('webhook-details').addEventListener('toggle', (e) => { if (e.target.open) loadWebhooks(); });
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-copy], .toggle');
    if (!t) return;
    if (t.dataset.copy !== undefined) { copyText(t.dataset.copy); return; }
    const inp = $(t.dataset.for); const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password'; t.textContent = show ? 'hide' : 'show';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  $('idempotency-key').value = genKey();
  parsePortfolio(); renderPortfolio(); renderSession();
  loadBrokers(); loadRecent(); wire();
});
