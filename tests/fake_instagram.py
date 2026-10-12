"""A stand-in for Instagram: a login page (cookie banner first, the newer name="email" field),
a hashtag page that needs a session, post pages and profile pages. Proves the scraper's login
and session handling -- not Instagram's live markup."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

hits = []
mode = {"login": "ok"}        # ok | no_form | wrong_password | challenge

LOGIN = """<!doctype html><title>Login • Instagram</title>
<div id="cookies"><button onclick="this.parentNode.remove()">Allow all cookies</button></div>
<form onsubmit="event.preventDefault(); go()"><input name="email" aria-label="Phone number, username or email">
<input name="pass" type="password"><button type="submit">Log in</button></form><p id="msg"></p>
<script>
function go() {
  fetch('/login-post', {method: 'POST'}).then(r => r.text()).then(t => {
    if (t === 'ok') { document.cookie = 'sessionid=abc; path=/'; location.href = '/'; }
    else if (t === 'challenge') { location.href = '/challenge/123/'; }
    else { document.getElementById('msg').textContent = 'Sorry, your password was incorrect. Please double-check your password.'; }
  });
}
</script>"""
NO_FORM = "<!doctype html><title>Instagram</title><p>Something went wrong. Please try again later.</p>"
TAG = "<!doctype html><title>#{tag}</title>" + "".join(f'<a href="/p/post{i}/">post</a>' for i in range(3)) + '<a href="/reel/r1/">reel</a>'
POST = '<!doctype html><title>post</title><header><a href="/{user}/">{user}</a></header>'
PROFILE = ('<!doctype html><title>@{user}</title><header><h2>{user}</h2><span>Dental clinic in Abu Dhabi. '
           'Book: {user}@clinic.ae +971 50 123 4{n}</span></header>')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, code=200, headers=None):
        b = body.encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        hits.append("POST " + self.path)
        self.send({"ok": "ok", "challenge": "challenge"}.get(mode["login"], "bad"))

    def do_GET(self):
        path = urlparse(self.path).path
        hits.append("GET " + path)
        logged_in = "sessionid=abc" in (self.headers.get("Cookie") or "")
        if path.startswith("/accounts/login"):
            return self.send(NO_FORM if mode["login"] == "no_form" else LOGIN)
        if path.startswith("/challenge"):
            return self.send("<title>Help us confirm it's you</title>")
        if path.startswith("/explore/tags/"):
            if not logged_in:
                return self.send("", 302, {"Location": "/accounts/login/?next=" + path})
            return self.send(TAG.replace("{tag}", path.split("/")[3]))
        if path.startswith(("/p/", "/reel/")):
            return self.send(POST.replace("{user}", "clinic_" + path.strip("/").split("/")[1]))
        if path == "/":
            return self.send("<title>Instagram</title><nav>home</nav>")
        user = path.strip("/")
        return self.send(PROFILE.replace("{user}", user).replace("{n}", str(100 + sum(map(ord, user)) % 900)))


def start():
    hits.clear()
    mode["login"] = "ok"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
