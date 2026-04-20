#!/bin/bash

# ============================================================
# ROV Capture → COLMAP → Blender Pipeline
# Mac M2 Air | mediamtx RTSP
#
# Usage:
#   ./rov_pipeline.sh capture      # capture with defaults
#   ./rov_pipeline.sh reconstruct  # run COLMAP on latest session
#   ./rov_pipeline.sh all          # capture then reconstruct
#
# Options (can be used with any command):
#   -i <ip>        ROV IP address         (default: 192.168.3.41)
#   -c <name>      Camera/stream name     (default: cam1)
#   -p <port>      RTSP port              (default: 8554)
#   -r <rate>      Capture rate in fps    (default: 0.5 = 1 frame/2sec)
#   -s <path>      Reconstruct a specific session folder
#
# Examples:
#   ./rov_pipeline.sh capture -i 192.168.3.52 -c cam1 -r 1
#   ./rov_pipeline.sh capture -r 2
#   ./rov_pipeline.sh reconstruct -s ~/rov_sessions/20240501_143022
#   ./rov_pipeline.sh all -i 192.168.3.52
#
# Requirements: ffmpeg and colmap installed via Homebrew
# ============================================================

set -e

# ----------------------------------------
# DEFAULTS — change these to suit your setup
# ----------------------------------------
DEFAULT_ROV_IP="192.168.3.41"
DEFAULT_STREAM="cam1"
DEFAULT_PORT="8554"
DEFAULT_RATE="0.5"    # frames per second
                      # 0.5 = 1 frame every 2s  → relaxed orbit
                      # 1   = 1 frame per second → standard
                      # 2   = 2 frames per second → dense / fast movement

# ----------------------------------------
# Parse command (first arg) then flags
# ----------------------------------------
COMMAND="${1:-}"
shift || true   # shift past command; ok if no args

ROV_IP="$DEFAULT_ROV_IP"
STREAM_PATH="$DEFAULT_STREAM"
RTSP_PORT="$DEFAULT_PORT"
CAPTURE_RATE="$DEFAULT_RATE"
SESSION_OVERRIDE=""

while getopts ":i:c:p:r:s:" opt; do
  case $opt in
    i) ROV_IP="$OPTARG" ;;
    c) STREAM_PATH="$OPTARG" ;;
    p) RTSP_PORT="$OPTARG" ;;
    r) CAPTURE_RATE="$OPTARG" ;;
    s) SESSION_OVERRIDE="$OPTARG" ;;
    :) echo "  ❌ Option -$OPTARG requires an argument."; exit 1 ;;
    \?) echo "  ❌ Unknown option: -$OPTARG"; exit 1 ;;
  esac
done

# ----------------------------------------
# Derived paths
# ----------------------------------------
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BASE="$HOME/rov_sessions/${TIMESTAMP}_${STREAM_PATH}"
FRAMES_DIR="$BASE/frames"
COLMAP_DIR="$BASE/colmap"
SPARSE_DIR="$COLMAP_DIR/sparse"
DATABASE="$COLMAP_DIR/database.db"

RTSP_URL="rtsp://$ROV_IP:$RTSP_PORT/$STREAM_PATH"

# ----------------------------------------
# Helper functions
# ----------------------------------------
log()  { echo ""; echo "▶ $1"; }
ok()   { echo "  ✅ $1"; }
warn() { echo "  ⚠️  $1"; }
die()  { echo "  ❌ $1"; exit 1; }

require() {
  command -v "$1" &>/dev/null || die "$1 not found. Run: brew install $2"
}

print_settings() {
  echo "  ROV IP:        $ROV_IP"
  echo "  Stream:        $STREAM_PATH  →  $RTSP_URL"
  echo "  Capture rate:  $CAPTURE_RATE fps  (1 frame every $(echo "scale=1; 1/$CAPTURE_RATE" | bc)s)"
  echo "  Session dir:   $BASE"
}

# ----------------------------------------
# STEP 1: Capture frames
# ----------------------------------------
capture() {
  require ffmpeg ffmpeg

  log "Capture settings:"
  print_settings
  echo ""

  mkdir -p "$FRAMES_DIR"

  echo "  Connecting to stream... (Ctrl+C to stop capturing)"
  echo ""

  trap 'echo ""; log "Capture stopped."; frame_count; echo ""; echo "  To reconstruct, run:"; echo "  ./rov_pipeline.sh reconstruct -s $BASE"; exit 0' INT

  ffmpeg \
    -rtsp_transport tcp \
    -i "$RTSP_URL" \
    -r "$CAPTURE_RATE" \
    -q:v 2 \
    "$FRAMES_DIR/frame_%04d.jpg" \
    2>&1 | grep --line-buffered -E "frame=|fps=|error|Error" || true
}

