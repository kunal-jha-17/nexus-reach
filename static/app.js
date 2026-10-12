/* Nexus Reach front end -- plain JavaScript, no build step.
   Everything talks to the JSON API in app.py, so the UI can be redesigned or
   replaced (React, etc.) without touching any pipeline logic. */
'use strict';

/* ------------------------------------------------------------ helpers */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STAGES = ['new', 'contacted', 'replied', 'qualified', 'won', 'lost', 'dead'];
const CHANNELS = ['email', 'yelp', 'facebook', 'instagram', 'whatsapp', 'phone', 'unknown'];
const CHANNEL_LABEL = { email: 'Email', yelp: 'Yelp', facebook: 'Facebook', instagram: 'Instagram',
  whatsapp: 'WhatsApp', phone: 'Phone / SMS', unknown: 'No channel' };
const TRADES = ['', 'hvac', 'plumbing', 'electrical', 'pest_control', 'roofing', 'other'];
const TRADE_LABEL = { '': '—', hvac: 'HVAC', plumbing: 'Plumbing', electrical: 'Electrical',
  pest_control: 'Pest control', roofing: 'Roofing', other: 'Other' };
const JOB_LABEL = { scrape: 'Scrape', enrich: 'Enrich', judge: 'Judge fit', draft: 'Write messages', send_bulk: 'Send emails',
  pipeline: 'Enrich → judge → draft', followups: 'Follow-ups' };
const DAY_LABEL = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const state = {
  view: 'dashboard', settings: null, config: null, campaigns: [], defaultCriteria: '',
  leads: { page: 0, size: 50, total: 0, rows: [], selected: new Set(), allMatching: false },
  jobs: [], jobSeen: {}, openLogs: new Set(), drawer: { id: null, lead: null, templates: null },
  campaign: null, cd: null, me: null, unassigned: 0, savedDefaultCriteria: '',
  pendingFilter: null, prefillCampaign: '', connClear: new Set(),
};

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: { 'X-Requested-With': 'fetch' } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); }
  catch (e) { setOffline(true); throw new Error("Can't reach the server — it may be restarting. Try again in a moment."); }
  if (res.status === 401) { location.href = '/login'; throw new Error('Please sign in.'); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  // 502/503/504 with no message of our own = the host's "service unavailable" page: the app is restarting
  const down = [502, 503, 504].includes(res.status) && !(data && data.error);
  setOffline(down);
  if (down) throw new Error('The server is restarting — give it a minute, then try again.');
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}
function setOffline(on) { const el = $('#offline'); if (el) el.hidden = !on; }

function toast(msg, kind = '') {
  // the same message twice in a row is one message (a restart used to stack six identical errors)
  const dup = $$('#toasts .toast').find((t) => t.textContent === msg);
  if (dup) { clearTimeout(dup._t); dup._t = setTimeout(() => dup.remove(), kind === 'err' ? 7000 : 3500); return; }
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  el._t = setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3500);
}
const fail = (e) => toast(e.message || String(e), 'err');
async function run(fn) { try { return await fn(); } catch (e) { fail(e); } }

function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + 's')}`; }
function badge(sel, n) { const el = $(sel); if (!el) return; el.hidden = !n; el.textContent = n; }
function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const opt = (v, label, sel) => `<option value="${esc(v)}"${sel ? ' selected' : ''}>${esc(label)}</option>`;

/* -------------------------------------------------------------- router */
const VIEWS = ['dashboard', 'campaigns', 'campaign', 'find', 'leads', 'pipeline', 'outreach', 'settings'];
const loaders = {
  dashboard: loadDashboard, campaigns: loadCampaignsView, campaign: loadCampaignView, find: loadFind,
  leads: loadLeads, pipeline: loadPipeline, outreach: loadOutreach, settings: loadSettingsView,
};

function parseHash() {
  const raw = location.hash.slice(1), i = raw.indexOf('/');
  return i === -1 ? [raw, ''] : [raw.slice(0, i), decodeURIComponent(raw.slice(i + 1))];
}
const campaignHash = (name) => `#campaign/${encodeURIComponent(name)}`;

function route() {
  let [view, param] = parseHash();
  if (!VIEWS.includes(view)) view = 'dashboard';
  if (view === 'campaign' && !param) view = 'campaigns';
  const sameView = state.shown === view;
  state.view = view; state.shown = view;
  if (view === 'campaign') state.campaign = param;
  if (view === 'settings') {
    showSettingsTab(param);
    // Moving between Settings tabs must not reload the form: that would throw away
    // whatever was typed on the tab just left, before it was saved.
    if (sameView) return;
  }
  VIEWS.forEach((v) => { $(`#view-${v}`).hidden = v !== view; });
  $$('.rail a[data-view]').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  renderRailCampaigns();
  window.scrollTo(0, 0);
  run(() => loaders[view](param));
  renderJobs();
  refreshJobs();
}
window.addEventListener('hashchange', route);

/* ------------------------------------------------------------ shared data */
async function loadCampaigns() {
  const d = await api('/api/campaigns');
  state.campaigns = d.campaigns; state.unassigned = d.unassigned;
  state.defaultCriteria = d.default_criteria; state.savedDefaultCriteria = d.saved_default_criteria;
  const active = d.campaigns.filter((c) => c.status !== 'archived');
  const fill = (sel, list, extra = '') => {
    const el = $(sel), cur = el.value;
    el.innerHTML = opt('', 'All campaigns') + extra + list.map((c) => opt(c.name, `${c.name} (${c.leads})`, c.name === cur)).join('');
    if ([...el.options].some((o) => o.value === cur)) el.value = cur;
  };
  ['#dash-campaign', '#en-campaign', '#ju-campaign', '#dr-campaign', '#pl-campaign'].forEach((s) => fill(s, active));
  fill('#f-campaign', d.campaigns, opt('__none__', `No campaign (${d.unassigned})`));
  $('#campaign-list').innerHTML = active.map((c) => `<option value="${esc(c.name)}">`).join('');
  renderRailCampaigns();
  return d;
}

async function loadSettings() {
  state.settings = await api('/api/settings');
  return state.settings;
}
async function loadConfig() {
  state.config = await api('/api/config-status');
  return state.config;
}

function templateOptions(selected = '', list = null) {
  const t = list || (state.settings && state.settings.templates) || [];
  return opt('', "Match each lead's channel", selected === '') + opt('rotate-all', 'Rotate through all templates', selected === 'rotate-all')
    + t.map((x) => opt(`${x.channel}::${x.name}`, `${x.name} (${CHANNEL_LABEL[x.channel]})`, selected === `${x.channel}::${x.name}`)).join('');
}

/* ------------------------------------------------------------ dashboard */
function repliesHtml(list, emptyText) {
  return list.length ? list.map((r) =>
    `<div class="list-row"><div class="grow"><b>${esc(r.business_name || '(no name)')}</b>
       ${r.campaign ? `<span class="chip">${esc(r.campaign)}</span>` : ''} <span class="muted">${ago(r.replied_at)}</span>
       <div class="sub quote">${r.snippet ? '“' + esc(String(r.snippet).replace(/\s+/g, ' ').slice(0, 160)) + '”' : 'Marked as replied'}</div></div>
     <button class="btn small primary" data-open="${r.id}">Open</button></div>`).join('')
    : `<div class="empty small"><b>No replies waiting</b>${esc(emptyText)}</div>`;
}

async function loadDashboard() {
  await Promise.all([loadCampaigns(), loadSettings(), loadConfig()]);
  const camp = $('#dash-campaign').value, q = camp ? `?campaign=${encodeURIComponent(camp)}` : '';
  const [s, replies] = await Promise.all([api('/api/stats' + q), api('/api/replies' + q)]);
  const st = s.by_stage, sent = s.by_send_status.sent || 0, e = s.email;
  const answered = (st.replied || 0) + (st.qualified || 0) + (st.won || 0);
  const tiles = [
    ['Leads', s.total], ['Fit your criteria', s.fit.yes], ['Messages written', s.drafted],
    ['Contacted', sent], ['Replied', answered, sent ? `${Math.round((answered / sent) * 100)}% of contacted` : ''], ['Won', st.won || 0],
  ];
  $('#dash-tiles').innerHTML = tiles.map(([l, n, sub]) =>
    `<div class="tile"><div class="n">${n}</div><div class="l">${esc(l)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ''}</div>`).join('');

  const next = [];
  if (!s.total) next.push(['Import a list or run a scrape to get your first leads.', 'Find leads', 'find']);
  else {
    if (replies.length) next.push([`${plural(replies.length, 'person', 'people')} replied and ${replies.length === 1 ? 'is' : 'are'} waiting for an answer.`, 'Answer them', 'outreach']);
    const todo = (s.total - s.enriched) + s.fit.unjudged;
    if (todo > 0) next.push([`${plural(s.total - s.enriched, 'lead')} to enrich and ${s.fit.unjudged} to judge.`, 'Run all steps', 'pipeline']);
    if (s.fit.yes > s.drafted) next.push([`${plural(s.fit.yes - s.drafted, 'qualifying lead')} ${s.fit.yes - s.drafted === 1 ? 'has' : 'have'} no message yet.`, 'Write messages', 'outreach']);
    if (e.followups_due + e.hand_followups_due > 0) next.push([`${plural(e.followups_due + e.hand_followups_due, 'follow-up')} due.`, 'Follow up', 'outreach']);
    if (!next.length) next.push(['Nothing waiting. Find more leads, or work the Outreach tab.', 'Find leads', 'find']);
  }
  $('#dash-next').innerHTML = next.slice(0, 5).map(([t, b, v]) =>
    `<div class="next-item"><span>${esc(t)}</span><a class="btn small" href="#${v}">${esc(b)}</a></div>`).join('');

  badge('#dash-replies-n', replies.length);
  $('#dash-replies').innerHTML = repliesHtml(replies.slice(0, 5), e.imap_configured
    ? 'Replies to your emails show up here on their own.' : 'Connect your inbox under Settings → Connections and replies show up here on their own.');

  const maxStage = Math.max(1, ...STAGES.map((k) => st[k] || 0));
  $('#dash-stages').innerHTML = STAGES.map((k) =>
    `<div class="stagebar"><span>${k}</span><span class="track"><i style="width:${((st[k] || 0) / maxStage) * 100}%"></i></span><span class="num">${st[k] || 0}</span></div>`).join('');

  const active = state.campaigns.filter((c) => c.status !== 'archived');
  $('#dash-campaigns').innerHTML = active.length ? `<div class="camp-table"><div class="camp-row head"><span>Campaign</span><span>Leads</span><span>Fit</span><span>Contacted</span><span>Replied</span><span>Won</span></div>` + active.map((c) =>
    `<a class="camp-row" href="${campaignHash(c.name)}"><span><b>${esc(c.name)}</b></span><span>${c.leads}</span><span>${c.fit_yes}</span>
       <span>${c.contacted}</span><span>${c.replied}</span><span>${c.won}</span></a>`).join('') + '</div>'
    : '<div class="empty small"><b>No campaigns yet</b>Create one in <a href="#campaigns">Campaigns</a> — or import a list with a campaign name and it appears automatically.</div>';

  const checked = !e.imap_configured ? 'inbox not connected' : !e.reply_check_minutes ? 'only when you press Check'
    : e.last_reply_check_at ? `checked ${ago(e.last_reply_check_at)}` : `every ${e.reply_check_minutes} min`;
  $('#dash-budgets').innerHTML =
    `<div class="kv"><span>Emails sent today</span><b>${e.sent_today} of ${e.daily_limit}${e.warmup_active ? ' (warming up)' : ''}</b></div>` +
    (e.window_open ? '' : `<div class="kv"><span>Sending hours</span><b class="warn-text">closed right now</b></div>`) +
    `<div class="kv"><span>Follow-ups due</span><b>${e.followups_due} by email · ${e.hand_followups_due} by hand</b></div>` +
    `<div class="kv"><span>Replies</span><b>${esc(checked)}</b></div>` +
    `<div class="kv"><span>Web searches this month</span><b>${s.serpapi_used} of ${s.serpapi_limit}</b></div>` +
    `<div class="kv"><span>On do-not-contact</span><b>${s.dnc}</b></div>`;
}
$('#dash-campaign').addEventListener('change', () => run(loadDashboard));
$('#view-dashboard').addEventListener('click', (e) => {
  const b = e.target.closest('[data-open]'); if (b) run(() => openLead(+b.dataset.open));
});

/* ------------------------------------------------------------ find leads */
function syncScrapeForm() {
  const p = $('#sc-platform').value;
  $('#sc-query-label').textContent = p === 'instagram' ? 'Hashtag' : 'Search';
  $('#sc-query').placeholder = p === 'instagram' ? 'dallasrealestate' : 'AC repair; furnace repair';
  $('#sc-location').closest('.field').hidden = p === 'instagram';
  $('#sc-location').placeholder = p === 'yelp' ? 'Dallas, TX (required)' : 'Garland, TX; Mesquite, TX';
}
$('#sc-platform').addEventListener('change', syncScrapeForm);

