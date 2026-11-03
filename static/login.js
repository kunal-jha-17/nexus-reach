'use strict';
const $ = (s) => document.querySelector(s);
let policy = { needs_first_user: false, allowed: true, needs_code: false };

function say(msg, ok) { const m = $('#auth-msg'); m.textContent = msg || ''; m.hidden = !msg; m.className = 'result ' + (ok ? 'ok' : 'err'); }
function only(id) { ['#login-form', '#signup-form', '#forgot-form', '#reset-form'].forEach((f) => { $(f).hidden = f !== id; }); }

function tab(which) {
  const signup = which === 'signup';
  only(signup ? '#signup-form' : '#login-form');
  $('#tab-login').classList.toggle('active', !signup); $('#tab-signup').classList.toggle('active', signup);
  $('#tab-login').setAttribute('aria-selected', String(!signup)); $('#tab-signup').setAttribute('aria-selected', String(signup));
  say(signup && !policy.allowed ? 'Sign-ups are closed on this server. Ask the owner to create an account for you.' : '');
  (signup ? $('#su-email') : $('#li-email')).focus();
}

async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' }, body: JSON.stringify(body) });
  let data = {}; try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status})`);
  return data;
}

async function postRaw(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' }, body: JSON.stringify(body) });
  let data = {}; try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) { const err = new Error(data.error || `Something went wrong (${res.status})`); err.data = data; throw err; }
  return data;
}
$('#show-forgot').addEventListener('click', () => { only('#forgot-form'); say(''); $('#fg-email').value = $('#li-email').value; $('#fg-email').focus(); });
$('#back-login').addEventListener('click', () => { tab('login'); });
$('#forgot-form').addEventListener('submit', async (e) => {
  e.preventDefault(); say('');
  try { const r = await post('/api/auth/forgot', { email: $('#fg-email').value }); say(r.message, true); }
  catch (err) { say(err.message); }
});
$('#reset-form').addEventListener('submit', async (e) => {
  e.preventDefault(); say('');
  const token = new URLSearchParams(location.search).get('reset');
  try {
    const r = await post('/api/auth/reset', { token, password: $('#rs-pw').value });
    if (r.message) { history.replaceState(null, '', '/login'); tab('login'); say(r.message, true); } else location.href = '/';
  } catch (err) { say(err.message); }
});
$('#tab-login').addEventListener('click', () => tab('login'));
$('#tab-signup').addEventListener('click', () => tab('signup'));

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault(); say('');
  try {
    await postRaw('/api/auth/login', { email: $('#li-email').value, password: $('#li-pw').value, code: $('#li-code').value });
    const inv = new URLSearchParams(location.search).get('invite');
    if (inv) { try { await post('/api/team/accept', { token: inv }); } catch (err2) { say(err2.message); return; } }
    location.href = '/';
  } catch (err) {
    if (err.data && err.data.need_code) { $('#li-code-field').hidden = false; $('#li-code').focus(); }
    say(err.message);
  }
});
$('#signup-form').addEventListener('submit', async (e) => {
  e.preventDefault(); say('');
  try {
    await post('/api/auth/signup', { name: $('#su-name').value, email: $('#su-email').value,
      password: $('#su-pw').value, code: $('#su-code').value, invite: new URLSearchParams(location.search).get('invite') || '' });
    location.href = '/';
  } catch (err) { say(err.message); }
});

(async () => {
  try {
    const res = await fetch('/api/auth/status'); policy = await res.json();
    if (policy.logged_in) { location.href = '/'; return; }
    $('#first-user').hidden = !policy.needs_first_user;
    $('#su-code-field').hidden = !policy.needs_code;
    if (policy.needs_code) {
      const setup = policy.code_kind === 'setup';
      $('#su-code-field label').textContent = setup ? 'Setup code (from the server log)' : 'Invite code';
      $('#su-code').required = true;
      if (setup) $('#first-user').innerHTML = 'No accounts exist yet. Because you\'re not on the server itself, creating the <b>admin</b> ' +
        'account needs the <b>setup code</b> printed in the server\'s log when it started (on Render: the <b>Logs</b> tab).';
    }
    if (policy.full && !policy.needs_first_user) say('This server has reached its user limit, so new accounts can\'t be created right now.');
    if (new URLSearchParams(location.search).get('reset')) { only('#reset-form'); $('#rs-pw').focus(); return; }
    const invite = new URLSearchParams(location.search).get('invite');
    if (invite) {
      try {
        const r = await (await fetch('/api/auth/invite?t=' + encodeURIComponent(invite))).json();
        if (r.error) { say(r.error); }
        else {
          $('#auth-tagline').textContent = `${r.owner} invited you to their team.`;
          $('#su-code-field').hidden = true; $('#su-code').required = false;
          if (r.exists) { tab('login'); $('#li-email').value = r.email; $('#li-pw').focus(); }
          else { tab('signup'); say(''); $('#su-email').value = r.email; $('#su-pw').focus(); }
          return;
        }
      } catch (e) { /* fall through to the normal page */ }
    }
    if (policy.needs_first_user) tab('signup'); else $('#li-email').focus();
  } catch (e) { /* server not reachable -- the form still shows */ }
})();

document.addEventListener('click', (e) => {
  const b = e.target.closest('.pw-eye'); if (!b) return;
  const input = document.getElementById(b.dataset.toggle);
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  b.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
  b.classList.toggle('is-showing', !showing);
});
