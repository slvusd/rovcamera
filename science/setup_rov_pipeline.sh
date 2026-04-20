#!/bin/bash

# ============================================================
# ROV 3D Reconstruction Pipeline - Mac Setup Script
# Installs: ffmpeg, COLMAP, and Blender (Apple Silicon)
# Assumes Homebrew is already installed
# ============================================================

set -e  # Exit immediately if any command fails

echo "========================================"
echo " ROV Pipeline Setup for Mac (Apple Silicon)"
echo "========================================"
echo ""

# --- Helper ---
check() {
  if command -v "$1" &>/dev/null; then
    echo "  ✅ $1 already installed, skipping."
    return 0
  else
    return 1
  fi
}

# ----------------------------------------
# 1. Update Homebrew
# ----------------------------------------
echo "→ Updating Homebrew..."
brew update
echo ""

# ----------------------------------------
# 2. Install ffmpeg
# ----------------------------------------
echo "→ Checking ffmpeg..."
if check ffmpeg; then
  :
else
  echo "  Installing ffmpeg..."
  brew install ffmpeg
  echo "  ✅ ffmpeg installed."
fi
echo ""

# ----------------------------------------
# 3. Install COLMAP
# ----------------------------------------
echo "→ Checking COLMAP..."
if check colmap; then
  :
else
  echo "  Installing COLMAP (this may take a few minutes)..."
  brew install colmap
  echo "  ✅ COLMAP installed."
fi
echo ""

# ----------------------------------------
# 4. Install Blender via Homebrew Cask
#    (installs the Apple Silicon native build)
# ----------------------------------------
echo "→ Checking Blender..."
if [ -d "/Applications/Blender.app" ]; then
  echo "  ✅ Blender already installed in /Applications, skipping."
else
  echo "  Installing Blender (Apple Silicon build)..."
  brew install --cask blender
  echo "  ✅ Blender installed."
fi
echo ""

# ----------------------------------------
# 5. Create working directory structure
# ----------------------------------------
echo "→ Creating project directory structure..."

BASE="$HOME/rov_reconstruction"
mkdir -p "$BASE/01_COLMAP"
mkdir -p "$BASE/02_VIDEOS"
mkdir -p "$BASE/03_FFMPEG"
mkdir -p "$BASE/04_SCENES"
mkdir -p "$BASE/05_SCRIPT"
mkdir -p "$BASE/recordings/cam1"
mkdir -p "$BASE/recordings/cam2"
mkdir -p "$BASE/recordings/cam3"

echo "  ✅ Directories created at $BASE"
echo ""

# ----------------------------------------
# 6. Create capture script for 3 cameras
# ----------------------------------------
echo "→ Creating 3-camera capture script..."

cat > "$BASE/05_SCRIPT/capture_cameras.sh" << 'CAPTURE_SCRIPT'
#!/bin/bash

# ============================================================
# Capture snapshots from 3 ROV cameras via RTSP (mediamtx)
# Usage: ./capture_cameras.sh <pi-ip-address>
# Example: ./capture_cameras.sh 192.168.1.50
# ============================================================

PI_IP="${1:-192.168.1.50}"   # Pass your Pi's IP as argument
RATE=0.5                      # Snapshots per second
BASE="$HOME/rov_reconstruction/recordings"

echo "Connecting to mediamtx at $PI_IP..."
echo "Press Ctrl+C to stop all captures."
echo ""

# Capture all 3 cameras in parallel
# Adjust stream names (cam1/cam2/cam3) to match your mediamtx config
ffmpeg -i "rtsp://$PI_IP:8554/cam1" -r $RATE "$BASE/cam1/snapshot_%04d.jpg" &
PID1=$!
echo "  Started cam1 (PID $PID1)"

ffmpeg -i "rtsp://$PI_IP:8554/cam2" -r $RATE "$BASE/cam2/snapshot_%04d.jpg" &
PID2=$!
echo "  Started cam2 (PID $PID2)"

ffmpeg -i "rtsp://$PI_IP:8554/cam3" -r $RATE "$BASE/cam3/snapshot_%04d.jpg" &
PID3=$!
echo "  Started cam3 (PID $PID3)"

# Wait and clean up all 3 on Ctrl+C
trap "echo ''; echo 'Stopping captures...'; kill $PID1 $PID2 $PID3 2>/dev/null; echo 'Done.'" EXIT
wait
CAPTURE_SCRIPT

chmod +x "$BASE/05_SCRIPT/capture_cameras.sh"
echo "  ✅ Capture script created at $BASE/05_SCRIPT/capture_cameras.sh"
echo ""

# ----------------------------------------
# 7. Verify installs
# ----------------------------------------
echo "========================================"
echo " Verifying installations..."
echo "========================================"

ffmpeg -version 2>&1 | head -1 && echo "  ✅ ffmpeg OK" || echo "  ❌ ffmpeg NOT found"
colmap help 2>&1 | head -1 && echo "  ✅ COLMAP OK" || echo "  ❌ COLMAP NOT found"
[ -d "/Applications/Blender.app" ] && echo "  ✅ Blender OK" || echo "  ❌ Blender NOT found"

echo ""
echo "========================================"
echo " Setup Complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Edit stream names in: $BASE/05_SCRIPT/capture_cameras.sh"
echo "     (match the stream names configured in mediamtx on your Pi)"
echo "  2. Run captures:  cd $BASE/05_SCRIPT && ./capture_cameras.sh <your-pi-ip>"
echo "  3. After capturing, copy snapshots into $BASE/02_VIDEOS for COLMAP"
echo "  4. Open Blender from /Applications and install the photogrammetry addon:"
echo "     github.com/SBCV/Blender-Addon-photogrammetry-importer"
echo ""
