"""A stand-in for Google Maps: a results feed that lazy-loads on scroll, RE-RENDERS its
cards on every load (the thing that hung the old scraper), and place pages that draw
their details late. Proves the scraper's flow, not Google's real markup."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

N = 120
def biz(i):
    kind = i % 4      # 0: real website (shown on card)  1: none  2: facebook only  3: real website NOT on card
    site = {0: f"https://biz{i}.example.com", 1: "", 2: f"https://www.facebook.com/biz{i}", 3: f"https://hidden{i}.example.com"}[kind]
    return {"i": i, "name": f"Biz {i} HVAC", "site": site, "card_site": site if kind in (0, 2) else "",
            "phone": f"(214) 555-{1000 + i}", "addr": f"{i} Elm St, Garland, TX 75040"}

FEED = """<!doctype html><title>maps</title><div role="feed" style="height:600px;overflow:auto"></div>
<script>
const ALL = %s; let shown = 0; const feed = document.querySelector('[role=feed]');
function render() {
  feed.innerHTML = '';                      // every load throws away and rebuilds all cards
  for (const b of ALL.slice(0, shown)) {
    const d = document.createElement('div'); d.setAttribute('role', 'article'); d.style.height = '90px';
    d.innerHTML = `<a class="hfpxzc" href="${location.origin}/maps/place/Biz+${b.i}/data=!4m7!3m6!1s0x${(1000+b.i).toString(16)}:0x${(5000+b.i).toString(16)}!8m2?authuser=0&rclk=1">${b.name}</a>`
      + (b.card_site ? `<a data-value="Website" href="${b.card_site}">Website</a>` : '');
    feed.appendChild(d);
  }
  const end = document.createElement('div');
  end.textContent = shown >= ALL.length ? "You've reached the end of the list." : 'loading';
  feed.appendChild(end);
}
function more() { shown = Math.min(ALL.length, shown + 7); render(); }
more();
let busy = false;
feed.addEventListener('scroll', () => {
  if (busy || shown >= ALL.length) return;
  if (feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 50) { busy = true; setTimeout(() => { more(); busy = false; }, 250); }
});
setInterval(render, 120);                   // ...and Maps-style background re-renders
</script>"""

PLACE = """<!doctype html><title>place</title><div id="pane"></div><script>
const b = %s;
setTimeout(() => { document.getElementById('pane').innerHTML = '<h1>' + b.name + '</h1>'; }, 300);
setTimeout(() => { document.getElementById('pane').innerHTML += 
  `<button data-item-id="address" aria-label="Address: ${b.addr}"></button>`
  + (b.site ? `<a data-item-id="authority" href="${b.site}">site</a>` : '')
  + `<button data-item-id="phone:tel:x" aria-label="Phone: ${b.phone}"></button>`
  + '<button jsaction="pane.rating.category">HVAC contractor</button><span aria-label="12 reviews"></span>'; }, 700);
</script>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        hits.append(self.path)
        if self.path.startswith("/maps/search/"):
            body = FEED % json.dumps([biz(i) for i in range(N)])
        elif self.path.startswith("/maps/place/Biz+"):
            i = int(self.path.split("Biz+")[1].split("/")[0])
            if i in BROKEN: time.sleep(0.2); body = "<title>x</title>"      # never draws a name
            else: body = PLACE % json.dumps(biz(i))
        else:
            self.send_response(404); self.end_headers(); return
        b = body.encode(); self.send_response(200); self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

hits, BROKEN = [], set()
def start(port=0, n=120, broken=()):
    global N; N = n; BROKEN.clear(); BROKEN.update(broken); hits.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