async function loadFind() {
  await Promise.all([loadCampaigns(), loadConfig()]);
  syncScrapeForm();
  const noBrowser = $('#sc-nobrowser');
  noBrowser.hidden = !!state.config.playwright;
  if (!state.config.playwright) {
    noBrowser.innerHTML = "This server doesn't have the scraping browser installed &mdash; most free hosts " +
      "(including a plain Render deploy) can't have one added in the build step. Scrape on your own computer " +
      "instead, then <a href=\"#find\">import the CSV here</a>. (Self-hosting? The included <code>Dockerfile</code> " +
      "bundles the browser so scraping works on the server too &mdash; see the README.)";
  }
  $('#sc-go').disabled = !state.config.playwright;
  $('#sc-schedule').disabled = !state.config.playwright;
  if (state.prefillCampaign) {
    $('#imp-campaign').value = state.prefillCampaign; $('#sc-campaign').value = state.prefillCampaign;
    state.prefillCampaign = '';
  }
  const list = await api('/api/schedules');
  $('#schedule-list').innerHTML = list.length ? list.map((s) =>
    `<div class="list-row"><div class="grow"><b>${esc(s.query)}</b> ${esc(s.location || '')}
       <div class="sub">${esc(s.platform.replace('_', ' '))} · up to ${s.max_results} each · every ${s.interval_hours} h${s.only_no_website ? ' · no-website only' : ''} · ${s.last_run_at ? 'last ran ' + ago(s.last_run_at) : 'not run yet'}${s.campaign ? ' · ' + esc(s.campaign) : ''}${s.active ? '' : ' · <b>paused</b>'}</div></div>
     <button class="btn small" data-sched-toggle="${s.id}" data-active="${s.active}">${s.active ? 'Pause' : 'Resume'}</button>
     <button class="btn small danger" data-sched-del="${s.id}">Delete</button></div>`).join('')
    : '<div class="empty small"><b>No recurring scrapes</b>Use "Schedule" above to repeat a search automatically.</div>';
}

function scrapeBody() {
  const tidy = (v) => v.split(/[;\n]+/).map((x) => x.trim()).filter(Boolean).join('; ');
  return { platform: $('#sc-platform').value, query: tidy($('#sc-query').value), location: tidy($('#sc-location').value),
    max_results: +$('#sc-max').value || 30, campaign: $('#sc-campaign').value.trim(),
    only_no_website: $('#sc-nosite').checked, then_pipeline: $('#sc-then').checked };
}
$('#sc-go').addEventListener('click', () => run(async () => {
  const r = await api('/api/scrape', { method: 'POST', body: scrapeBody() });
  toast(`Scrape started${r.searches > 1 ? ` (${r.searches} searches)` : ''} — progress appears under Scrape runs.`, 'ok'); refreshJobs();
}));
$('#sc-schedule').addEventListener('click', () => run(async () => {
  await api('/api/schedules', { method: 'POST', body: { ...scrapeBody(), interval_hours: +$('#sc-hours').value || 24 } });
  toast('Scheduled.', 'ok'); loadFind();
}));
$('#schedule-list').addEventListener('click', (e) => run(async () => {
  const t = e.target.closest('button'); if (!t) return;
  if (t.dataset.schedDel) await api(`/api/schedules/${t.dataset.schedDel}`, { method: 'DELETE' });
  else if (t.dataset.schedToggle) await api(`/api/schedules/${t.dataset.schedToggle}`, { method: 'PATCH', body: { active: t.dataset.active !== '1' } });
  else return;
  loadFind();
}));

$('#imp-go').addEventListener('click', () => run(async () => {
  const f = $('#imp-file').files[0];
  if (!f) throw new Error('Choose a CSV or Excel file first');
  const fd = new FormData();
  fd.append('file', f); fd.append('campaign', $('#imp-campaign').value.trim()); fd.append('source', $('#imp-source').value.trim() || 'import');
  const box = $('#imp-result');
  try {
    const r = await api('/api/import', { method: 'POST', body: fd });
    const map = Object.entries(r.column_mapping || {}).map(([k, v]) => `${k} → ${v}`).join(', ');
    box.className = 'result';
    box.innerHTML = `<b>${r.inserted} new</b>, ${r.merged} already known (merged)` +
      (r.skipped_empty ? `, ${r.skipped_empty} empty rows skipped` : '') +
      (r.skipped_suppressed ? `, ${r.skipped_suppressed} on do-not-contact skipped` : '') +
      `<div class="sub">Matched: ${esc(map || 'nothing')}${r.unmapped_columns.length ? ' · Not matched (kept in notes if short): ' + esc(r.unmapped_columns.join(', ')) : ''}</div>`;
    if (r.missing_name) {
      box.innerHTML += `<div class="sub bad-text"><b>${r.missing_name} of these have no business name.</b> No column looked like a name &mdash; rename that column to "Business Name" in the file and import it again (existing leads are updated, not duplicated).</div>`;
    }
    box.hidden = false;
    $('#imp-file').value = '';
    loadCampaigns();
    toast(`Import finished: ${r.inserted} new, ${r.merged} already known` + (r.missing_name ? ` (${r.missing_name} without a name)` : '') + '.', r.missing_name ? 'err' : 'ok');
  } catch (e) { box.className = 'result err'; box.textContent = e.message; box.hidden = false; toast('Import failed: ' + e.message, 'err'); }
}));

/* ------------------------------------------------------------ suggestions */
// A dropdown of this account's own past searches / locations / lead names, shown after
// 3+ characters. One shared list element, positioned under whichever input is active.
const suggestBox = document.createElement('ul');
suggestBox.className = 'suggest'; suggestBox.id = 'suggest-box'; suggestBox.hidden = true;
suggestBox.setAttribute('role', 'listbox');
document.body.appendChild(suggestBox);
const sg = { input: null, items: [], active: -1, timer: null, seq: 0 };

function hideSuggest() {
  suggestBox.hidden = true; sg.items = []; sg.active = -1;
  if (sg.input) sg.input.setAttribute('aria-expanded', 'false');
}
function placeSuggest() {
  if (!sg.input || suggestBox.hidden) return;
  const r = sg.input.getBoundingClientRect();
  suggestBox.style.left = `${r.left + window.scrollX}px`;
  suggestBox.style.top = `${r.bottom + window.scrollY + 2}px`;
  suggestBox.style.width = `${r.width}px`;
}
function paintSuggest() {
  suggestBox.innerHTML = sg.items.map((t, i) =>
    `<li role="option" id="suggest-${i}" data-i="${i}" class="${i === sg.active ? 'on' : ''}" aria-selected="${i === sg.active}">${esc(t)}</li>`).join('');
  if (sg.input) sg.input.setAttribute('aria-activedescendant', sg.active >= 0 ? `suggest-${sg.active}` : '');
}
function pickSuggest(i) {
  const input = sg.input, value = sg.items[i];
  if (!input || value == null) return;
  if (input.dataset.multi) {                                    // keep earlier entries, replace the one being typed
    const done = input.value.split(';').slice(0, -1).map((x) => x.trim()).filter(Boolean);
    input.value = done.concat(value).join('; ') + '; ';
  } else input.value = value;
  hideSuggest();
  sg.skipNext = true;                                           // don't re-suggest for our own event
  input.dispatchEvent(new Event('input', { bubbles: true }));   // so live filters re-run
  sg.skipNext = false;
}
function attachSuggest(sel, field) {
  const input = $(sel); if (!input) return;
  input.setAttribute('autocomplete', 'off'); input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-controls', 'suggest-box');
  input.setAttribute('aria-expanded', 'false');
  input.addEventListener('input', () => {
    if (sg.skipNext) { sg.skipNext = false; return; }           // the input event we fired ourselves
    clearTimeout(sg.timer);
    const q = (input.dataset.multi ? input.value.split(';').pop() : input.value).trim();
    if (q.length < 3) { hideSuggest(); return; }
    const seq = ++sg.seq;
    sg.timer = setTimeout(async () => {
      let r;
      try { r = await api(`/api/suggest?field=${field}&q=${encodeURIComponent(q)}` + (input.dataset.multi ? `&context=${encodeURIComponent(input.value)}` : '')); } catch (e) { return; }
      if (seq !== sg.seq || document.activeElement !== input) return;   // a newer keystroke won
      sg.input = input; sg.items = r.suggestions || []; sg.active = -1;
      if (!sg.items.length) { hideSuggest(); return; }
      suggestBox.hidden = false; input.setAttribute('aria-expanded', 'true');
      paintSuggest(); placeSuggest();
    }, 180);
  });
  input.addEventListener('keydown', (e) => {
    if (suggestBox.hidden || sg.input !== input) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = sg.items.length;
      sg.active = e.key === 'ArrowDown' ? (sg.active + 1) % n : (sg.active - 1 + n) % n;
      paintSuggest();
    } else if (e.key === 'Enter' && sg.active >= 0) { e.preventDefault(); pickSuggest(sg.active); }
    else if (e.key === 'Escape') { hideSuggest(); }
  });
  input.addEventListener('blur', () => setTimeout(() => { if (sg.input === input) hideSuggest(); }, 150));
}
suggestBox.addEventListener('mousedown', (e) => {               // mousedown: fires before the input's blur
  const li = e.target.closest('li'); if (!li) return;
  e.preventDefault(); pickSuggest(+li.dataset.i);
});
window.addEventListener('resize', placeSuggest);
window.addEventListener('scroll', placeSuggest, true);
$('#sc-query').dataset.multi = '1'; $('#sc-location').dataset.multi = '1';
attachSuggest('#sc-query', 'scrape_query');
attachSuggest('#sc-location', 'scrape_location');
// "3 searches x 4 places = 12 searches" under the form, so the size of a run is never a surprise
const splitMulti = (v) => [...new Set(v.split(/[;\n]+/).map((x) => x.trim().replace(/\s+/g, ' ')).filter(Boolean).map((x) => x.toLowerCase()))];
function updateScrapePlan() {
  const el = $('#sc-plan'); if (!el) return;
  const q = splitMulti($('#sc-query').value).length;
  const l = $('#sc-platform').value === 'instagram' ? 0 : splitMulti($('#sc-location').value).length;
  const n = q * Math.max(l, 1), max = +$('#sc-max').value || 30;
  if (n <= 1) { el.hidden = true; return; }
  el.hidden = false;
  el.className = 'hint' + (n > 40 ? ' bad-text' : '');
  el.textContent = n > 40
    ? `That's ${n} searches — the most one run can do is 40. Remove some, or split it into two runs.`
    : `${q} search term${q === 1 ? '' : 's'}${l ? ` × ${l} place${l === 1 ? '' : 's'}` : ''} = ${n} searches, run one after another, up to ${max} listings each (${n * max} listings checked at most).`;
}
['#sc-query', '#sc-location', '#sc-max', '#sc-platform'].forEach((s) => ['input', 'change'].forEach((ev) => $(s).addEventListener(ev, updateScrapePlan)));
attachSuggest('#f-q', 'lead_search');

/* ------------------------------------------------------------ leads table */
function filterValues() {
  const f = { q: $('#f-q').value.trim(), campaign: $('#f-campaign').value, stage: $('#f-stage').value,
    fit: $('#f-fit').value, channel: $('#f-channel').value, has: $('#f-has').value, sent: $('#f-sent').value };
  Object.keys(f).forEach((k) => { if (f[k] === '') delete f[k]; });
  return f;
}
function initLeadFilters() {
  $('#f-stage').innerHTML = opt('', 'Any stage') + STAGES.map((s) => opt(s, s)).join('');
  $('#f-channel').innerHTML = opt('', 'Any channel') + CHANNELS.map((c) => opt(c, CHANNEL_LABEL[c])).join('');
  $('#bulk-stage').innerHTML = opt('', 'Set stage…') + STAGES.map((s) => opt(s, s)).join('');
}

async function loadLeads() {
  await loadCampaigns();
  await loadSettings().catch(() => {});
  const L = state.leads;
  if (state.pendingFilter) {
    const p = state.pendingFilter; state.pendingFilter = null;
    ['q', 'campaign', 'stage', 'fit', 'channel', 'has', 'sent'].forEach((k) => { $(`#f-${k}`).value = p[k] !== undefined ? p[k] : ''; });
    L.page = 0; L.selected.clear(); L.allMatching = false;
  }
  const p = new URLSearchParams({ ...filterValues(), sort: $('#f-sort').value, limit: L.size, offset: L.page * L.size });
  const d = await api('/api/leads?' + p);
  L.rows = d.leads; L.total = d.total;
  const maxPage = Math.max(0, Math.ceil(L.total / L.size) - 1);
  if (L.page > maxPage) { L.page = maxPage; return loadLeads(); }
  renderLeads();
}

