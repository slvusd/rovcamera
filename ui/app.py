import os
import signal
import socket
import subprocess
import glob
from flask import Flask, render_template, jsonify, send_from_directory, request

app = Flask(__name__)

# RTSP host selected by client subnet — edit the values to reconfigure
MEDIAMTX_HOST_BY_SUBNET = {
    "192.168.3": "192.168.3.52",
    "192.168.0": "192.168.0.8",
}
MEDIAMTX_PORT = 8889
MEDIAMTX_HLS_PORT = 8888


def _reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _detect_default_host() -> str:
    """Return the first configured host reachable on MEDIAMTX_PORT, or the first entry."""
    for host in MEDIAMTX_HOST_BY_SUBNET.values():
        if _reachable(host, MEDIAMTX_PORT):
            return host
    return next(iter(MEDIAMTX_HOST_BY_SUBNET.values()))


MEDIAMTX_HOST_DEFAULT = _detect_default_host()
print(f"[mediamtx] default host → {MEDIAMTX_HOST_DEFAULT}:{MEDIAMTX_PORT}")


def mediamtx_host() -> str:
    client_ip = request.remote_addr or ""
    subnet = ".".join(client_ip.split(".")[:3])
    return MEDIAMTX_HOST_BY_SUBNET.get(subnet, MEDIAMTX_HOST_DEFAULT)

# Where the pipeline script lives — adjust if needed
PIPELINE_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "science", "rov_pipeline.sh")

# PID files per camera so each cam can capture independently
PID_DIR = os.path.expanduser("~/.rov_pids")
os.makedirs(PID_DIR, exist_ok=True)

# All captured frames live here, organised by camera
SESSION_BASE = os.path.expanduser("~/rov_sessions")


def pid_file(cam: str) -> str:
    return os.path.join(PID_DIR, f"{cam}.pid")


def is_capturing(cam: str) -> bool:
    pf = pid_file(cam)
    if not os.path.exists(pf):
        return False
    try:
        pid = int(open(pf).read().strip())
        os.kill(pid, 0)   # signal 0 = just check existence
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        os.remove(pf)
        return False


def latest_session_frames(cam: str) -> list[str]:
    """Return sorted list of /rov_sessions/<latest>_<cam>/frames/*.jpg web paths."""
    pattern = os.path.join(SESSION_BASE, f"*_{cam}", "frames", "*.jpg")
    files = sorted(glob.glob(pattern))
    # Return paths relative to SESSION_BASE so we can serve them
    return [f"/sessions/{os.path.relpath(f, SESSION_BASE)}" for f in files]


def latest_blend(cam: str) -> str | None:
    pattern = os.path.join(SESSION_BASE, f"*_{cam}", "*.blend")
    files = sorted(glob.glob(pattern))
    return f"/sessions/{os.path.relpath(files[-1], SESSION_BASE)}" if files else None


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", host=mediamtx_host(), port=MEDIAMTX_PORT)


# ── Static session files (frames + blend previews) ─────────────────────────────

@app.route("/sessions/<path:filename>")
def session_file(filename):
    return send_from_directory(SESSION_BASE, filename)


# ── Capture control ────────────────────────────────────────────────────────────

