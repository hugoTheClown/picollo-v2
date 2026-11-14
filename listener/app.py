import re
import signal
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from markupsafe import escape

app = Flask(__name__)

PHOTOS_DIR = Path(__file__).resolve().parent.parent / "photos"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Held by whoever is talking to the camera: the liveview worker or a capture.
camera_lock = threading.Lock()
last_capture = None


class LiveView:
    """Owns the gphoto2 liveview process in a background thread.

    The camera stream is decoupled from HTTP clients: any number of browsers
    can watch /preview, and a browser disconnecting never touches the camera.

    gphoto2 only notices a signal between frames, and it blocks writing to
    stdout once the pipe fills (a frame is ~300 KB, the pipe holds 64 KB), so
    the worker keeps draining stdout until EOF even after asking it to stop.
    """

    STOP_GRACE = 3  # seconds to exit cleanly after SIGINT before SIGKILL

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.running = False
        self._ctl = threading.Lock()
        self._thread = None
        self._proc = None

    def start(self):
        """Start liveview. Returns False if the camera is busy with a capture."""
        with self._ctl:
            if self._thread and self._thread.is_alive():
                return True
            if not camera_lock.acquire(blocking=False):
                return False
            self._proc = subprocess.Popen(
                ["gphoto2", "--stdout", "--capture-movie"],
                stdout=subprocess.PIPE,
            )
            with self.cond:
                self.running = True
                self.frame = None
            self._thread = threading.Thread(target=self._run, args=(self._proc,), daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=STOP_GRACE + 2):
        """Stop liveview and wait until the camera is released."""
        with self._ctl:
            proc, thread = self._proc, self._thread
            if not thread:
                return
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                # Last resort if gphoto2 hangs without exiting; killing it
                # also closes the pipe so the worker's read returns EOF.
                threading.Timer(self.STOP_GRACE, lambda: proc.poll() is None and proc.kill()).start()
            thread.join(timeout)

    def _run(self, proc):
        buf = bytearray()
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break  # gphoto2 exited (stopped, killed, or camera error)
                search_from = max(len(buf) - 1, 0)
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        del buf[:-1]  # keep a trailing 0xFF in case FFD8 is split
                        break
                    end = buf.find(b"\xff\xd9", max(start + 2, search_from))
                    if end == -1:
                        del buf[:start]
                        break
                    frame = bytes(buf[start : end + 2])
                    del buf[: end + 2]
                    search_from = 0
                    with self.cond:
                        self.frame = frame
                        self.seq += 1
                        self.cond.notify_all()
        finally:
            proc.stdout.close()
            proc.wait()
            with self.cond:
                self.running = False
                self.frame = None
                self.cond.notify_all()
            camera_lock.release()


liveview = LiveView()


# Changes on every listener restart; open kiosk pages reload when it does so
# they never keep running stale JS after a deploy.
BOOT_ID = uuid.uuid4().hex


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok")


@app.route("/status", methods=["GET"])
def status():
    today_dir = PHOTOS_DIR / datetime.now().strftime("%Y-%m-%d")
    photos_today = len(list(today_dir.glob("*.jpg"))) if today_dir.is_dir() else 0
    return jsonify(photos_today=photos_today, last_capture=last_capture)


@app.route("/liveview/status", methods=["GET"])
def liveview_status():
    return jsonify(active=liveview.running, boot_id=BOOT_ID)


@app.route("/liveview/start", methods=["POST"])
def liveview_start():
    if not liveview.start():
        return jsonify(status="busy", error="capture in progress", active=False), 409
    return jsonify(status="ok", active=True)


@app.route("/liveview/stop", methods=["POST"])
def liveview_stop():
    liveview.stop()
    return jsonify(status="ok", active=False)