function reachChips(l) {
  const c = [];
  if (l.email) c.push(['email', l.email]); if (l.phone || l.phone_raw) c.push(['phone', l.phone || l.phone_raw]);
  if (l.has_website) c.push(['site', l.website]); if (l.facebook_url) c.push(['fb', 'Facebook page']);
  if (l.instagram_url) c.push(['ig', 'Instagram profile']); if (l.yelp_url) c.push(['yelp', 'Yelp page']);
  return c.length ? `<div class="reach">${c.map(([x, t]) => `<span class="chip" title="${esc(t)}">${x}</span>`).join('')}</div>` : '<span class="muted dash">—</span>';
}
function fitChip(l) {
  if (l.fit === 1) return `<span class="chip ok" title="${esc(l.fit_reason)}">Fits</span>`;
  if (l.fit === 0) return `<span class="chip bad" title="${esc(l.fit_reason)}">Rejected</span>`;
  return '<span class="muted dash">—</span>';
}
function sentChip(l) {
  if (l.send_status === 'sent') return `<span class="chip info">Sent${l.follow_up_count ? ' +' + l.follow_up_count : ''}</span>`;
  if (l.send_status === 'failed') return '<span class="chip bad" title="The email could not be delivered">Failed</span>';
  return l.message ? '<span class="chip warn">Drafted</span>' : '<span class="muted dash">—</span>';
}

function renderLeads() {
  const L = state.leads;
  $('#leads-body').innerHTML = L.rows.map((l) => {
    const place = [l.city, l.state].filter(Boolean).join(', ');
    const sub = [TRADE_LABEL[l.trade] && l.trade ? TRADE_LABEL[l.trade] : '', place].filter(Boolean).join(' · ');
    return `<tr data-id="${l.id}" class="${L.selected.has(l.id) ? 'selected' : ''}">
      <td class="chk"><input type="checkbox" data-sel="${l.id}" ${L.selected.has(l.id) ? 'checked' : ''} aria-label="Select ${esc(l.business_name)}"></td>
      <td class="c-biz"><div class="bizname">${esc(l.business_name || '(no name)')}${l.do_not_contact ? ' <span class="chip bad">do not contact</span>' : ''}</div><div class="sub">${esc(sub)}</div></td>
      <td data-l="Reach">${reachChips(l)}</td>
      <td class="num" data-l="Score" title="${esc(l.score_reasons)}">${l.score}</td>
      <td data-l="Fit">${fitChip(l)}</td>
      <td class="nowrap" data-l="Channel">${esc(CHANNEL_LABEL[l.channel] || l.channel)}${l.channel_locked ? ' <span class="muted" title="Chosen by hand">•</span>' : ''}</td>
      <td data-l="Stage"><span class="chip stage-${l.stage}">${l.stage}</span></td>
      <td data-l="Outreach">${sentChip(l)}</td></tr>`;
  }).join('');
  const empty = $('#leads-empty');
  empty.hidden = L.rows.length > 0;
  if (!L.rows.length) {
    const filtered = Object.keys(filterValues()).length > 0;
    empty.innerHTML = filtered ? '<b>No leads match these filters</b>Try clearing a filter.'
      : '<b>No leads yet</b>Import a CSV or run a scrape from <a href="#find">Find leads</a>.';
  }
  const from = L.total ? L.page * L.size + 1 : 0, to = Math.min(L.total, (L.page + 1) * L.size);
  $('#pg-info').textContent = `${from}–${to} of ${L.total}`;
  $('#pg-prev').disabled = L.page === 0;
  $('#pg-next').disabled = to >= L.total;
  $('#ld-export').href = '/api/export.csv?' + new URLSearchParams(filterValues());
  $('#sel-page').checked = L.rows.length > 0 && L.rows.every((l) => L.selected.has(l.id));
  updateBulkBar();
}

function updateBulkBar() {
  const L = state.leads, n = L.allMatching ? L.total : L.selected.size;
  $('#bulkbar').hidden = n === 0;
  $('#bulk-count').textContent = `${n} selected`;
  const allBtn = $('#bulk-all');
  const pageAll = L.rows.length > 0 && L.rows.every((l) => L.selected.has(l.id));
  if (L.allMatching) { allBtn.hidden = false; allBtn.textContent = 'Clear selection'; }
  else if (pageAll && L.total > L.rows.length) { allBtn.hidden = false; allBtn.textContent = `Select all ${L.total} matching`; }
  else allBtn.hidden = true;
}

const reloadLeads = debounce(() => { state.leads.page = 0; state.leads.selected.clear(); state.leads.allMatching = false; run(loadLeads); }, 250);
['#f-q'].forEach((s) => $(s).addEventListener('input', reloadLeads));
['#f-campaign', '#f-stage', '#f-fit', '#f-channel', '#f-has', '#f-sent', '#f-sort'].forEach((s) => $(s).addEventListener('change', reloadLeads));
$('#pg-prev').addEventListener('click', () => { state.leads.page--; run(loadLeads); });
$('#pg-next').addEventListener('click', () => { state.leads.page++; run(loadLeads); });

$('#leads-body').addEventListener('click', (e) => {
  const cb = e.target.closest('[data-sel]');
  if (cb) {
    const id = +cb.dataset.sel;
    cb.checked ? state.leads.selected.add(id) : state.leads.selected.delete(id);
    state.leads.allMatching = false;
    cb.closest('tr').classList.toggle('selected', cb.checked);
    $('#sel-page').checked = state.leads.rows.every((l) => state.leads.selected.has(l.id));
    updateBulkBar();
    return;
  }
  const tr = e.target.closest('tr'); if (tr) run(() => openLead(+tr.dataset.id));
});
$('#sel-page').addEventListener('change', (e) => {
  const L = state.leads;
  L.rows.forEach((l) => (e.target.checked ? L.selected.add(l.id) : L.selected.delete(l.id)));
  L.allMatching = false; renderLeads();
});
$('#bulk-all').addEventListener('click', () => {
  const L = state.leads;
  if (L.allMatching) { L.allMatching = false; L.selected.clear(); } else L.allMatching = true;
  renderLeads();
});

function bulkPayload() {
  const L = state.leads;
  return L.allMatching ? { filters: filterValues() } : { ids: [...L.selected] };
}
function afterBulk() { state.leads.selected.clear(); state.leads.allMatching = false; run(loadLeads); }

$('#bulkbar').addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('[data-bulk]'); if (!b) return;
  const n = state.leads.allMatching ? state.leads.total : state.leads.selected.size;
  const kind = b.dataset.bulk;
  if (kind === 'pipeline') { await api('/api/pipeline', { method: 'POST', body: bulkPayload() }); toast(`Enriching, judging and drafting ${plural(n, 'lead')} — see Recent runs on the Dashboard.`, 'ok'); refreshJobs(); }
  else if (kind === 'enrich') { await api('/api/enrich', { method: 'POST', body: { ...bulkPayload(), force: true } }); toast('Enrichment started.', 'ok'); refreshJobs(); }
  else if (kind === 'judge') { await api('/api/judge', { method: 'POST', body: { ...bulkPayload(), force: true } }); toast('Judging started.', 'ok'); refreshJobs(); }
  else if (kind === 'draft') { await api('/api/draft', { method: 'POST', body: { ...bulkPayload(), template: '' } }); toast('Drafting started (leads that already have a message are skipped).', 'ok'); refreshJobs(); }
  else if (kind === 'campaign') {
    const name = prompt(`Move ${n} leads to which campaign? (leave empty to remove the campaign)`);
    if (name === null) return;
    await api('/api/leads/bulk', { method: 'POST', body: { ...bulkPayload(), action: 'set_campaign', value: name.trim() } });
    toast('Campaign updated.', 'ok'); loadCampaigns(); afterBulk();
  } else if (kind === 'dnc') {
    if (!confirm(`Mark ${n} leads as do-not-contact? Nothing will be sent to them.`)) return;
    await api('/api/leads/bulk', { method: 'POST', body: { ...bulkPayload(), action: 'set_dnc', value: true } });
    toast('Marked do-not-contact.', 'ok'); afterBulk();
  } else if (kind === 'delete') {
    if (!confirm(`Permanently delete ${n} leads? This can't be undone.`)) return;
    await api('/api/leads/bulk', { method: 'POST', body: { ...bulkPayload(), action: 'delete' } });
    toast('Deleted.', 'ok'); loadCampaigns(); afterBulk();
  }
}));
$('#bulk-stage').addEventListener('change', (e) => run(async () => {
  if (!e.target.value) return;
  await api('/api/leads/bulk', { method: 'POST', body: { ...bulkPayload(), action: 'set_stage', value: e.target.value } });
  e.target.value = ''; toast('Stage updated.', 'ok'); afterBulk();
}));
$('#ld-add').addEventListener('click', () => openNewLead());

/* ------------------------------------------------------------ campaigns */
function renderRailCampaigns() {
  const list = state.campaigns.filter((c) => c.status !== 'archived').slice(0, 12);
  $('#rail-campaigns').innerHTML = list.map((c) =>
    `<a href="${campaignHash(c.name)}" class="${state.view === 'campaign' && state.campaign === c.name ? 'active' : ''}">${esc(c.name)}</a>`).join('');
}

async function loadCampaignsView() {
  await loadCampaigns();
  const showArchived = $('#cp-show-archived').checked;
  const list = state.campaigns.filter((c) => showArchived || c.status !== 'archived');
  $('#cp-archived-wrap').hidden = !state.campaigns.some((c) => c.status === 'archived');
  $('#cp-cards').innerHTML = list.length ? list.map((c) =>
    `<button class="camp ${c.status === 'archived' ? 'archived' : ''}" data-camp="${esc(c.name)}">
       <h3>${esc(c.name)} ${c.status === 'archived' ? '<span class="chip">Archived</span>' : ''}${c.has_overrides ? ' <span class="chip info" title="Has its own templates, channel order or about-you text">custom</span>' : ''}</h3>
       <div class="desc">${esc(c.description || 'No description')}</div>
       <div class="mini"><div><b>${c.leads}</b>leads</div><div><b>${c.contacted}</b>contacted</div>
         <div><b>${c.replied}</b>replied</div><div><b>${c.won}</b>won</div></div></button>`).join('')
    : '<div class="card empty"><b>No campaigns yet</b>Create your first one — or import a CSV with a campaign name and it appears here automatically.</div>';
  $('#cp-unassigned').innerHTML = state.unassigned
    ? `${state.unassigned} lead${state.unassigned === 1 ? ' isn\'t' : 's aren\'t'} in any campaign. <button class="linklike" id="cp-show-unassigned">View them</button>` : '';
  $('#cp-copy').innerHTML = opt('', 'Blank campaign') + state.campaigns.map((c) => opt(c.name, `Copy settings from ${c.name}`)).join('');
}
$('#cp-show-archived').addEventListener('change', () => run(loadCampaignsView));
$('#cp-new-btn').addEventListener('click', () => { $('#cp-new').hidden = false; $('#cp-name').focus(); });
$('#cp-cancel').addEventListener('click', () => { $('#cp-new').hidden = true; });
$('#cp-create').addEventListener('click', () => run(async () => {
  const r = await api('/api/campaigns', { method: 'POST', body: {
    name: $('#cp-name').value, description: $('#cp-desc').value, copy_from: $('#cp-copy').value } });
  $('#cp-name').value = ''; $('#cp-desc').value = ''; $('#cp-new').hidden = true;
  toast('Campaign created.', 'ok');
  location.hash = campaignHash(r.campaign.name);
}));
$('#cp-cards').addEventListener('click', (e) => {
  const c = e.target.closest('[data-camp]'); if (c) location.hash = campaignHash(c.dataset.camp);
});
$('#view-campaigns').addEventListener('click', (e) => {
  if (e.target.id === 'cp-show-unassigned') { state.pendingFilter = { campaign: '__none__' }; location.hash = '#leads'; }
});

/* ----- campaign dashboard */
function funnelHtml(funnel) {
  const top = Math.max(1, funnel[0].count);
  return funnel.map((s, i) => {
    const prev = i ? funnel[i - 1].count : null;
    const conv = prev ? Math.round((s.count / prev) * 100) : null;
    return `<div class="funnel-row"><span>${esc(s.label)}</span><span class="track"><i style="width:${(s.count / top) * 100}%"></i></span>
      <span class="n">${s.count}${conv !== null ? ` <small>${conv}%</small>` : ''}</span></div>`;
  }).join('');
}
function chartHtml(series) {
  const max = Math.max(1, ...series.map((d) => Math.max(d.sent, d.replies)));
  const H = 100;
  return `<div class="chart">${series.map((d) =>
    `<div class="day" title="${d.date}: ${d.sent} sent, ${d.replies} replies"><div class="bars">
       <i class="s" style="height:${(d.sent / max) * H}px"></i><i class="r" style="height:${(d.replies / max) * H}px"></i></div>
       <span class="lbl">${d.date.slice(8)}</span></div>`).join('')}</div>
     <div class="legend"><span><i style="background:var(--accent)"></i>Messages sent</span><span><i style="background:var(--ok)"></i>Replies</span></div>`;
}
function breakdownHtml(title, list, label = (x) => x) {
  if (!list.length) return '';
  const max = Math.max(...list.map((x) => x.count));
  return `<div class="brk-title">${esc(title)}</div>` + list.map((x) =>
    `<div class="brk"><span>${esc(label(x.name))}</span><span class="track"><i style="width:${(x.count / max) * 100}%"></i></span><span class="num">${x.count}</span></div>`).join('');
}
function eventLine(e) {
  let d = {}; try { d = JSON.parse(e.detail); } catch (x) { d = {}; }
  if (typeof d !== 'object' || d === null) d = {};
  const who = `<a href="#" data-open="${e.lead_id}">${esc(e.business_name || 'lead')}</a>`;
  switch (e.kind) {
    case 'email_sent': return `Emailed ${who}${d.subject ? ` — “${esc(d.subject)}”` : ''}`;
    case 'followup_sent': return `Followed up with ${who}`;
    case 'manual_sent': return `Sent by hand to ${who} (${esc(e.detail)})`;
    case 'reply': return `${who} replied${d.snippet ? ` — “${esc(String(d.snippet).slice(0, 90))}”` : ''}`;
    case 'optout': return `${who} asked not to be contacted`;
    case 'stage': return `${who}: ${esc(e.detail)}`;
    case 'enriched': return `Found ${esc(e.detail)} for ${who}`;
    default: return `${who}: ${esc(e.kind)}`;
  }
}

