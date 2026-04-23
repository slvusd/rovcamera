#!/bin/bash

# ============================================================
# ROV Pi — Master Install Script
# Sets up all services on the Raspberry Pi 5
#
# Installs / reinstalls:
#   1. mediamtx        — RTSP/WebRTC camera server  (port 8554/8889)
#   2. rov-stats       — Pi health monitor dashboard (port 9000)
#   3. rov-ui          — Flask camera UI + capture   (port 8080)
#
# Safe to re-run: stops each service, updates files, restarts.
#
# Usage:
#   ./install.sh              # install everything
#   ./install.sh mediamtx     # reinstall just mediamtx
#   ./install.sh stats        # reinstall just rov-stats
#   ./install.sh ui           # reinstall just rov-ui
#
# Assumes repo is at /home/pi/rovcamera/ with structure:
#   mediamtx/   mediamtx binary + mediamtx.yml
#   stats/      stats.py  (copied here by this script)
#   ui/         app.py, templates/, static/
#   science/    rov_pipeline.sh
#   install.sh  (this file)
# ============================================================

set -e

REPO="/home/pi/rovcamera"
USER="pi"

log()  { echo ""; echo "━━━ $1 ━━━"; }
ok()   { echo "  ✅ $1"; }
warn() { echo "  ⚠️  $1"; }
die()  { echo "  ❌ $1"; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "This script runs on the Pi only."
[ -d "$REPO" ]               || die "Repo not found at $REPO"

INSTALL_TARGET="${1:-all}"

# ── Helper: stop + start a service ───────────────────────────

service_install() {
    local name="$1"
    local unit_file="$2"
    local desc="$3"

    echo ""
    echo "  Writing /etc/systemd/system/${name}.service..."
    sudo tee "/etc/systemd/system/${name}.service" > /dev/null << EOF
$unit_file
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable  "$name"
    sudo systemctl restart "$name"
    sleep 2
    if systemctl is-active --quiet "$name"; then
        ok "$name is running."
    else
        warn "$name failed to start. Check: journalctl -u $name -n 30"
    fi
}

service_stop() {
    local name="$1"
    if systemctl is-active --quiet "$name" 2>/dev/null; then
        echo "  Stopping $name..."
        sudo systemctl stop "$name"
    fi
}

# ════════════════════════════════════════════════════════════
# 1. mediamtx
# ════════════════════════════════════════════════════════════

install_mediamtx() {
    log "mediamtx (RTSP/WebRTC server)"

    MEDIAMTX_DIR="$REPO/mediamtx"
    [ -f "$MEDIAMTX_DIR/mediamtx" ]     || die "mediamtx binary not found at $MEDIAMTX_DIR/mediamtx"
    [ -f "$MEDIAMTX_DIR/mediamtx.yml" ] || die "mediamtx.yml not found at $MEDIAMTX_DIR/mediamtx.yml"

    service_stop "mediamtx"
    # Kill any stray ffmpeg from previous run
    pkill -f "ffmpeg.*v4l2" 2>/dev/null || true
    sleep 1

    service_install "mediamtx" \
"[Unit]
Description=MediaMTX RTSP/WebRTC server
After=network.target
Wants=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${MEDIAMTX_DIR}
ExecStartPre=/bin/sleep 3
ExecStart=${MEDIAMTX_DIR}/mediamtx ${MEDIAMTX_DIR}/mediamtx.yml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target" \
        "mediamtx"

    ok "mediamtx listening on :8554 (RTSP) :8889 (WebRTC)"
}

# ════════════════════════════════════════════════════════════
# 2. rov-stats
# ════════════════════════════════════════════════════════════

install_stats() {
    log "rov-stats (Pi health dashboard, port 9000)"

    STATS_SRC="$REPO/stats/stats.py"
    STATS_DIR="$REPO/stats"
    [ -f "$STATS_SRC" ] || die "stats.py not found at $STATS_SRC"

    command -v python3 &>/dev/null || die "python3 not found"

    service_stop "rov-stats"

    mkdir -p "$STATS_DIR"

    # Create venv with system-site-packages so rclpy is visible
    if [ ! -x "$STATS_DIR/venv/bin/python3" ]; then
        [ -d "$STATS_DIR/venv" ] && rm -rf "$STATS_DIR/venv"
        echo "  Creating venv..."
        python3 -m venv --system-site-packages "$STATS_DIR/venv"
        ok "venv created."
    else
        ok "venv already exists."
    fi

    # Write a wrapper that sources ROS2 env before starting Python.
    # This sets LD_LIBRARY_PATH, PYTHONPATH, etc. that rclpy C extensions need.
    WRAPPER="$STATS_DIR/start_stats.sh"
    cat > "$WRAPPER" << 'WRAPPER_EOF'
#!/bin/bash
[ -f /opt/ros/jazzy/setup.bash ]          && source /opt/ros/jazzy/setup.bash
[ -f /home/pi/slvrov_ros/install/setup.bash ] && source /home/pi/slvrov_ros/install/setup.bash
export ROS_DOMAIN_ID=42
[ -f /home/pi/fastdds_config.xml ] && export FASTRTPS_DEFAULT_PROFILES_FILE=/home/pi/fastdds_config.xml
exec /home/pi/rovcamera/stats/venv/bin/python3 /home/pi/rovcamera/stats/stats.py
WRAPPER_EOF
    chmod +x "$WRAPPER"
    ok "Wrapper script written: $WRAPPER"

    service_install "rov-stats" \
"[Unit]
Description=ROV Pi Stats Monitor
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${STATS_DIR}
ExecStart=${WRAPPER}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target" \
        "rov-stats"

    ok "Stats dashboard at http://$(hostname -I | awk '{print $1}'):9000/"
}

# ════════════════════════════════════════════════════════════
# 3. rov-ui (Flask)
# ════════════════════════════════════════════════════════════

install_ui() {
    log "rov-ui (Flask camera UI, port 8080)"

    UI_DIR="$REPO/ui"
    [ -f "$UI_DIR/app.py" ]           || die "app.py not found at $UI_DIR/app.py"
    [ -f "$UI_DIR/requirements.txt" ]  || die "requirements.txt not found at $UI_DIR/requirements.txt"

    service_stop "rov-ui"

    echo "  Rebuilding venv from system Python..."
    rm -rf "$UI_DIR/venv"
    /usr/bin/python3 -m venv "$UI_DIR/venv"
    "$UI_DIR/venv/bin/pip" install --quiet -r "$UI_DIR/requirements.txt"
    ok "Dependencies installed: $("$UI_DIR/venv/bin/pip" show flask | grep Version)"

    service_install "rov-ui" \
"[Unit]
Description=ROV Camera UI
After=network.target mediamtx.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${UI_DIR}
ExecStart=${UI_DIR}/venv/bin/python3 ${UI_DIR}/app.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target" \
        "rov-ui"

    ok "UI available at http://$(hostname -I | awk '{print $1}'):8080/"
}

# ════════════════════════════════════════════════════════════
# Dispatch
# ════════════════════════════════════════════════════════════

install_pi4() {
    log "Pi 4 relay — mediamtx (pulls from ROV) + rov-ui"

    MEDIAMTX_DIR="$REPO/mediamtx"
    [ -f "$MEDIAMTX_DIR/mediamtx" ]         || die "mediamtx binary not found at $MEDIAMTX_DIR/mediamtx"
    [ -f "$MEDIAMTX_DIR/mediamtx-pi4.yml" ] || die "mediamtx-pi4.yml not found at $MEDIAMTX_DIR/mediamtx-pi4.yml"

    service_stop "mediamtx"

    service_install "mediamtx" \
"[Unit]
Description=MediaMTX relay (Pi 4 — pulls from ROV Pi 5)
After=network.target
Wants=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${MEDIAMTX_DIR}
ExecStartPre=/bin/sleep 3
ExecStart=${MEDIAMTX_DIR}/mediamtx ${MEDIAMTX_DIR}/mediamtx-pi4.yml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target" \
        "mediamtx"

    ok "mediamtx relay listening on :8889 (WebRTC) — pulling from 192.168.3.52"

    install_ui
}

case "$INSTALL_TARGET" in
    all)
        install_mediamtx
        install_stats
        install_ui
        ;;
    mediamtx) install_mediamtx ;;
    stats)    install_stats    ;;
    ui)       install_ui       ;;
    pi4)      install_pi4      ;;
    *)
        echo ""
        echo "Usage: $0 [all|mediamtx|stats|ui|pi4]"
        echo ""
        echo "  all       Install/reinstall everything (ROV Pi 5, default)"
        echo "  mediamtx  Reinstall mediamtx service only"
        echo "  stats     Reinstall rov-stats service only"
        echo "  ui        Reinstall rov-ui (Flask) service only"
        echo "  pi4       Install Pi 4 surface relay (mediamtx + ui, no cameras)"
        echo ""
        exit 1
        ;;
esac

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

IP=$(hostname -I | awk '{print $1}')

echo ""
echo "════════════════════════════════════════"
echo " Install complete!"
echo "════════════════════════════════════════"
echo ""
echo "  Camera UI:   http://${IP}:8080/"
echo "  Stats:       http://${IP}:9000/"
echo "  RTSP stream: rtsp://${IP}:8554/cam1"
echo ""
echo "  Logs:"
echo "    journalctl -u mediamtx -f"
echo "    journalctl -u rov-stats -f"
echo "    journalctl -u rov-ui    -f"
echo ""
echo "  Restart a service:"
echo "    sudo systemctl restart mediamtx"
echo "    sudo systemctl restart rov-stats"
echo "    sudo systemctl restart rov-ui"
echo ""