frame_count() {
  COUNT=$(ls "$FRAMES_DIR"/*.jpg 2>/dev/null | wc -l | tr -d ' ')
  echo "  Frames captured: $COUNT"
  if [ "$COUNT" -lt 30 ]; then
    warn "Only $COUNT frames — COLMAP works best with 30+ for a small object."
  else
    ok "$COUNT frames — good for reconstruction."
  fi
}

# ----------------------------------------
# STEP 2: COLMAP reconstruction
# ----------------------------------------
reconstruct() {
  require colmap colmap

  # Use override session, or auto-find the most recent one
  if [ -n "$SESSION_OVERRIDE" ]; then
    BASE="$SESSION_OVERRIDE"
    FRAMES_DIR="$BASE/frames"
    COLMAP_DIR="$BASE/colmap"
    SPARSE_DIR="$COLMAP_DIR/sparse"
    DATABASE="$COLMAP_DIR/database.db"
    log "Using specified session: $BASE"
  elif [ ! -d "$FRAMES_DIR" ]; then
    LATEST_FRAMES=$(ls -td "$HOME/rov_sessions"/*/frames 2>/dev/null | head -1)
    if [ -z "$LATEST_FRAMES" ]; then
      die "No sessions found. Run capture first, or use -s <path> to specify a session."
    fi
    FRAMES_DIR="$LATEST_FRAMES"
    BASE=$(dirname "$LATEST_FRAMES")
    COLMAP_DIR="$BASE/colmap"
    SPARSE_DIR="$COLMAP_DIR/sparse"
    DATABASE="$COLMAP_DIR/database.db"
    log "Using most recent session: $BASE"
  fi

  [ -d "$FRAMES_DIR" ] || die "Frames directory not found: $FRAMES_DIR"

  COUNT=$(ls "$FRAMES_DIR"/*.jpg 2>/dev/null | wc -l | tr -d ' ')
  [ "$COUNT" -lt 5 ] && die "Only $COUNT frames in $FRAMES_DIR — need at least 5 to reconstruct."
  ok "$COUNT frames found."

  mkdir -p "$SPARSE_DIR"

  log "Step 1/3: Feature extraction"
  colmap feature_extractor \
    --database_path "$DATABASE" \
    --image_path "$FRAMES_DIR" \
    --ImageReader.single_camera 1 \
    --SiftExtraction.use_gpu 0
  ok "Features extracted."

  log "Step 2/3: Sequential feature matching"
  colmap sequential_matcher \
    --database_path "$DATABASE" \
    --SequentialMatching.loop_detection 1
  ok "Features matched."

  log "Step 3/3: Sparse reconstruction (may take a few minutes on M2)"
  colmap mapper \
    --database_path "$DATABASE" \
    --image_path "$FRAMES_DIR" \
    --output_path "$SPARSE_DIR"
  ok "Reconstruction complete."

  if [ -d "$SPARSE_DIR/0" ]; then
    MODEL_PATH="$SPARSE_DIR/0"
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║        Import into Blender                               ║"
    echo "  ╠══════════════════════════════════════════════════════════╣"
    echo "  ║  1. Open Blender → delete the default cube              ║"
    echo "  ║  2. Edit → Preferences → Add-ons →                      ║"
    echo "  ║     search 'Photogrammetry' → enable the addon          ║"
    echo "  ║     (install from: github.com/SBCV/                     ║"
    echo "  ║      Blender-Addon-photogrammetry-importer)             ║"
    echo "  ║  3. File → Import → Colmap (model/workspace)            ║"
    echo "  ║  4. Navigate to this folder:                            ║"
    printf  "  ║     %-56s║\n" "$MODEL_PATH  "
    echo "  ║  5. Check 'Suppress Distortion Warnings' → Import       ║"
    echo "  ║  6. File → Save As to keep your .blend file             ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    echo "  Full session: $BASE"
  else
    warn "COLMAP finished but produced no model in $SPARSE_DIR."
    warn "Common causes:"
    warn "  - Not enough frame overlap (try -r 1 or -r 2 for more frames)"
    warn "  - ROV moved too fast between frames"
    warn "  - Poor lighting / low contrast on the subject"
  fi
}

# ----------------------------------------
# Main dispatch
# ----------------------------------------
case "$COMMAND" in
  capture)
    log "ROV Capture Pipeline"
    capture
    ;;
  reconstruct)
    log "COLMAP Reconstruction"
    reconstruct
    ;;
  all)
    log "ROV Full Pipeline: Capture → COLMAP"
    print_settings
    capture
    reconstruct
    ;;
  help|--help|-h|"")
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  capture      Connect to ROV and grab frames (Ctrl+C to stop)"
    echo "  reconstruct  Run COLMAP on captured frames"
    echo "  all          Capture then immediately reconstruct"
    echo ""
    echo "Options:"
    echo "  -i <ip>      ROV IP address       (default: $DEFAULT_ROV_IP)"
    echo "  -c <name>    Camera stream name    (default: $DEFAULT_STREAM)"
    echo "  -p <port>    RTSP port             (default: $DEFAULT_PORT)"
    echo "  -r <rate>    Capture rate fps      (default: $DEFAULT_RATE)"
    echo "               0.5 = 1 frame/2s, 1 = 1 frame/s, 2 = 2 frames/s"
    echo "  -s <path>    Session folder for reconstruct (default: latest)"
    echo ""
    echo "Examples:"
    echo "  $0 capture                          # use all defaults"
    echo "  $0 capture -i 192.168.3.52          # different ROV IP"
    echo "  $0 capture -c cam2 -r 1             # cam2, 1 frame/sec"
    echo "  $0 reconstruct                       # process latest session"
    echo "  $0 reconstruct -s ~/rov_sessions/20240501_143022_cam1"
    echo "  $0 all -i 192.168.3.52 -r 2         # full pipeline, fast capture"
    echo ""
    exit 0
    ;;
  *)
    die "Unknown command: '$COMMAND'. Run '$0 help' for usage."
    ;;
esac