async function loadCampaignView(name) {
  if (!state.cd || state.cd.campaign.name !== name) {      // switching campaigns: don't show the old one's numbers
    ['#cd-tiles', '#cd-funnel', '#cd-queue', '#cd-chart', '#cd-breakdown', '#cd-recent'].forEach((s) => { $(s).innerHTML = ''; });
    $('#cd-title').textContent = name; $('#cd-desc').textContent = '';
  }
  await Promise.all([loadCampaigns(), loadConfig()]);
  let d;
  try { d = await api('/api/campaigns/' + encodeURIComponent(name)); }
  catch (e) { toast(e.message, 'err'); location.hash = '#campaigns'; return; }
  state.cd = d; state.campaign = d.campaign.name;
  renderRailCampaigns();
  const c = d.campaign, f = Object.fromEntries(d.funnel.map((x) => [x.key, x.count]));
  $('#cd-title').innerHTML = `${esc(c.name)} ${c.status === 'archived' ? '<span class="chip">Archived</span>' : ''}`;
  $('#cd-desc').textContent = c.description || '';
  $('#cd-archive').textContent = c.status === 'archived' ? 'Unarchive' : 'Archive';

  const pct = (v) => (v === null || v === undefined ? '—' : `${v}%`);
  const last14 = (k) => d.series.reduce((a, x) => a + x[k], 0);
  $('#cd-tiles').innerHTML = [
    ['Leads', f.leads], ['Contacted', f.contacted], ['Reply rate', pct(d.rates.reply_rate)],
    ['Win rate', pct(d.rates.win_rate)], ['Judged as fit', pct(d.rates.fit_rate)], ['Sent, last 14 days', last14('sent')],
  ].map(([l, n]) => `<div class="tile"><div class="n">${n}</div><div class="l">${esc(l)}</div></div>`).join('');

  $('#cd-funnel').innerHTML = f.leads ? funnelHtml(d.funnel)
    + `<div class="hint" style="margin-top:8px">Enriched ${d.progress.enriched} · judged ${d.progress.judged} · messages drafted ${d.progress.drafted} (of ${d.progress.total})</div>`
    : '<div class="empty"><b>No leads in this campaign yet</b>Use <b>Add leads</b> above to import a CSV or start a scrape into it.</div>';

  const q = d.queue;
  q.replies = (d.replies || []).length;
  const qrows = [
    ['replies', 'replied and need an answer', 'Answer'],
    ['needs_enrich', 'need contact details found', 'Enrich'], ['needs_judge', "haven't been judged", 'Judge fit'],
    ['needs_draft', 'need a message', 'Write messages'], ['ready_email', 'emails ready to send', 'Send emails'],
    ['hand_queue', 'to send by hand', 'Open queue'], ['followups_due', 'email follow-ups due', 'Follow up'],
    ['hand_followups_due', 'follow-ups due by hand', 'Follow up'],
  ];
  $('#cd-pipeline').disabled = !(q.needs_enrich || q.needs_judge || q.needs_draft);
  $('#cd-replies-card').hidden = !q.replies;
  $('#cd-replies').innerHTML = q.replies ? repliesHtml(d.replies, '') : '';
  $('#cd-queue').innerHTML = qrows.map(([k, text, label]) =>
    `<div class="qrow"><span><span class="count">${q[k]}</span>${esc(text)}</span>` +
    (q[k] ? `<button class="btn small" data-q="${k}">${esc(label)}</button>` : '<span class="done">All done</span>') + '</div>').join('');

  $('#cd-chart').innerHTML = chartHtml(d.series);
  $('#cd-breakdown').innerHTML = f.leads
    ? breakdownHtml('How to reach them', d.by_channel, (n) => CHANNEL_LABEL[n] || n)
      + breakdownHtml('Trade', d.by_trade, (n) => TRADE_LABEL[n] || n) + breakdownHtml('City', d.by_city)
      + breakdownHtml('Where they came from', d.by_source) : '<div class="hint">Nothing yet.</div>';
  $('#cd-recent').innerHTML = d.recent.length ? d.recent.map((e) =>
    `<div class="ev-row"><span class="muted">${ago(e.created_at)}</span><span>${eventLine(e)}</span></div>`).join('')
    : '<div class="hint">Nothing has happened in this campaign yet.</div>';
  renderJobs();
  fillCampaignSettings(d);
}

function fillCampaignSettings(d) {
  const c = d.campaign, saved = (c.criteria || '').trim(), defaultSaved = (d.saved_default_criteria || '').trim();
  $('#cs-desc').value = c.description || '';
  const shown = saved || defaultSaved || d.sample_criteria;
  $('#cs-criteria').value = shown;
  state.cd.criteriaShown = shown; state.cd.criteriaWasSaved = !!saved;
  $('#cs-criteria-note').textContent = saved ? 'This campaign has its own criteria.'
    : defaultSaved ? 'Showing your account-wide default — edit it and save to give this campaign its own.'
      : 'Showing the built-in sample — edit it and save to use your own.';
  $('#cs-info').value = c.business_info || '';
  const prioOn = !!(c.channel_priority && c.channel_priority.length);
  $('#cs-prio-on').checked = prioOn; state.csPrio.set(prioOn ? c.channel_priority : d.global.channel_priority); $('#cs-prio').hidden = !prioOn;
  const tplOn = !!(c.templates && c.templates.length);
  $('#cs-tpl-on').checked = tplOn; state.csTpl.set(tplOn ? c.templates : d.global.templates);
  $('#cs-tpl').hidden = !tplOn; $('#cs-tpl-add').hidden = !tplOn; $('#cs-tpl-help').hidden = !tplOn;
  $('#cs-tpl-help').innerHTML = placeholderHelp();
}
$('#cs-prio-on').addEventListener('change', (e) => { $('#cs-prio').hidden = !e.target.checked; });
$('#cs-tpl-on').addEventListener('change', (e) => { ['#cs-tpl', '#cs-tpl-add', '#cs-tpl-help'].forEach((x) => { $(x).hidden = !e.target.checked; }); });
$('#cs-tpl-add').addEventListener('click', () => state.csTpl.add());
$('#cs-save').addEventListener('click', () => run(async () => {
  const d = state.cd, val = $('#cs-criteria').value.trim();
  const unchanged = !d.criteriaWasSaved && val === d.criteriaShown.trim();   // untouched default -> keep inheriting
  await api(`/api/campaigns/${encodeURIComponent(d.campaign.name)}`, { method: 'PUT', body: {
    description: $('#cs-desc').value, criteria: unchanged ? '' : val, business_info: $('#cs-info').value,
    channel_priority: $('#cs-prio-on').checked ? state.csPrio.get() : [],
    templates: $('#cs-tpl-on').checked ? state.csTpl.get() : [],
  } });
  toast('Campaign settings saved.', 'ok');
  loadCampaignView(d.campaign.name);
}));

$('#cd-leads').addEventListener('click', () => { state.pendingFilter = { campaign: state.campaign }; location.hash = '#leads'; });
$('#cd-import').addEventListener('click', () => { state.prefillCampaign = state.campaign; location.hash = '#find'; });
$('#cd-archive').addEventListener('click', () => run(async () => {
  const c = state.cd.campaign, next = c.status === 'archived' ? 'active' : 'archived';
  await api(`/api/campaigns/${encodeURIComponent(c.name)}`, { method: 'PUT', body: { status: next } });
  toast(next === 'archived' ? 'Archived — hidden from the pickers, nothing deleted.' : 'Restored.', 'ok');
  loadCampaignView(c.name);
}));
$('#cd-delete').addEventListener('click', () => run(async () => {
  const c = state.cd.campaign, n = state.cd.funnel[0].count;
  if (!confirm(`Delete the campaign "${c.name}"?\n\nIts ${n} lead${n === 1 ? '' : 's'} will stay in All leads, just no longer in a campaign.`)) return;
  const alsoLeads = n > 0 && confirm(`Also permanently delete its ${n} lead${n === 1 ? '' : 's'}?\n\nOK = delete the leads too.  Cancel = keep the leads.`);
  await api(`/api/campaigns/${encodeURIComponent(c.name)}${alsoLeads ? '?delete_leads=1' : ''}`, { method: 'DELETE' });
  toast('Campaign deleted.', 'ok'); location.hash = '#campaigns';
}));
$('#cd-queue').addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('[data-q]'); if (!b) return;
  const camp = state.campaign, k = b.dataset.q;
  if (k === 'needs_enrich') await api('/api/enrich', { method: 'POST', body: { campaign: camp } });
  else if (k === 'needs_judge') await api('/api/judge', { method: 'POST', body: { campaign: camp } });
  else if (k === 'needs_draft') await api('/api/draft', { method: 'POST', body: { campaign: camp, template: '' } });
  else if (k === 'replies') { state.pendingFilter = { campaign: camp, stage: 'replied' }; location.hash = '#leads'; return; }
  else if (k === 'ready_email') {
    if (!confirm(`Send the drafted emails in "${camp}", paced by your daily limit and delay?`)) return;
    await api('/api/send-bulk', { method: 'POST', body: { campaign: camp } });
  } else { state.pendingOutreachCampaign = camp; location.hash = '#outreach'; return; }
  toast('Started — progress appears under Recent runs.', 'ok'); refreshJobs();
}));
$('#view-campaign').addEventListener('click', (e) => {
  const a = e.target.closest('[data-open]'); if (a) { e.preventDefault(); run(() => openLead(+a.dataset.open)); }
});
$('#cd-pipeline').addEventListener('click', () => run(async () => {
  await api('/api/pipeline', { method: 'POST', body: { campaign: state.campaign } });
  toast('Enriching, judging and drafting — progress appears under Recent runs.', 'ok'); refreshJobs();
}));

/* ------------------------------------------------------------- pipeline */
async function loadPipeline() {
  await Promise.all([loadCampaigns(), loadConfig()]);
  const u = await api('/api/usage');
  $('#en-usage').textContent = `Web searches this month: ${u.serpapi_used} of ${u.serpapi_limit}` + (state.config.serpapi ? '' : ' (no SerpAPI key — free website scan only)');
  const sel = $('#cr-campaign'), cur = sel.value || 'default';
  const names = ['default', ...state.campaigns.map((c) => c.name)];
  sel.innerHTML = names.map((n) => opt(n, n === 'default' ? 'default (all campaigns)' : n, n === cur)).join('');
  loadCriteria();
}
function loadCriteria() {
  const name = $('#cr-campaign').value || 'default';
  let saved = '';
  if (name === 'default') saved = (state.savedDefaultCriteria || '').trim();
  else { const c = state.campaigns.find((x) => x.name === name); saved = c ? (c.criteria || '').trim() : ''; }
  $('#cr-text').value = saved || state.defaultCriteria;
  $('#cr-status').textContent = saved ? 'Saved criteria.' : 'Showing the built-in sample. Edit it, then Save to use your version.';
}
$('#cr-campaign').addEventListener('change', loadCriteria);
$('#cr-sample').addEventListener('click', () => { $('#cr-text').value = state.defaultCriteria; });
$('#cr-save').addEventListener('click', () => run(async () => {
  const typed = $('#cr-new').value.trim();
  const name = typed || $('#cr-campaign').value || 'default';
  if (typed && !state.campaigns.some((c) => c.name.toLowerCase() === typed.toLowerCase())) {
    await api('/api/campaigns', { method: 'POST', body: { name: typed } });
  }
  await api(`/api/campaigns/${encodeURIComponent(name)}`, { method: 'PUT', body: { criteria: $('#cr-text').value } });
  $('#cr-new').value = '';
  toast(`Criteria saved for "${name}".`, 'ok');
  await loadPipeline(); $('#cr-campaign').value = name; loadCriteria();
}));
$('#pl-go').addEventListener('click', () => run(async () => {
  await api('/api/pipeline', { method: 'POST', body: { campaign: $('#pl-campaign').value } });
  toast('Started — enrich, then judge, then draft.', 'ok'); refreshJobs();
}));
$('#en-go').addEventListener('click', () => run(async () => {
  await api('/api/enrich', { method: 'POST', body: { campaign: $('#en-campaign').value, force: $('#en-force').checked, limit: $('#en-limit').value || null } });
  toast('Enrichment started.', 'ok'); refreshJobs();
}));
$('#ju-go').addEventListener('click', () => run(async () => {
  await api('/api/judge', { method: 'POST', body: { campaign: $('#ju-campaign').value, force: $('#ju-force').checked } });
  toast('Judging started.', 'ok'); refreshJobs();
}));

