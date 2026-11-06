import subprocess
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

PHOTOS_DIR = Path(__file__).resolve().parent.parent / "photos"
capture_lock = threading.Lock()


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok")


@app.route("/capture", methods=["POST"])
def capture():
    if not capture_lock.acquire(blocking=False):
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

        if result.returncode != 0:
            return jsonify(status="error", error=result.stderr.strip()), 500

        return jsonify(status="ok", file=str(filename))
    except subprocess.TimeoutExpired:
        return jsonify(status="error", error="capture timed out"), 504
    finally:
        capture_lock.release()


@app.route("/")
def index():
    return (
        '<html><body style="margin:0;background:#000">'
        '<img src="/preview" style="width:100%;height:100vh;object-fit:contain">'
        "</body></html>"
    )


@app.route("/preview")
def preview():
    if not capture_lock.acquire(blocking=False):
        return jsonify(status="busy", error="camera in use"), 409

    def generate():
        proc = subprocess.Popen(
            ["gphoto2", "--stdout", "--capture-movie"],
            stdout=subprocess.PIPE,
        )
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        break
                    frame = buf[start : end + 2]
                    buf = buf[end + 2 :]
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            capture_lock.release()

    return Response(
        stream_with_context(generate()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/background", methods=["POST"])
def background():
    # Placeholder for the tablet background-selection input source.
    # Chroma-key/background-replacement pipeline isn't implemented yet.
    data = request.get_json(silent=True) or {}
    return jsonify(status="ok", received=data.get("background"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
