'use strict';
const $ = (s) => document.querySelector(s);
let policy = { needs_first_user: false, allowed: true, needs_code: false };

function say(msg) { const m = $('#auth-msg'); m.textContent = msg || ''; m.hidden = !msg; }

function tab(which) {
  const signup = which === 'signup';
  $('#login-form').hidden = signup; $('#signup-form').hidden = !signup;
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

$('#tab-login').addEventListener('click', () => tab('login'));
$('#tab-signup').addEventListener('click', () => tab('signup'));

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault(); say('');
  try { await post('/api/auth/login', { email: $('#li-email').value, password: $('#li-pw').value }); location.href = '/'; }
  catch (err) { say(err.message); }
});
$('#signup-form').addEventListener('submit', async (e) => {
  e.preventDefault(); say('');
  try {
    await post('/api/auth/signup', { name: $('#su-name').value, email: $('#su-email').value,
      password: $('#su-pw').value, code: $('#su-code').value });
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