/* ------------------------------------------------------------- outreach */
function channelUrl(l, msg) {
  const digits = (l.phone || '').replace(/\D/g, '');
  switch (l.channel) {
    case 'yelp': return l.yelp_url;
    case 'facebook': return l.facebook_url;
    case 'instagram': return l.instagram_url;
    case 'whatsapp': return digits ? `https://wa.me/${digits}?text=${encodeURIComponent(msg)}` : '';
    case 'phone': return (l.phone || l.phone_raw) ? `sms:${l.phone || l.phone_raw}` : '';
    case 'email': {
      const m = msg.match(/^\s*subject:\s*(.*)\n+([\s\S]*)$/i);
      return `mailto:${l.email}?subject=${encodeURIComponent(m ? m[1] : '')}&body=${encodeURIComponent(m ? m[2] : msg)}`;
    }
    default: return '';
  }
}
async function copyAndOpen(l, msgOverride) {
  const raw = msgOverride ?? l.message ?? '';
  if (!raw.trim()) throw new Error('This lead has no message yet — generate one first.');
  // The server fills {name}-style placeholders with this lead's details, and refuses
  // (with a message saying what's missing) rather than hand back a half-filled message.
  const msg = (await api(`/api/leads/${l.id}/final-message`, { method: 'POST', body: { message: raw } })).message;
  try { await navigator.clipboard.writeText(msg); toast('Message copied — paste it in.', 'ok'); }
  catch (e) { toast('Could not copy automatically — select the text and copy it yourself.', 'err'); }
  const url = channelUrl(l, msg);
  if (url) window.open(url, url.startsWith('mailto:') || url.startsWith('sms:') ? '_self' : '_blank', 'noopener');
  else toast('No link for this channel — add the profile URL or phone number first.', 'err');
}

async function loadOutreach() {
  await Promise.all([loadCampaigns(), loadSettings(), loadConfig()]);
  if (state.pendingOutreachCampaign !== undefined) {
    $('#dr-campaign').value = state.pendingOutreachCampaign; delete state.pendingOutreachCampaign;
  }
  const camp = $('#dr-campaign').value, q = camp ? `campaign=${encodeURIComponent(camp)}` : '';
  const eff = await api('/api/settings' + (q ? `?${q}` : ''));
  $('#dr-template').innerHTML = templateOptions($('#dr-template').value, eff.templates);
  if (state.exactTouched === undefined) $('#dr-exact').checked = !!eff.exact_templates;
  const ai = state.config.llm.draft.ready;
  const warn = [];
  if (!ai && !eff.templates.length) warn.push('To write messages you need either an AI key (Settings → Connections) or a template to fill in (Settings → Messaging).');
  else if (!ai) warn.push('No AI key is set, so messages can only be made from your templates exactly as written. Add a key under Settings → Connections to have each one rewritten for the lead.');
  if (!state.config.smtp) warn.push('Email sending is off until you add your email details under Settings → Connections.');
  $('#out-warn').hidden = !warn.length; $('#out-warn').innerHTML = warn.map(esc).join('<br>');
  if (!ai) { $('#dr-exact').checked = true; $('#dr-exact').disabled = true; } else $('#dr-exact').disabled = false;

  const handParams = { send_status: 'unsent', drafted: 'yes', dnc: 'no', not_rejected: '1', hand: '1', open_stage: '1', sort: 'score', limit: 200 };
  if (camp) handParams.campaign = camp;
  const amp = q ? `&${q}` : '';
  const [es, fu, hfu, hand, replies] = await Promise.all([
    api('/api/email-status'), api('/api/followups-due' + (q ? `?${q}` : '')), api('/api/followups-due?kind=hand' + amp),
    api('/api/leads?' + new URLSearchParams(handParams)), api('/api/replies' + (q ? `?${q}` : '')),
  ]);
  const checked = !es.imap_configured ? 'inbox not connected' : !es.reply_check_minutes ? 'only when you press Check'
    : `automatic, every ${es.reply_check_minutes} min` + (es.last_reply_check_at ? ` (last ${ago(es.last_reply_check_at)})` : '');
  $('#em-status').innerHTML =
    `<div class="kv"><span>Sent today (all campaigns)</span><b>${es.sent_today} of ${es.daily_limit}</b></div>` +
    (es.warmup_active ? `<div class="kv"><span>Warm-up</span><b>capped below your ${es.max_limit}/day limit for now</b></div>` : '') +
    (es.window_open ? '' : `<div class="kv"><span>Sending hours</span><b class="warn-text">closed right now</b></div>`) +
    `<div class="kv"><span>Reply checking</span><b>${esc(checked)}</b></div>` +
    (es.window_open ? '' : `<p class="hint" style="margin-top:8px">${esc(es.window_note)}</p>`);
  $('#em-send').disabled = !es.smtp_configured || !es.window_open;
  $('#em-replies').disabled = !es.imap_configured;

  badge('#rp-n', replies.length);
  $('#rp-list').innerHTML = repliesHtml(replies, es.imap_configured
    ? 'Replies to your emails appear here on their own. For WhatsApp, Instagram and the rest, open the lead and set its stage to “replied”.'
    : 'Connect your inbox under Settings → Connections to have email replies picked up automatically.');

  badge('#fu-n', fu.length);
  $('#fu-all').hidden = !fu.length; $('#fu-all').disabled = !es.smtp_configured || !es.window_open;
  $('#fu-all').textContent = `Send all ${fu.length} due`;
  $('#fu-list').innerHTML = fu.length ? fu.map((l) =>
    `<div class="list-row"><div class="grow"><b>${esc(l.business_name || '(no name)')}</b> <span class="muted">${esc(l.email)}</span>
       <div class="sub">emailed ${ago(l.last_sent_at)} · ${plural(l.follow_up_count, 'follow-up')} sent</div></div>
     <button class="btn small" data-open="${l.id}">Open</button>
     <button class="btn small" data-followup="${l.id}">Send follow-up</button></div>`).join('')
    : '<div class="empty small"><b>No email follow-ups due</b>Leads that haven\'t replied show up here after your follow-up delay.</div>';

  const queue = hand.leads;
  badge('#hand-n', hand.total);
  $('#hand-list').innerHTML = queue.length ? queue.map((l) =>
    `<div class="list-row" data-row="${l.id}"><div class="grow"><b>${esc(l.business_name || '(no name)')}</b> <span class="chip">${esc(CHANNEL_LABEL[l.channel])}</span>
       <div class="sub">${esc((l.message || '').replace(/\s+/g, ' ').slice(0, 140))}${(l.message || '').length > 140 ? '…' : ''}</div></div>
     <button class="btn small" data-open="${l.id}">Open</button>
     <button class="btn small primary" data-copyopen="${l.id}">Copy &amp; open</button>
     <button class="btn small" data-marksent="${l.id}">Mark sent</button></div>`).join('')
    + (hand.total > queue.length ? `<p class="hint" style="margin-top:8px">Showing the first ${queue.length} of ${hand.total}.</p>` : '')
    : '<div class="empty small"><b>Nothing to send by hand</b>Write messages first; leads reachable only via Yelp, Facebook, Instagram, WhatsApp or phone appear here.</div>';

  badge('#hfu-n', hfu.length);
  $('#hfu-list').innerHTML = hfu.length ? hfu.map((l) =>
    `<div class="list-row" data-row="${l.id}"><div class="grow"><b>${esc(l.business_name || '(no name)')}</b> <span class="chip">${esc(CHANNEL_LABEL[l.channel])}</span>
       <div class="sub">messaged ${ago(l.last_sent_at)} · ${plural(l.follow_up_count, 'follow-up')} so far</div></div>
     <button class="btn small" data-open="${l.id}">Open</button>
     <button class="btn small primary" data-hfu="${l.id}">Copy follow-up &amp; open</button>
     <button class="btn small" data-hfudone="${l.id}">Mark followed up</button></div>`).join('')
    : '<div class="empty small"><b>No follow-ups due by hand</b>Leads you messaged who haven\'t replied show up here after your follow-up delay.</div>';
  state.handQueue = Object.fromEntries([...queue, ...hfu].map((l) => [l.id, l]));
}
$('#dr-campaign').addEventListener('change', () => run(loadOutreach));
$('#dr-exact').addEventListener('change', () => { state.exactTouched = true; });
$('#dr-go').addEventListener('click', () => run(async () => {
  await api('/api/draft', { method: 'POST', body: { campaign: $('#dr-campaign').value, template: $('#dr-template').value,
    force: $('#dr-force').checked, exact: $('#dr-exact').checked } });
  toast('Writing messages…', 'ok'); refreshJobs();
}));
$('#em-send').addEventListener('click', () => run(async () => {
  const camp = $('#dr-campaign').value;
  if (!confirm(`Start sending emails to every lead with a message${camp ? ` in "${camp}"` : ''}, paced by your delay and daily limit?`)) return;
  await api('/api/send-bulk', { method: 'POST', body: { campaign: camp, only_fit: $('#em-onlyfit').checked } });
  toast('Sending started.', 'ok'); refreshJobs();
}));
$('#em-replies').addEventListener('click', () => run(async () => {
  const b = $('#em-replies'); b.disabled = true; b.textContent = 'Checking…';
  try {
    const r = await api('/api/check-replies', { method: 'POST' });
    toast(`Looked at ${plural(r.checked, 'new message')}: ${plural(r.replied, 'reply', 'replies')}` + (r.suppressed ? `, ${r.suppressed} opted out` : '')
      + (r.bounced ? `, ${r.bounced} bounced` : '') + (r.auto_replies ? `, ${plural(r.auto_replies, 'auto-reply', 'auto-replies')} ignored` : ''), 'ok');
  } finally { b.textContent = 'Check for replies'; await loadOutreach(); }
}));
$('#fu-all').addEventListener('click', () => run(async () => {
  const camp = $('#dr-campaign').value;
  if (!confirm(`Send every follow-up that's due${camp ? ` in "${camp}"` : ''}? Your inbox is checked for replies first.`)) return;
  await api('/api/followups', { method: 'POST', body: { campaign: camp } });
  toast('Sending follow-ups.', 'ok'); refreshJobs();
}));
$('#view-outreach').addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.open) return openLead(+b.dataset.open);
  if (b.dataset.followup) { b.disabled = true; await api(`/api/leads/${b.dataset.followup}/followup`, { method: 'POST' }); toast('Follow-up sent.', 'ok'); return loadOutreach(); }
  if (b.dataset.copyopen) { await copyAndOpen(state.handQueue[b.dataset.copyopen]); b.closest('.list-row').classList.add('touched'); return; }
  if (b.dataset.marksent) { await api(`/api/leads/${b.dataset.marksent}/mark-sent`, { method: 'POST', body: {} }); toast('Marked as sent.', 'ok'); return loadOutreach(); }
  if (b.dataset.hfu) {
    const l = state.handQueue[b.dataset.hfu];
    b.disabled = true;
    try {
      const r = await api(`/api/leads/${l.id}/followup-text`, { method: 'POST', body: {} });
      await copyAndOpen(l, r.message); b.closest('.list-row').classList.add('touched');
    } finally { b.disabled = false; }
    return;
  }
  if (b.dataset.hfudone) { await api(`/api/leads/${b.dataset.hfudone}/mark-sent`, { method: 'POST', body: { followup: true } }); toast('Follow-up recorded.', 'ok'); return loadOutreach(); }
}));

/* ---------------------------------------------------------------- editors */
// Small reusable editors, used by Settings and by each campaign's own settings.
function TemplateEditor(root) {
  let list = [];
  const chans = CHANNELS.filter((c) => c !== 'unknown');
  function render() {
    root.innerHTML = list.length ? list.map((t, i) =>
      `<div class="tpl" data-i="${i}"><div class="row">
         <div class="field"><label>Name</label><input data-t="name" value="${esc(t.name)}"></div>
         <div class="field"><label>Channel</label><select data-t="channel">${chans.map((c) => opt(c, CHANNEL_LABEL[c], c === t.channel)).join('')}</select></div>
         <button class="btn small danger" data-t-del="${i}">Remove</button></div>
         <textarea data-t="body" rows="5" aria-label="Template text">${esc(t.body)}</textarea></div>`).join('')
      : '<div class="empty"><b>No templates yet</b>Without a template the AI writes a short opener from your "About you" text.</div>';
  }
  root.addEventListener('input', (e) => {
    const box = e.target.closest('.tpl'); if (!box || !e.target.dataset.t) return;
    list[+box.dataset.i][e.target.dataset.t] = e.target.value;
  });
  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-t-del]'); if (!b) return;
    list.splice(+b.dataset.tDel, 1); render();
  });
  return {
    set(v) { list = (v || []).map((t) => ({ ...t })); render(); },
    get() { return list.map((t) => ({ ...t })); },
    add() { list.push({ name: '', channel: 'email', body: '' }); render(); },
  };
}