@app.route("/capture/<cam>/start", methods=["POST"])
def capture_start(cam):
    if is_capturing(cam):
        return jsonify({"status": "already_capturing", "cam": cam}), 409

    pf = pid_file(cam)
    proc = subprocess.Popen(
        ["bash", PIPELINE_SCRIPT, "capture", "start", "-c", cam],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    proc.wait()  # script exits quickly after spawning ffmpeg in background
    # PID file is written by the script itself
    _ = pf  # pf still used by is_capturing()

    return jsonify({"status": "capturing", "cam": cam, "pid": proc.pid})


@app.route("/capture/<cam>/stop", methods=["POST"])
def capture_stop(cam):
    pf = pid_file(cam)
    if not os.path.exists(pf):
        return jsonify({"status": "not_capturing", "cam": cam}), 404

    try:
        pid = int(open(pf).read().strip())
        os.killpg(os.getpgid(pid), signal.SIGINT)   # SIGINT = clean ffmpeg shutdown
    except (ValueError, ProcessLookupError):
        pass
    finally:
        if os.path.exists(pf):
            os.remove(pf)

    frames = latest_session_frames(cam)
    return jsonify({"status": "stopped", "cam": cam, "frame_count": len(frames)})


@app.route("/capture/<cam>/status")
def capture_status(cam):
    frames = latest_session_frames(cam)
    return jsonify({
        "cam": cam,
        "capturing": is_capturing(cam),
        "frame_count": len(frames),
    })


# ── Snapshot (single frame) ────────────────────────────────────────────────────

_HLS_OK = "ok"
_HLS_INACTIVE = "inactive"   # 404 — stream not publishing, don't bother with RTSP
_HLS_ERR = "err"             # network/timeout — worth trying RTSP as fallback


def _snapshot_via_hls(cam: str, out_path: str) -> str:
    """Fetch the MediaMTX HLS thumbnail. Returns one of _HLS_* constants."""
    import urllib.request, urllib.error
    import time as _time
    url = f"http://{MEDIAMTX_HOST_DEFAULT}:{MEDIAMTX_HLS_PORT}/{cam}/thumbnail.jpg"
    t0 = _time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            ct = resp.headers.get_content_type()
            elapsed = _time.monotonic() - t0
            app.logger.info("HLS thumbnail cam=%s status=%d ct=%s elapsed=%.2fs", cam, resp.status, ct, elapsed)
            if resp.status == 200 and ct == "image/jpeg":
                with open(out_path, "wb") as f:
                    f.write(resp.read())
                return _HLS_OK
    except urllib.error.HTTPError as e:
        elapsed = _time.monotonic() - t0
        if e.code == 404:
            app.logger.info("HLS thumbnail 404 (stream inactive) cam=%s elapsed=%.2fs", cam, elapsed)
            return _HLS_INACTIVE
        app.logger.warning("HLS thumbnail failed cam=%s elapsed=%.2fs err=%s", cam, elapsed, e)
    except Exception as e:
        app.logger.warning("HLS thumbnail failed cam=%s elapsed=%.2fs err=%s", cam, _time.monotonic() - t0, e)
    return _HLS_ERR


def _snapshot_via_rtsp(cam: str, out_path: str) -> tuple[bool, str]:
    """Grab one frame over RTSP with ffmpeg. Returns (success, error_detail)."""
    rtsp_url = f"rtsp://{MEDIAMTX_HOST_DEFAULT}:8554/{cam}"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-timeout", "5000000",
                "-i", rtsp_url,
                "-frames:v", "1",
                "-q:v", "2",
                out_path,
            ],
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, f"RTSP timed out: {rtsp_url}"
    if result.returncode != 0 or not os.path.exists(out_path):
        return False, result.stderr.decode()[-600:]
    return True, ""


@app.route("/snapshot/<cam>", methods=["POST"])
def snapshot(cam):
    import time

    out_dir = os.path.join(SESSION_BASE, f"snapshots_{cam}")
    os.makedirs(out_dir, exist_ok=True)
    filename = f"snap_{int(time.time())}.jpg"
    out_path = os.path.join(out_dir, filename)

    hls = _snapshot_via_hls(cam, out_path)
    if hls == _HLS_OK:
        method = "hls"
    elif hls == _HLS_INACTIVE:
        return jsonify({"status": "error", "detail": f"stream {cam} is not active"}), 503
    else:
        ok, detail = _snapshot_via_rtsp(cam, out_path)
        if not ok:
            app.logger.error("snapshot failed cam=%s\n%s", cam, detail)
            return jsonify({"status": "error", "detail": detail}), 500
        method = "rtsp"

    web_path = f"/sessions/snapshots_{cam}/{filename}"
    return jsonify({"status": "ok", "cam": cam, "path": web_path, "method": method})


@app.route("/snapshot/<cam>/upload", methods=["POST"])
def snapshot_upload(cam):
    import time
    out_dir = os.path.join(SESSION_BASE, f"snapshots_{cam}")
    os.makedirs(out_dir, exist_ok=True)
    filename = f"snap_{int(time.time())}.jpg"
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "wb") as f:
        f.write(request.get_data())
    return jsonify({"status": "ok", "cam": cam, "path": f"/sessions/snapshots_{cam}/{filename}"})


# ── Gallery data ───────────────────────────────────────────────────────────────

@app.route("/gallery/<cam>")
def gallery(cam):
    frames = latest_session_frames(cam)
    # Also include any one-off snapshots
    snap_pattern = os.path.join(SESSION_BASE, f"snapshots_{cam}", "*.jpg")
    snaps = sorted(glob.glob(snap_pattern))
    snap_paths = [f"/sessions/{os.path.relpath(f, SESSION_BASE)}" for f in snaps]
    blend = latest_blend(cam)
    return jsonify({
        "cam": cam,
        "frames": frames + snap_paths,
        "blend": blend,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True, threaded=True)