@app.route("/capture", methods=["POST"])
def capture():
    global last_capture

    # Free the camera from liveview (blocks until gphoto2 has exited), then
    # take the lock; a timeout here means another capture is still running.
    liveview.stop()

    if not camera_lock.acquire(timeout=10):
        return jsonify(status="busy", error="capture already in progress"), 409

    try:
        now = datetime.now()
        day_dir = PHOTOS_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = day_dir / f"{now.strftime('%H%M%S')}.jpg"

        result = subprocess.run(
            ["gphoto2", "--capture-image-and-download", "--filename", str(filename)],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # gphoto2 can exit 0 even after a failed capture (e.g. a PTP
        # timeout logged to stderr) — the file actually landing on disk is
        # the real signal of success, not just the exit code.
        if result.returncode != 0 or not filename.exists():
            error = result.stderr.strip() or "gphoto2 reported success but no file was written"
            last_capture = {"status": "error", "error": error, "at": now.isoformat()}
            return jsonify(last_capture), 500

        last_capture = {"status": "ok", "file": str(filename), "at": now.isoformat()}
        return jsonify(last_capture)
    except subprocess.TimeoutExpired:
        last_capture = {"status": "error", "error": "capture timed out", "at": now.isoformat()}
        return jsonify(last_capture), 504
    finally:
        camera_lock.release()


FAVICON = (
    '<link rel="icon" href="data:image/svg+xml,'
    '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22>'
    '<text y=%22.9em%22 font-size=%2290%22>📸</text></svg>">'
)


@app.route("/kiosk")
def kiosk():
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Picollo</title>
{FAVICON}
<style>
  html, body {{ margin: 0; height: 100%; background: #000; overflow: hidden; }}
  .preview {{ width: 100%; height: 100vh; object-fit: contain; display: block; }}
  .idle {{
    width: 100%; height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; color: #fff;
    font-family: system-ui, sans-serif; text-align: center;
  }}
  .idle .emoji {{ font-size: 4em; }}
  .banner {{
    position: fixed; left: 0; right: 0; bottom: 0;
    padding: 18px 24px;
    background: linear-gradient(to top, rgba(0,0,0,.75), transparent);
    color: #fff;
    font-family: system-ui, sans-serif;
    text-align: center;
  }}
  .banner h1 {{ margin: 0; font-size: 1.4em; font-weight: 600; letter-spacing: .03em; }}
  .banner p {{ margin: 4px 0 0; font-size: .95em; opacity: .85; }}
  .hidden {{ display: none; }}
</style>
</head>
<body>
  <div id="idleScreen" class="idle">
    <div class="emoji">📸</div>
    <h1>Picollo</h1>
    <p>Stiskni tlačítko a vyfoť se!</p>
  </div>
  <img id="previewImg" class="preview hidden" alt="Picollo live preview">
  <div id="banner" class="banner hidden">
    <h1>📸 Picollo</h1>
    <p>Dívej se a stiskni tlačítko znovu!</p>
  </div>

<script>
const BOOT_ID = '{BOOT_ID}';
let live = false;
const img = document.getElementById('previewImg');

// If the stream fails or ends while liveview should be on (e.g. first
// frame not ready yet), reconnect instead of leaving a black screen.
function reconnect() {{
  if (live) setTimeout(() => {{ if (live) img.src = '/preview?t=' + Date.now(); }}, 500);
}}
img.addEventListener('error', reconnect);

async function poll() {{
  try {{
    const r = await fetch('/liveview/status', {{ cache: 'no-store' }}).then(r => r.json());
    if (r.boot_id !== BOOT_ID) {{ location.reload(); return; }}
    if (r.active && !live) {{
      live = true;
      document.getElementById('previewImg').src = '/preview?t=' + Date.now();
      document.getElementById('previewImg').classList.remove('hidden');
      document.getElementById('banner').classList.remove('hidden');
      document.getElementById('idleScreen').classList.add('hidden');
    }} else if (!r.active && live) {{
      live = false;
      document.getElementById('previewImg').src = '';
      document.getElementById('previewImg').classList.add('hidden');
      document.getElementById('banner').classList.add('hidden');
      document.getElementById('idleScreen').classList.remove('hidden');
    }}
  }} catch (e) {{}}
}}

setInterval(poll, 700);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Picollo — řídicí panel</title>
{FAVICON}
<style>
  body {{
    margin: 0; padding: 32px 20px 60px; background: #111; color: #eee;
    font-family: system-ui, sans-serif; display: flex; flex-direction: column;
    align-items: center;
  }}
  h1 {{ margin: 0 0 4px; }}
  .sub {{ opacity: .6; margin-bottom: 28px; }}
  .card {{
    background: #1c1c1c; border-radius: 12px; padding: 20px 24px;
    width: 100%; max-width: 480px; margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,.4);
  }}
  .card h2 {{ margin: 0 0 12px; font-size: 1em; opacity: .7; text-transform: uppercase; letter-spacing: .05em; }}
  .row {{ display: flex; justify-content: space-between; padding: 4px 0; }}
  .row span:last-child {{ opacity: .85; }}
  #shoot {{
    display: block; width: 100%; max-width: 480px; padding: 18px;
    font-size: 1.2em; font-weight: 600; border: none; border-radius: 12px;
    background: #e8453c; color: #fff; cursor: pointer; margin-bottom: 20px;
  }}
  #shoot:disabled {{ background: #555; cursor: default; }}
  #result {{ min-height: 1.4em; margin-bottom: 20px; opacity: .9; }}
  ul {{ margin: 0; padding-left: 20px; }}
  li {{ margin-bottom: 6px; }}
  a {{ color: #8ab4ff; }}
</style>
</head>
<body>
  <h1>📸 Picollo</h1>
  <div class="sub">řídicí panel</div>

  <div class="card">
    <h2>Stav</h2>
    <div class="row"><span>Listener</span><span id="st-listener">…</span></div>
    <div class="row"><span>Liveview</span><span id="st-live">…</span></div>
    <div class="row"><span>Fotek dnes</span><span id="st-today">…</span></div>
    <div class="row"><span>Poslední capture</span><span id="st-last">…</span></div>
  </div>

  <button id="shoot">Vyfotit</button>
  <div id="result"></div>

  <div class="card">
    <h2>Jak to funguje</h2>
    <ul>
      <li>1. stisk tlačítka (ESP32-C3) → spustí liveview na kiosk displeji.</li>
      <li>2. stisk tlačítka → vyfotí (liveview se automaticky vypne).</li>
      <li>Bez 2. stisku se liveview po chvíli sám vypne.</li>
      <li>Fotka se uloží na NUC do <code>photos/RRRR-MM-DD/HHMMSS.jpg</code>.</li>
      <li>Tlačítko "Vyfotit" výše dělá totéž jako fyzický 2. stisk — pro test.</li>
      <li>Upload/tisk fotek ještě nejsou zapojené (plánováno později).</li>
    </ul>
  </div>

  <div class="card">
    <h2>Odkazy</h2>
    <ul>
      <li><a href="/kiosk">/kiosk</a> — fullscreen náhled pro hosty</li>
      <li><a href="/gallery">/gallery</a> — galerie pořízených fotek</li>
      <li><a href="/health">/health</a> — liveness check</li>
      <li><a href="/status">/status</a> — stav v JSON</li>
    </ul>
  </div>

<script>
async function refreshStatus() {{
  try {{
    const h = await fetch('/health').then(r => r.json());
    document.getElementById('st-listener').textContent = h.status === 'ok' ? 'OK' : 'chyba';
  }} catch (e) {{
    document.getElementById('st-listener').textContent = 'nedostupný';
  }}
  try {{
    const lv = await fetch('/liveview/status').then(r => r.json());
    document.getElementById('st-live').textContent = lv.active ? 'aktivní' : 'vypnuté';
  }} catch (e) {{}}
  try {{
    const s = await fetch('/status').then(r => r.json());
    document.getElementById('st-today').textContent = s.photos_today;
    if (s.last_capture) {{
      const time = new Date(s.last_capture.at).toLocaleTimeString();
      document.getElementById('st-last').textContent =
        (s.last_capture.status === 'ok' ? '✅ ' : '⚠️ ') + time;
    }} else {{
      document.getElementById('st-last').textContent = 'zatím žádný';
    }}
  }} catch (e) {{}}
}}

document.getElementById('shoot').addEventListener('click', async () => {{
  const btn = document.getElementById('shoot');
  const result = document.getElementById('result');
  btn.disabled = true;
  result.textContent = 'Fotím…';
  try {{
    const r = await fetch('/capture', {{ method: 'POST' }});
    const data = await r.json();
    result.textContent = r.ok ? ('✅ Hotovo: ' + data.file) : ('⚠️ ' + (data.error || 'chyba'));
  }} catch (e) {{
    result.textContent = '⚠️ Listener nedostupný';
  }}
  btn.disabled = false;
  refreshStatus();
}});

refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>"""


def list_photos_by_day():
    """[(date, [photo paths])], newest day and newest photo first."""
    days = []
    if PHOTOS_DIR.is_dir():
        for day_dir in sorted(PHOTOS_DIR.iterdir(), reverse=True):
            if not day_dir.is_dir() or not DATE_RE.match(day_dir.name):
                continue
            photos = sorted(day_dir.glob("*.jpg"), reverse=True)
            if photos:
                days.append((day_dir.name, photos))
    return days


@app.route("/gallery")
def gallery():
    days = list_photos_by_day()

    sections = ""
    for day, photos in days:
        thumbs = "".join(
            f'<a href="/gallery/{day}/{p.name}">'
            f'<img src="/photos/{day}/{p.name}" loading="lazy"></a>'
            for p in photos
        )
        sections += f'<h2>{day}</h2><div class="grid">{thumbs}</div>'

    if not sections:
        sections = '<p class="empty">Zatím žádné fotky.</p>'

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Picollo — galerie</title>
{FAVICON}
<style>
  body {{
    margin: 0; padding: 32px 20px 60px; background: #111; color: #eee;
    font-family: system-ui, sans-serif;
  }}
  h1 {{ margin: 0 0 4px; }}
  .sub {{ opacity: .6; margin-bottom: 28px; }}
  .sub a {{ color: #8ab4ff; }}
  h2 {{ opacity: .7; font-size: 1em; text-transform: uppercase; letter-spacing: .05em; margin: 24px 0 10px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 10px;
  }}
  .grid img {{
    width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 8px;
    display: block; background: #222;
  }}
  .empty {{ opacity: .6; }}
</style>
</head>
<body>
  <h1>📸 Picollo</h1>
  <div class="sub">galerie &middot; <a href="/">zpět na řídicí panel</a></div>
  {sections}
</body>
</html>"""


@app.route("/gallery/<date>/<filename>")
def gallery_photo(date, filename):
    if not DATE_RE.match(date) or not (PHOTOS_DIR / date / filename).is_file():
        abort(404)
    src = f"/photos/{date}/{escape(filename)}"

    # Neighbours in gallery order: "newer" is left/previous, "older" right/next.
    order = [(day, p.name) for day, photos in list_photos_by_day() for p in photos]
    i = order.index((date, filename)) if (date, filename) in order else -1
    newer = f"/gallery/{order[i - 1][0]}/{escape(order[i - 1][1])}" if i > 0 else ""
    older = f"/gallery/{order[i + 1][0]}/{escape(order[i + 1][1])}" if 0 <= i < len(order) - 1 else ""
    position = f"{i + 1} / {len(order)}" if i >= 0 else ""

    def arrow(href, label, cls):
        if href:
            return f'<a class="nav {cls}" href="{href}" aria-label="{label}">{"‹" if cls == "prev" else "›"}</a>'
        return f'<span class="nav {cls} off" aria-hidden="true">{"‹" if cls == "prev" else "›"}</span>'
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Picollo — {date} {escape(filename)}</title>
{FAVICON}
<style>
  body {{
    margin: 0; padding: 20px; background: #111; color: #eee;
    font-family: system-ui, sans-serif; box-sizing: border-box;
    min-height: 100vh; display: flex; flex-direction: column;
  }}
  .bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }}
  .bar a {{ color: #8ab4ff; text-decoration: none; }}
  .meta {{ opacity: .6; }}
  .photo {{ flex: 1; display: flex; align-items: center; justify-content: center; gap: 12px; min-height: 0; }}
  .photo img {{ min-width: 0; max-width: calc(100% - 120px); max-height: calc(100vh - 90px); border-radius: 8px; }}
  .nav {{
    flex: none; width: 48px; height: 48px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: #1c1c1c; color: #eee; font-size: 2em; line-height: 1;
    text-decoration: none; user-select: none;
  }}
  a.nav:hover {{ background: #2a2a2a; }}
  .nav.off {{ opacity: .2; }}
</style>
</head>
<body>
  <div class="bar">
    <a href="/gallery">← zpět do galerie</a>
    <span class="meta">{date} · {escape(filename)} · {position}</span>
    <a href="{src}" target="_blank">originál</a>
  </div>
  <div class="photo">
    {arrow(newer, "Novější fotka", "prev")}
    <img src="{src}" alt="Fotka {date} {escape(filename)}">
    {arrow(older, "Starší fotka", "next")}
  </div>
<script>
// Keyboard: ←/→ between photos, Esc back to the gallery.
document.addEventListener('keydown', e => {{
  const go = {{ ArrowLeft: '{newer}', ArrowRight: '{older}', Escape: '/gallery' }}[e.key];
  if (go) location.href = go;
}});
</script>
</body>
</html>"""


@app.route("/photos/<date>/<filename>")
def serve_photo(date, filename):
    if not DATE_RE.match(date):
        abort(404)
    return send_from_directory(PHOTOS_DIR / date, filename)


@app.route("/preview")
def preview():
    # Just a viewer: streams the worker's latest frames. Any number of
    # clients can watch; disconnecting has no effect on the camera.
    if not liveview.running:
        return jsonify(status="idle", error="liveview not running"), 503

    def generate():
        last_seq = -1
        while True:
            with liveview.cond:
                liveview.cond.wait_for(
                    lambda: liveview.seq != last_seq or not liveview.running, timeout=5
                )
                if not liveview.running:
                    return
                frame, last_seq = liveview.frame, liveview.seq
            if frame is None:
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame
                + b"\r\n"
            )

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/background", methods=["POST"])
def background():
    # Placeholder for the tablet background-selection input source.
    # Chroma-key/background-replacement pipeline isn't implemented yet.
    data = request.get_json(silent=True) or {}
    return jsonify(status="ok", received=data.get("background"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