function PriorityEditor(root) {
  let items = [];
  const all = CHANNELS.filter((c) => c !== 'unknown');
  function render() {
    root.innerHTML = items.map((p, i) =>
      `<div class="prio"><input type="checkbox" data-p-on="${i}" ${p.on ? 'checked' : ''} aria-label="Use ${esc(CHANNEL_LABEL[p.c])}">
         <span class="name">${esc(CHANNEL_LABEL[p.c])}</span>
         <button class="btn small" data-p-up="${i}" ${i === 0 ? 'disabled' : ''} aria-label="Move up">Up</button>
         <button class="btn small" data-p-down="${i}" ${i === items.length - 1 ? 'disabled' : ''} aria-label="Move down">Down</button></div>`).join('');
  }
  root.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    const swap = (i, j) => { [items[i], items[j]] = [items[j], items[i]]; render(); };
    if (b.dataset.pUp) swap(+b.dataset.pUp, +b.dataset.pUp - 1);
    if (b.dataset.pDown) swap(+b.dataset.pDown, +b.dataset.pDown + 1);
  });
  root.addEventListener('change', (e) => { if (e.target.dataset.pOn !== undefined) items[+e.target.dataset.pOn].on = e.target.checked; });
  return {
    set(order) {
      const o = (order || []).filter((c) => all.includes(c));
      items = [...o.map((c) => ({ c, on: true })), ...all.filter((c) => !o.includes(c)).map((c) => ({ c, on: false }))];
      render();
    },
    get() { return items.filter((p) => p.on).map((p) => p.c); },
  };
}
function placeholderHelp() {
  const list = ((state.config && state.config.placeholders) || '').split(/\s+/).filter(Boolean);
  return 'Blanks you can use: ' + list.map((p) => `<code>${esc(p)}</code>`).join(' ')
    + '. Add a fallback after a bar for when a lead has nothing there: <code>{first_name|there}</code>.';
}
state.tplEditor = TemplateEditor($('#st-templates'));
state.prioEditor = PriorityEditor($('#st-priority'));
state.csTpl = TemplateEditor($('#cs-tpl'));
state.csPrio = PriorityEditor($('#cs-prio'));
$('#st-tpl-add').addEventListener('click', () => state.tplEditor.add());

/* ------------------------------------------------------------- settings */
const CONN_GROUPS = [
  { title: 'AI (writing, judging, enrichment)', ready: (c) => c.llm.draft.ready, test: 'llm',
    help: 'Pick one provider and paste its key. Free tiers work (Groq or Google Gemini). The model boxes are optional.',
    fields: ['LLM_PROVIDER', null, 'GROQ_API_KEY', 'GROQ_MODEL', 'OPENAI_API_KEY', 'OPENAI_MODEL', 'GEMINI_API_KEY', 'GEMINI_MODEL'] },
  { title: 'Email sending', ready: (c) => c.smtp, test: 'smtp',
    readyLabel: (c) => (c.email_via === 'gmail_api' ? 'Ready · Gmail API' : c.email_via === 'smtp' ? 'Ready · SMTP' : 'Not set'),
    help: 'Your Gmail address, plus either a Gmail API refresh token (works on hosts that block email ports, like Render\'s free plan — see the README) '
      + 'or an App Password from myaccount.google.com/apppasswords (not your normal password). The App Password is also what reply checking uses.',
    fields: ['SMTP_USER', 'SMTP_FROM_NAME', 'GMAIL_REFRESH_TOKEN', 'SMTP_PASS', 'SMTP_HOST', 'SMTP_PORT'] },
  { title: 'Reply checking', ready: (c) => c.imap, test: 'imap', help: 'Leave these blank to reuse your email details above.',
    fields: ['IMAP_USER', 'IMAP_PASS', 'IMAP_HOST'] },
  { title: 'Phone numbers', ready: () => true, readyLabel: (c) => `+${c.country_code}`,
    help: 'The country calling code assumed for numbers written without one: 1 = US/Canada, 44 = UK, 91 = India, 61 = Australia. Numbers that start with + are always read as written.',
    fields: ['DEFAULT_COUNTRY_CODE'] },
  { title: 'Web search for enrichment', ready: (c) => c.serpapi, help: 'serpapi.com, free plan: 100 searches a month. Optional.',
    fields: ['SERPAPI_KEY', 'SERPAPI_MONTHLY_LIMIT'] },
  { title: 'Instagram scraping', ready: (c) => c.instagram_login, help: 'Use a burner account, never your real one. Optional.',
    fields: ['IG_USERNAME', 'IG_PASSWORD'] },
];
const CONN_LABEL = {
  LLM_PROVIDER: 'Which AI to use', GROQ_API_KEY: 'Groq API key', GROQ_MODEL: 'Groq model',
  OPENAI_API_KEY: 'OpenAI API key', OPENAI_MODEL: 'OpenAI model', GEMINI_API_KEY: 'Gemini API key',
  GEMINI_MODEL: 'Gemini model', SMTP_USER: 'Your email address', SMTP_PASS: 'App password',
  SMTP_FROM_NAME: 'Sender name', SMTP_HOST: 'Mail server (SMTP)', SMTP_PORT: 'Mail server port',
  IMAP_USER: 'Inbox login', IMAP_PASS: 'Inbox password', IMAP_HOST: 'Inbox server (IMAP)',
  SERPAPI_KEY: 'SerpAPI key', SERPAPI_MONTHLY_LIMIT: 'Searches per month', IG_USERNAME: 'Burner username', IG_PASSWORD: 'Burner password',
  DEFAULT_COUNTRY_CODE: 'Default country code', GMAIL_REFRESH_TOKEN: 'Gmail API refresh token',
};
const CONN_PLACEHOLDER = { SMTP_HOST: 'smtp.gmail.com', SMTP_PORT: '587', IMAP_HOST: 'imap.gmail.com', SERPAPI_MONTHLY_LIMIT: '100',
  GROQ_MODEL: 'default', OPENAI_MODEL: 'default', GEMINI_MODEL: 'default', DEFAULT_COUNTRY_CODE: '1', SMTP_FROM_NAME: 'e.g. Kunal from NeuroSpark' };

function renderConnections(fields) {
  const by = Object.fromEntries(fields.map((f) => [f.name, f]));
  state.connClear = new Set();
  $('#st-conn').innerHTML = CONN_GROUPS.map((g) => {
    const ok = g.ready(state.config);
    const inputs = g.fields.map((n) => {
      if (n === null) return '<div class="field spacer-cell"></div>';      // keeps pairs side by side
      const f = by[n]; if (!f) return '';
      let input;
      if (n === 'LLM_PROVIDER') {
        input = `<select data-conn="${n}" data-orig="${esc(f.value || '')}">${[['', 'Default (Groq)'], ['groq', 'Groq'], ['openai', 'OpenAI'], ['gemini', 'Gemini']]
          .map(([v, l]) => opt(v, l, v === (f.value || ''))).join('')}</select>`;
      } else if (f.secret) {
        input = `<input type="password" autocomplete="new-password" data-conn="${n}" placeholder="${f.set ? esc(f.hint || 'saved') + ' (saved)' : 'not set'}">`;
      } else {
        input = `<input data-conn="${n}" data-orig="${esc(f.value || '')}" value="${esc(f.value || '')}" placeholder="${esc(CONN_PLACEHOLDER[n] || '')}">`;
      }
      const note = f.set && f.source === 'server' ? '<div class="hint">Using the server\'s own setting.</div>'
        : f.secret && f.source === 'account' ? `<div class="hint"><button class="linklike" data-conn-clear="${n}">Remove the saved value</button></div>` : '';
      return `<div class="field"><label>${esc(CONN_LABEL[n] || n)}</label>${input}${note}</div>`;
    }).join('');
    const label = g.readyLabel ? g.readyLabel(state.config) : (ok ? 'Ready' : 'Not set');
    const tone = label === 'Not set' ? 'warn' : ok && g.test ? 'ok' : g.readyLabel ? 'info' : ok ? 'ok' : 'warn';
    return `<div class="conn-group"><h3>${esc(g.title)} <span class="chip ${tone}">${esc(label)}</span></h3>
      <div class="hint">${esc(g.help)}</div>
      ${g.test === 'smtp' && by.GMAIL_REFRESH_TOKEN && by.GMAIL_REFRESH_TOKEN.set && !state.config.gmail_api_client
        ? '<div class="notice">A Gmail API refresh token is saved, but this server has no GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET yet, so it can\'t be used. Add both on the host (Render > Environment).</div>' : ''}
      <div class="conn-grid">${inputs}</div>
      ${g.test ? `<div class="row actions conn-test"><button class="btn small" data-conn-test="${g.test}">Save &amp; test this connection</button>
        <span class="hint" data-conn-result="${g.test}">Saves any changes above, then tests the connection.</span></div>` : ''}</div>`;
  }).join('');
}

function collectConnChanges() {
  const set = {}, clear = [...state.connClear];
  $$('#st-conn [data-conn]').forEach((el) => {
    const name = el.dataset.conn, val = el.value.trim();
    if (el.type === 'password') { if (val) set[name] = val; }
    else if (val !== (el.dataset.orig || '')) { if (val) set[name] = val; else clear.push(name); }
  });
  return { set, clear };
}
async function saveConnections() {
  const { set, clear } = collectConnChanges();
  if (!Object.keys(set).length && !clear.length) return null;
  const r = await api('/api/connections', { method: 'PUT', body: { set, clear } });
  await loadConfig(); renderConnections(r.fields); $('#conn-unreadable').hidden = !r.unreadable;
  if (r.phones_fixed) toast(`${plural(r.phones_fixed, 'phone number')} that couldn't be read before can be now.`, 'ok');
  return r;
}

$('#st-conn').addEventListener('click', (e) => {
  const t = e.target.closest('[data-conn-test]');
  if (t) {
    const kind = t.dataset.connTest;
    run(async () => {
      const out = $(`[data-conn-result="${kind}"]`);
      t.disabled = true; out.textContent = 'Saving…'; out.className = 'hint';
      try {
        await saveConnections();                         // always save first -- a typed-but-unsaved
        out.textContent = 'Testing…';                     // value must never be tested as "not set"
        const r = await api('/api/connections/test', { method: 'POST', body: { kind } });
        // the fields just re-rendered from the save; re-find this test's result slot
        $(`[data-conn-result="${kind}"]`).textContent = '✓ ' + r.message;
        $(`[data-conn-result="${kind}"]`).className = 'hint ok-text';
      } catch (err) {
        $(`[data-conn-result="${kind}"]`).textContent = '✗ ' + err.message;
        $(`[data-conn-result="${kind}"]`).className = 'hint bad-text';
      }
    });
    return;
  }
  const b = e.target.closest('[data-conn-clear]'); if (!b) return;
  state.connClear.add(b.dataset.connClear);
  b.closest('.hint').textContent = 'Will be removed when you save.';
});
$('#conn-save').addEventListener('click', () => run(async () => {
  const r = await saveConnections();
  toast(r ? 'Connections saved.' : 'Nothing changed.');
}));

const SETTINGS_TABS = ['messaging', 'sending', 'connections', 'account'];
function showSettingsTab(name) {
  const tab = SETTINGS_TABS.includes(name) ? name : 'messaging';
  $$('#view-settings .tabpane').forEach((p) => { p.hidden = p.dataset.pane !== tab; });
  $$('#view-settings .subtabs a').forEach((a) => {
    a.classList.toggle('active', a.dataset.tab === tab); a.setAttribute('aria-selected', a.dataset.tab === tab);
  });
  $('#st-save').hidden = !['messaging', 'sending'].includes(tab);     // the other two tabs have their own save buttons
}

const hourLabel = (h) => (h === 0 || h === 24 ? 'midnight' : h === 12 ? 'noon' : h < 12 ? `${h} am` : `${h - 12} pm`);
async function loadSettingsView() {
  // Until the saved values have arrived the form can't be typed in -- otherwise they would land
  // on top of (and wipe out) whatever had been typed in the meantime.
  $('#view-settings').classList.add('loading');
  try { await fillSettingsView(); } finally { $('#view-settings').classList.remove('loading'); }
}
async function fillSettingsView() {
  await Promise.all([loadSettings(), loadConfig()]);
  const s = state.settings;
  $('#st-info').value = s.business_info;
  $('#st-limit').value = s.email_daily_limit; $('#st-delay').value = s.email_send_delay_seconds;
  $('#st-fudays').value = s.followup_delay_days; $('#st-fumax').value = s.max_followups;
  $('#st-warmup').checked = !!s.warmup_enabled; $('#st-footer').value = s.email_footer;
  $('#st-autoremove').checked = !!s.auto_remove_rejected;
  $('#st-autoremove-warn').hidden = !s.auto_remove_rejected;
  $('#st-exact').checked = !!s.exact_templates; $('#st-followup').value = s.followup_template || '';
  $('#st-window').checked = !!s.send_window_enabled; $('#st-tz').value = s.timezone || '';
  $('#st-start').innerHTML = Array.from({ length: 24 }, (_, h) => opt(h, hourLabel(h), h === s.send_start_hour)).join('');
  $('#st-end').innerHTML = Array.from({ length: 24 }, (_, i) => opt(i + 1, hourLabel(i + 1), i + 1 === s.send_end_hour)).join('');
  $('#st-days').innerHTML = DAY_LABEL.map((d, i) =>
    `<label><input type="checkbox" value="${i}" ${(s.send_days || []).includes(i) ? 'checked' : ''}><span>${d}</span></label>`).join('');
  const rm = $('#st-replymin'), want = String(s.reply_check_minutes ?? 30);
  if (![...rm.options].some((o) => o.value === want)) rm.insertAdjacentHTML('afterbegin', opt(want, `${want} minutes`));
  rm.value = want;
  $('#st-tpl-help').innerHTML = placeholderHelp();
  $('#ju-removal-note').textContent = s.auto_remove_rejected
    ? 'Auto-remove is ON — a rejected lead with no notes, draft or activity is deleted immediately.'
    : 'Rejected leads are kept, not deleted — filter by Fit = Rejected to review them.';
  state.tplEditor.set(s.templates); state.prioEditor.set(s.channel_priority);
  $('#ac-name').value = (state.me && state.me.name) || '';
  await renderSuppression();
  const conn = await api('/api/connections');
  renderConnections(conn.fields);
  $('#conn-unreadable').hidden = !conn.unreadable;
  renderUsers();
  renderStorage();
}

