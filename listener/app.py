import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from markupsafe import escape

app = Flask(__name__)

PHOTOS_DIR = Path(__file__).resolve().parent.parent / "photos"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

last_capture = None


class CameraError(Exception):
    pass


class Camera:
    """One long-lived `gphoto2 --shell` session that owns the camera.

    Spawning gphoto2 per photo meant reconnecting to the camera every time
    and first tearing down the liveview process: ~0.9 s from request to
    shutter. Keeping a single session open and shooting straight out of
    liveview brings that down to ~0.25 s.

    A worker thread runs every command, so the camera is only ever driven
    from one place: it loops `capture-preview` while liveview is on and
    services capture requests in between. HTTP clients only read the latest
    frame, so any number of viewers can watch /preview.
    """

    MARKER = "Local directory now"  # printed by `lcd .`, appended to every command
    PREVIEW_TIMEOUT = 5
    CAPTURE_TIMEOUT = 20

    def __init__(self):
        self.cond = threading.Condition()  # guards the fields below
        self.frame = None
        self.seq = 0
        self._liveview = False
        self._capture_req = None
        shm = Path("/dev/shm")
        self._workdir = Path(tempfile.mkdtemp(prefix="picollo-", dir=shm if shm.is_dir() else None))
        self._proc = None
        self._lines = None
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- public API (called from request threads) ----

    @property
    def running(self):
        return self._liveview

    def start_liveview(self):
        with self.cond:
            self._liveview = True
            self.cond.notify_all()

    def stop_liveview(self):
        with self.cond:
            self._liveview = False
            self.frame = None
            self.cond.notify_all()

    def capture(self, dest):
        """Take a photo into `dest`. Returns None on success, else an error string."""
        req = {"dest": dest, "done": threading.Event(), "error": None}
        with self.cond:
            if self._capture_req is not None:
                return "busy"
            self._capture_req = req
            # Ends liveview on the kiosk; the worker shoots straight out of
            # liveview without leaving it first, which is what makes it fast.
            self._liveview = False
            self.frame = None
            self.cond.notify_all()
        if not req["done"].wait(self.CAPTURE_TIMEOUT + 5):
            return "capture timed out"
        return req["error"]

    # ---- worker ----

    def _worker(self):
        viewfinder_on = False
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self._capture_req or self._liveview or viewfinder_on)
                req, want_liveview = self._capture_req, self._liveview
            try:
                if req:
                    try:
                        self._capture_to(req["dest"])
                    except CameraError as e:
                        req["error"] = str(e)
                    finally:
                        with self.cond:
                            self._capture_req = None
                        req["done"].set()
                elif want_liveview:
                    self._run("capture-preview", self.PREVIEW_TIMEOUT)
                    viewfinder_on = True
                    frame = (self._workdir / "capture_preview.jpg").read_bytes()
                    with self.cond:
                        if self._liveview:
                            self.frame = frame
                            self.seq += 1
                            self.cond.notify_all()
                    continue
                if viewfinder_on:
                    # Liveview ended: drop the mirror so the camera isn't left
                    # with its sensor exposed and the screen on.
                    viewfinder_on = False
                    self._run("set-config viewfinder=0", self.PREVIEW_TIMEOUT)
            except (CameraError, OSError) as e:
                app.logger.warning("camera: %s", e)
                self._kill()
                viewfinder_on = False
                time.sleep(1)  # don't spin if the camera is unplugged/off

    def _capture_to(self, dest):
        # A session that died (camera auto-off, replugged) fails the first
        # command; retry once on a fresh session if no photo was taken.
        for attempt in (1, 2):
            for old in self._workdir.glob("capt*"):
                old.unlink()
            try:
                self._run("capture-image-and-download", self.CAPTURE_TIMEOUT)
            except CameraError:
                if attempt == 2 or list(self._workdir.glob("capt*")):
                    raise
                self._kill()
                continue
            shots = sorted(self._workdir.glob("capt*"))
            if not shots:
                raise CameraError("gphoto2 reported success but no file was downloaded")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(shots[-1]), dest)
            return

    def _run(self, command, timeout):
        """Run one shell command; raises CameraError on gphoto2 errors."""
        self._ensure_session()
        self._proc.stdin.write(f"{command}\nlcd .\n")
        self._proc.stdin.flush()
        out, deadline = [], time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                self._kill()
                raise CameraError(f"'{command}' timed out after {timeout}s")
            if line is None:
                self._kill()
                raise CameraError(f"gphoto2 exited during '{command}': {''.join(out).strip()[-300:]}")
            if self.MARKER in line:
                break
            out.append(line)
        errors = [l.strip() for l in out if "*** Error" in l or "ERROR:" in l]
        if errors:
            raise CameraError(f"'{command}': " + " ".join(errors))

    def _ensure_session(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["gphoto2", "--shell", "--force-overwrite"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self._workdir, text=True, errors="replace", bufsize=1,
        )
        lines = self._lines = queue.Queue()

        def pump(stream):
            for line in stream:
                lines.put(line)
            lines.put(None)  # EOF

        threading.Thread(target=pump, args=(self._proc.stdout,), daemon=True).start()

    def _kill(self):
        if self._proc:
            self._proc.kill()
            self._proc.wait()
            self._proc = None


camera = Camera()


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
    return jsonify(active=camera.running, boot_id=BOOT_ID)


@app.route("/liveview/start", methods=["POST"])
def liveview_start():
    camera.start_liveview()
    return jsonify(status="ok", active=True)


@app.route("/liveview/stop", methods=["POST"])
def liveview_stop():
    camera.stop_liveview()
    return jsonify(status="ok", active=False)


@app.route("/capture", methods=["POST"])
def capture():
    global last_capture

    now = datetime.now()
    filename = PHOTOS_DIR / now.strftime("%Y-%m-%d") / f"{now.strftime('%H%M%S')}.jpg"

    error = camera.capture(filename)
    if error == "busy":
        return jsonify(status="busy", error="capture already in progress"), 409
    if error:
        last_capture = {"status": "error", "error": error, "at": now.isoformat()}
        return jsonify(last_capture), 504 if "timed out" in error else 500

    last_capture = {"status": "ok", "file": str(filename), "at": now.isoformat()}
    return jsonify(last_capture)


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
    if not camera.running:
        return jsonify(status="idle", error="liveview not running"), 503

    def generate():
        last_seq = -1
        while True:
            with camera.cond:
                camera.cond.wait_for(
                    lambda: camera.seq != last_seq or not camera.running, timeout=5
                )
                if not camera.running:
                    return
                frame, last_seq = camera.frame, camera.seq
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