$('#st-autoremove').addEventListener('change', (e) => { $('#st-autoremove-warn').hidden = !e.target.checked; });
$('#st-tz-mine').addEventListener('click', () => {
  try { $('#st-tz').value = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { /* very old browser */ }
});
$('#st-save').addEventListener('click', () => run(async () => {
  const num = (sel, fallback) => { const v = $(sel).value.trim(); return v === '' || Number.isNaN(+v) ? fallback : +v; };
  const body = {
    business_info: $('#st-info').value, email_footer: $('#st-footer').value, templates: state.tplEditor.get(),
    channel_priority: state.prioEditor.get(),
    email_daily_limit: num('#st-limit', 40), email_send_delay_seconds: num('#st-delay', 45),
    followup_delay_days: num('#st-fudays', 3), max_followups: num('#st-fumax', 2),
    warmup_enabled: $('#st-warmup').checked, auto_remove_rejected: $('#st-autoremove').checked,
    exact_templates: $('#st-exact').checked, followup_template: $('#st-followup').value,
    send_window_enabled: $('#st-window').checked, send_start_hour: +$('#st-start').value, send_end_hour: +$('#st-end').value,
    send_days: $$('#st-days input:checked').map((c) => +c.value), timezone: $('#st-tz').value.trim(),
    reply_check_minutes: +$('#st-replymin').value,
  };
  const dropped = state.tplEditor.get().filter((t) => !(t.name || '').trim() || !(t.body || '').trim()).length;
  state.settings = await api('/api/settings', { method: 'PUT', body });
  state.exactTouched = undefined;
  toast(dropped ? `Settings saved — ${plural(dropped, 'template')} without a name or text ${dropped === 1 ? 'was' : 'were'} left out.` : 'Settings saved.', dropped ? '' : 'ok');
  loadSettingsView();
}));

async function renderSuppression() {
  const list = await api('/api/suppression');
  $('#sp-list').innerHTML = list.length ? list.map((s) =>
    `<div class="list-row"><div class="grow">${esc(s.value)} <span class="sub">${esc(s.kind)} · ${esc(s.reason)}</span></div>
      <button class="btn small" data-sp-del="${esc(s.value)}">Remove</button></div>`).join('')
    : '<div class="empty small"><b>Nobody yet</b>People who reply asking you to stop are added automatically.</div>';
}
$('#sp-add').addEventListener('click', () => run(async () => {
  await api('/api/suppression', { method: 'POST', body: { value: $('#sp-value').value, reason: 'added by hand' } });
  $('#sp-value').value = ''; toast('Added.', 'ok'); renderSuppression();
}));
$('#sp-list').addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('[data-sp-del]'); if (!b) return;
  await api('/api/suppression', { method: 'DELETE', body: { value: b.dataset.spDel } });
  toast('Removed — leads it had flagged can be contacted again.'); renderSuppression();
}));

$('#ac-save').addEventListener('click', () => run(async () => {
  await api('/api/account', { method: 'PUT', body: { name: $('#ac-name').value } });
  state.me = await api('/api/auth/me'); showWho(); toast('Saved.', 'ok');
}));
$('#pw-save').addEventListener('click', () => run(async () => {
  const cur = $('#pw-cur').value, next = $('#pw-new').value, confirm_ = $('#pw-confirm').value;
  if (!cur || !next) { toast('Fill in your current and new password.', 'err'); return; }
  if (next.length < 8) { toast('New password must be at least 8 characters.', 'err'); return; }
  if (next !== confirm_) { toast("New password and confirmation don't match.", 'err'); return; }
  await api('/api/auth/password', { method: 'POST', body: { current: cur, new: next } });
  $('#pw-cur').value = ''; $('#pw-new').value = ''; $('#pw-confirm').value = '';
  toast('Password changed. Other browsers have been signed out.', 'ok');
}));

document.addEventListener('click', (e) => {
  const b = e.target.closest('.pw-eye'); if (!b) return;
  const input = document.getElementById(b.dataset.toggle);
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  b.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
  b.classList.toggle('is-showing', !showing);
});

async function renderStorage() {
  const card = $('#st-storage-card');
  card.hidden = !(state.me && state.me.is_admin);
  if (card.hidden) return;
  const d = await api('/api/admin/storage'), s = d.sync;
  let html;
  if (!s.enabled) {
    html = `<div class="notice">Data is only stored on this server's own disk. If your host wipes its disk on restart
      (free plans often do), everyone's leads and logins would be lost. See the free-tier guide in the README to copy them to Supabase Storage.</div>
      <div class="kv"><span>Data folder</span><b>${esc(d.data_dir)}</b></div>`;
    $('#stor-backup').hidden = true;
  } else {
    $('#stor-backup').hidden = false;
    const bad = s.last_error ? `<div class="notice">${esc(s.last_error)}</div>` : '';
    const conflicts = Object.keys(s.conflicts || {});
    html = bad + `
      <div class="kv"><span>Copied to</span><b>${esc(s.kind)}</b></div>
      <div class="kv"><span>Last successful copy</span><b>${s.last_ok ? ago(s.last_ok) : 'not yet'}</b></div>
      <div class="kv"><span>Waiting to upload</span><b>${s.waiting_to_upload.length ? s.waiting_to_upload.join(', ') : 'nothing'}</b></div>
      <div class="kv"><span>Daily backup</span><b>${s.last_backup_day || 'none yet'} (keeping ${s.keep_days} days)</b></div>
      <div class="kv"><span>Storage used</span><b>${s.used_mb ?? '?'} MB of ${s.budget_mb} MB budget</b></div>` +
      (conflicts.length ? `<div class="notice">Conflict on ${esc(conflicts.join(', '))}: another copy of the app changed the stored data. Nothing was overwritten. Run <code>python3 manage.py sync-status</code> on the server.</div>` : '');
  }
  html += `<div class="kv"><span>Limits</span><b>${d.limits.max_users} people · ${d.limits.max_leads_per_user} leads each</b></div>`;
  $('#st-storage').innerHTML = html;
}
$('#stor-backup').addEventListener('click', () => run(async () => {
  const r = await api('/api/admin/backup-now', { method: 'POST' });
  toast(`Backed up ${r.files_backed_up} files.`, 'ok'); renderStorage();
}));

async function renderUsers() {
  const card = $('#st-users-card');
  card.hidden = !(state.me && state.me.is_admin);
  if (card.hidden) return;
  const list = await api('/api/admin/users');
  $('#st-users').innerHTML = list.map((u) =>
    `<div class="list-row"><div class="grow"><b>${esc(u.email)}</b>${u.is_admin ? ' <span class="chip info">admin</span>' : ''}
       <div class="sub">${u.leads === null ? '' : u.leads + ' leads · '}${u.last_login_at ? 'last signed in ' + ago(u.last_login_at) : 'never signed in'}</div></div>
     ${u.id === state.me.id ? '' : `<button class="btn small danger" data-user-del="${u.id}" data-email="${esc(u.email)}">Delete</button>`}</div>`).join('');
}
$('#st-users').addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('[data-user-del]'); if (!b) return;
  if (!confirm(`Delete ${b.dataset.email} and ALL of their leads, campaigns and settings? This cannot be undone.`)) return;
  await api(`/api/admin/users/${b.dataset.userDel}`, { method: 'DELETE' });
  toast('Account deleted.', 'ok'); renderUsers();
}));

/* ------------------------------------------------------------------ jobs */
let pollTimer = null;
async function refreshJobs() {
  clearTimeout(pollTimer);
  try {
    // whole logs only for the runs whose "Log" is open; a few closing lines for the rest
    const jobs = await api(`/api/jobs?limit=40&logs=${[...state.openLogs].join(',')}`);
    const finished = [];
    jobs.forEach((j) => {
      const active = ['queued', 'running'].includes(j.status);
      const prev = state.jobSeen[j.id];
      if (prev === 'active' && !active) finished.push(j);
      state.jobSeen[j.id] = active ? 'active' : 'done';
    });
    state.jobs = jobs;
    renderJobs();
    finished.forEach((j) => {
      toast(`${JOB_LABEL[j.kind] || j.kind}: ${j.summary || j.status}`, j.status === 'error' ? 'err' : 'ok');
      const again = { leads: loadLeads, dashboard: loadDashboard, outreach: loadOutreach, find: loadCampaigns,
        campaign: () => loadCampaignView(state.campaign), campaigns: loadCampaignsView, pipeline: loadCampaigns }[state.view];
      if (again) Promise.resolve(again()).catch(() => {});
    });
  } catch (e) { /* server restarting -- try again */ }
  const anyActive = state.jobs.some((j) => ['queued', 'running'].includes(j.status));
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refreshJobs, anyActive ? 1800 : 6000);
}

function jobCard(j) {
  const active = ['queued', 'running'].includes(j.status);
  const pct = j.total ? Math.min(100, Math.round((j.progress / j.total) * 100)) : (active ? 5 : 100);
  const log = (j.log || '').trim();
  return `<div class="job" data-job="${j.id}">
    <div class="job-head"><b>${esc(JOB_LABEL[j.kind] || j.kind)}</b><span class="chip ${j.status}">${j.status}</span>
      <span class="muted">${ago(j.created_at)}</span><span class="spacer"></span>
      ${active ? `<button class="btn small" data-stop="${j.id}">Stop</button>` : ''}</div>
    ${active ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="sub">${j.progress}${j.total ? ' of ' + j.total : ''}</div>` : ''}
    ${j.summary ? `<div class="summary">${esc(j.summary)}</div>` : ''}
    ${log ? `<details data-log="${j.id}" ${state.openLogs.has(j.id) ? 'open' : ''}><summary>Log</summary><pre>${j.log_more ? '…\n' : ''}${esc(log)}</pre></details>` : ''}</div>`;
}
function renderJobs() {
  $$(`#view-${state.view} [data-jobs]`).forEach((box) => {
    const kinds = box.dataset.jobs ? box.dataset.jobs.split(',') : null;
    const limit = +box.dataset.limit || 5;
    const scoped = box.dataset.campaignJobs !== undefined;
    const list = state.jobs.filter((j) => (!kinds || kinds.includes(j.kind)) && (!scoped || j.campaign === state.campaign)).slice(0, limit);
    box.innerHTML = list.length ? list.map(jobCard).join('')
      : box.dataset.quiet ? '' : '<div class="hint" style="margin-top:8px">Nothing has run yet.</div>';
    // keep an open log scrolled to its newest line while the run is going
    $$('details[open] pre', box).forEach((p) => { p.scrollTop = p.scrollHeight; });
  });
}
document.addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('[data-stop]'); if (!b) return;
  await api(`/api/jobs/${b.dataset.stop}/stop`, { method: 'POST' });
  toast('Stopping after the current item…'); refreshJobs();
}));
document.addEventListener('toggle', (e) => {
  const d = e.target; if (!d.dataset || !d.dataset.log) return;
  d.open ? state.openLogs.add(+d.dataset.log) : state.openLogs.delete(+d.dataset.log);
  if (d.open) refreshJobs();                    // fetch the whole log now that someone wants to read it
}, true);

/* ---------------------------------------------------------------- drawer */
const DW_SECTIONS = [
  ['Business', [['business_name', 'Business name', 'wide'], ['contact_name', 'Contact person'], ['trade', 'Trade', 'trade'],
    ['category_raw', 'Category (as found)'], ['address', 'Address', 'wide'], ['city', 'City'], ['state', 'State']]],
  ['Contact', [['phone', 'Phone'], ['email', 'Email'], ['website', 'Website', 'wide'], ['instagram_url', 'Instagram'],
    ['facebook_url', 'Facebook'], ['linkedin_url', 'LinkedIn'], ['yelp_url', 'Yelp page'], ['google_maps_url', 'Google Maps page']]],
  ['Tracking', [['campaign', 'Campaign'], ['stage', 'Stage', 'stage'], ['channel', 'How to reach', 'channel'], ['notes', 'Notes', 'notes']]],
];

function fieldHtml(key, label, kind, lead) {
  const wide = ['wide', 'notes'].includes(kind) ? ' wide' : '';
  let v = lead[key] ?? '';
  if (key === 'phone') v = lead.phone || lead.phone_raw || '';
  let input;
  if (kind === 'trade') input = `<select data-f="${key}">${TRADES.map((t) => opt(t, TRADE_LABEL[t], t === lead.trade)).join('')}</select>`;
  else if (kind === 'stage') input = `<select data-f="${key}">${STAGES.map((s) => opt(s, s, s === lead.stage)).join('')}</select>`;
  else if (kind === 'channel') {
    const cur = lead.channel_locked ? lead.channel : 'auto';
    input = `<select data-f="channel">${opt('auto', `Automatic (${CHANNEL_LABEL[lead.channel] || 'none'})`, cur === 'auto')}${CHANNELS.map((c) => opt(c, CHANNEL_LABEL[c], cur === c)).join('')}</select>`;
  } else if (kind === 'notes') input = `<textarea data-f="${key}" rows="3">${esc(v)}</textarea>`;
  else input = `<input data-f="${key}" value="${esc(v)}">`;
  return `<div class="field${wide}"><label>${esc(label)}</label>${input}</div>`;
}

function drawerHtml(lead, events) {
  const isNew = !lead.id;
  const secs = DW_SECTIONS.map(([title, fields]) =>
    `<section class="dw-section"><h3>${title}</h3><div class="dw-grid">${fields.map(([k, l, kind]) => fieldHtml(k, l, kind, lead)).join('')}</div>
     ${title === 'Tracking' ? `<label class="check"><input type="checkbox" data-f="do_not_contact" ${lead.do_not_contact ? 'checked' : ''}> Do not contact</label>` : ''}</section>`).join('');
  if (isNew) return secs;

  const facts = [
    `<div><b>Score ${lead.score}/10</b> — ${esc(lead.score_reasons || '')}</div>`,
    lead.fit === null ? '<div><b>Fit:</b> not judged yet</div>'
      : `<div><b>Fit:</b> ${lead.fit ? 'yes' : 'no'} (${esc(lead.fit_confidence)}) — ${esc(lead.fit_reason)}</div>`,
    lead.enriched_at ? `<div><b>Enriched</b> ${ago(lead.enriched_at)}${lead.enrichment_notes ? ' — ' + esc(lead.enrichment_notes) : ''}</div>` : '<div><b>Not enriched yet</b></div>',
    `<div><b>Source:</b> ${esc(lead.source || 'unknown')}${lead.last_query ? ' — ' + esc(lead.last_query) : ''} · added ${ago(lead.created_at)}</div>`,
    lead.dnc_reason ? `<div><b>Do-not-contact reason:</b> ${esc(lead.dnc_reason)}</div>` : '',
  ].join('');

  const digits = (lead.phone || '').replace(/\D/g, '');
  const links = [
    lead.phone || lead.phone_raw ? `<a class="btn small" href="tel:${esc(lead.phone || lead.phone_raw)}">Call</a>` : '',
    digits ? `<a class="btn small" href="https://wa.me/${digits}" target="_blank" rel="noopener">WhatsApp</a>` : '',
    lead.email ? `<a class="btn small" href="mailto:${esc(lead.email)}">Email</a>` : '',
    lead.website ? `<a class="btn small" href="${esc(lead.website)}" target="_blank" rel="noopener">Website</a>` : '',
    lead.google_maps_url ? `<a class="btn small" href="${esc(lead.google_maps_url)}" target="_blank" rel="noopener">Maps</a>` : '',
    lead.facebook_url ? `<a class="btn small" href="${esc(lead.facebook_url)}" target="_blank" rel="noopener">Facebook</a>` : '',
    lead.instagram_url ? `<a class="btn small" href="${esc(lead.instagram_url)}" target="_blank" rel="noopener">Instagram</a>` : '',
    lead.yelp_url ? `<a class="btn small" href="${esc(lead.yelp_url)}" target="_blank" rel="noopener">Yelp</a>` : '',
  ].filter(Boolean).join('');
  const nextStages = { new: ['contacted', 'replied'], contacted: ['replied', 'dead'], replied: ['qualified', 'won', 'lost'],
    qualified: ['won', 'lost'], won: [], lost: ['new'], dead: ['new'] }[lead.stage] || [];
  const quick = `<section class="dw-section"><div class="quick">
      <span class="chip stage-${lead.stage}">${lead.stage}</span>
      ${nextStages.map((st) => `<button class="btn small" data-stage="${st}">Mark ${st}</button>`).join('')}
      <span class="spacer"></span>${links}</div></section>`;
  const canEmail = lead.email && state.config && state.config.smtp;
  const followable = lead.email_message_id && lead.send_status === 'sent' && lead.stage === 'contacted';
  const msg = `<section class="dw-section"><h3>Message</h3>
    <div class="row"><div class="field"><label>Template</label><select id="dw-tpl">${templateOptions('', state.drawer.templates)}</select></div>
      <button class="btn small" id="dw-gen" style="margin-bottom:12px">${lead.message ? 'Redraft' : 'Generate'}</button></div>
    <textarea id="dw-message" rows="9" aria-label="Message">${esc(lead.message)}</textarea>
    <div class="msg-actions">
      ${lead.channel === 'email' || lead.email ? `<button class="btn primary small" id="dw-send" ${canEmail && lead.send_status !== 'sent' ? '' : 'disabled'}
          title="${!lead.email ? 'No email address' : !state.config.smtp ? 'Email isn\'t set up yet (Settings → Connections)' : lead.send_status === 'sent' ? 'Already sent' : ''}">Send email</button>` : ''}
      <button class="btn small" id="dw-copy">Copy &amp; open</button>
      <button class="btn small" id="dw-mark" ${lead.send_status === 'sent' ? 'disabled' : ''}>Mark sent</button>
      ${followable ? '<button class="btn small" id="dw-follow">Send follow-up</button>' : ''}
    </div></section>`;

  const evs = events.map((ev) => {
    let d = ev.detail || '';
    try { const j = JSON.parse(d); d = j.snippet || j.text || j.subject || d; } catch (e) { /* plain text */ }
    return `<div class="ev"><span class="muted">${ago(ev.created_at)}</span><span><b>${esc(ev.kind.replace('_', ' '))}</b> ${esc(String(d).slice(0, 220))}</span></div>`;
  }).join('') || '<div class="muted">No activity yet.</div>';

  return `${quick}<section class="dw-section"><div class="facts">${facts}</div></section>${secs}${msg}
    <section class="dw-section"><h3>Activity</h3><div class="timeline">${evs}</div></section>`;
}

function openDrawer(lead, events = [], templates = null) {
  // Re-rendering the lead that's already open (after a save/draft/send) keeps the scroll position.
  const y = lead.id && lead.id === state.drawer.id ? $('#dw-body').scrollTop : 0;
  state.drawer = { id: lead.id || null, lead, templates };
  $('#dw-title').textContent = lead.id ? lead.business_name || '(no name)' : 'New lead';
  $('#dw-sub').textContent = lead.id ? [TRADE_LABEL[lead.trade] && lead.trade ? TRADE_LABEL[lead.trade] : '', [lead.city, lead.state].filter(Boolean).join(', ')].filter(Boolean).join(' · ') : '';
  $('#dw-body').innerHTML = drawerHtml(lead, events);
  $('#dw-delete').hidden = !lead.id;
  $('#dw-save').textContent = lead.id ? 'Save changes' : 'Add lead';
  $('#scrim').hidden = false;
  const d = $('#drawer'); d.classList.add('open'); d.setAttribute('aria-hidden', 'false');
  d.scrollTop = 0; $('#dw-body').scrollTop = y;
}
function closeDrawer() {
  $('#drawer').classList.remove('open'); $('#drawer').setAttribute('aria-hidden', 'true');
  $('#scrim').hidden = true; state.drawer = { id: null, lead: null, templates: null };
}
async function openLead(id) {
  if (!state.config) await loadConfig();
  if (!state.settings) await loadSettings();
  const d = await api(`/api/leads/${id}`);
  openDrawer(d.lead, d.events, d.templates);
}
function openNewLead() {
  run(async () => {
    if (!state.config) await loadConfig();
    openDrawer({ business_name: '', stage: 'new', channel: 'unknown', channel_locked: 0, do_not_contact: 0, trade: '' });
  });
}

function drawerChanges() {
  const lead = state.drawer.lead, patch = {};
  $$('#dw-body [data-f]').forEach((el) => {
    const k = el.dataset.f;
    const val = el.type === 'checkbox' ? (el.checked ? 1 : 0) : el.value.trim();
    if (k === 'channel') {
      const cur = lead.channel_locked ? lead.channel : 'auto';
      if (val !== cur) patch.channel = val;
    } else if (k === 'phone') {
      if (val !== (lead.phone || lead.phone_raw || '')) patch.phone = val;
    } else if (k === 'do_not_contact') {
      if (val !== (lead.do_not_contact ? 1 : 0)) patch.do_not_contact = val;
    } else if (val !== (lead[k] ?? '')) patch[k] = val;
  });
  const m = $('#dw-message');
  if (m && m.value !== (lead.message || '')) patch.message = m.value;
  return patch;
}

async function saveDrawer(quiet = false) {
  const patch = drawerChanges();
  if (!state.drawer.id) {
    if (!(patch.business_name || '').trim()) throw new Error('Business name is required');
    const r = await api('/api/leads', { method: 'POST', body: patch });
    toast(r.is_new ? 'Lead added.' : 'That lead already existed — details merged in.', 'ok');
    if (state.view === 'leads') loadLeads();
    loadCampaigns();
    return openLead(r.lead.id);
  }
  if (!Object.keys(patch).length) { if (!quiet) toast('Nothing to save.'); return; }
  const r = await api(`/api/leads/${state.drawer.id}`, { method: 'PATCH', body: patch });
  if (!quiet) toast('Saved.', 'ok');
  const d = await api(`/api/leads/${state.drawer.id}`);
  openDrawer(d.lead, d.events, d.templates);
  if (state.view === 'leads') loadLeads();
  return r;
}

$('#dw-save').addEventListener('click', () => run(() => saveDrawer()));
$('#dw-close').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#drawer').classList.contains('open')) closeDrawer(); });
$('#dw-delete').addEventListener('click', () => run(async () => {
  if (!confirm('Permanently delete this lead?')) return;
  await api(`/api/leads/${state.drawer.id}`, { method: 'DELETE' });
  closeDrawer(); toast('Deleted.', 'ok'); loadCampaigns();
  if (state.view === 'leads') loadLeads();
}));

async function refreshDrawer() {
  const d = await api(`/api/leads/${state.drawer.id}`);
  openDrawer(d.lead, d.events, d.templates);
  if (state.view === 'leads') loadLeads();
  if (state.view === 'outreach') loadOutreach();
}
$('#dw-body').addEventListener('click', (e) => run(async () => {
  const b = e.target.closest('button'); if (!b) return;
  const id = state.drawer.id;
  if (b.dataset.stage) {
    await saveDrawer(true);
    await api(`/api/leads/${id}`, { method: 'PATCH', body: { stage: b.dataset.stage } });
    toast(`Marked ${b.dataset.stage}.`, 'ok'); await refreshDrawer();
    if (state.view === 'dashboard') loadDashboard();
  } else if (b.id === 'dw-gen') {
    const tpl = $('#dw-tpl').value;
    await saveDrawer(true);
    b.disabled = true; b.textContent = 'Writing…';
    let r;
    try { r = await api(`/api/leads/${id}/draft`, { method: 'POST', body: { template: tpl } }); }
    finally { await refreshDrawer(); }
    if (r.unfilled && r.unfilled.length) toast(`Written, but ${r.unfilled.join(', ')} had nothing to fill it — edit the message or the lead before sending.`, 'err');
    else toast('Message written.', 'ok');
  } else if (b.id === 'dw-send') {
    await saveDrawer(true);
    const r = await api(`/api/leads/${id}/send-email`, { method: 'POST', body: {} });
    toast(`Email sent — "${r.subject}"`, 'ok'); await refreshDrawer();
  } else if (b.id === 'dw-copy') {
    const l = { ...state.drawer.lead, ...drawerLive() };
    await copyAndOpen(l, $('#dw-message').value);
  } else if (b.id === 'dw-mark') {
    await saveDrawer(true);
    await api(`/api/leads/${id}/mark-sent`, { method: 'POST', body: {} });
    toast('Marked as sent.', 'ok'); await refreshDrawer();
  } else if (b.id === 'dw-follow') {
    await api(`/api/leads/${id}/followup`, { method: 'POST' });
    toast('Follow-up sent.', 'ok'); await refreshDrawer();
  }
}));
function drawerLive() { // current values of contact fields, in case they were edited but not saved
  const o = {};
  $$('#dw-body [data-f]').forEach((el) => { if (['email', 'phone', 'yelp_url', 'facebook_url', 'instagram_url'].includes(el.dataset.f)) o[el.dataset.f] = el.value.trim(); });
  return o;
}

/* ------------------------------------------------------------------ boot */
function showWho() {
  const who = $('#who');
  who.textContent = state.me.name || state.me.email; who.title = state.me.email;
}
$('#signout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } finally { location.href = '/login'; }
});

initLeadFilters();
(async () => {
  try { state.me = await api('/api/auth/me'); } catch (e) { return; }   // a 401 already redirected to /login
  showWho();
  if (!location.hash) history.replaceState(null, '', '#dashboard');
  route();
})();
