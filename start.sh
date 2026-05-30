#!/bin/bash

# Start the hotel-room security monitor.
#
#   - OpenCV motion detector holds the FaceTime camera, saves motion snapshots,
#     and ALSO emits the 30-min proof-of-life heartbeat from the same stream
#     (a separate ffmpeg grab can't share the camera on macOS).
#   - Wrapped in `caffeinate` so the system never sleeps while monitoring.
#     The display is allowed to sleep (dark screen, stealthier, saves battery);
#     the camera keeps capturing because the SYSTEM stays awake.
#   - Survives screen lock: the detector keeps running and keeps the camera open
#     across a lock, so motion + heartbeat snapshots continue.
#
# Everything lives in the folder this script is in. Run it from Terminal (so
# macOS attributes camera access to your session), then lock the screen and leave.

set -u
SM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SM_DIR/motion.pid"

echo "Starting security monitor... (base: $SM_DIR)"

# --- Stop any existing detector ---
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    sleep 1
fi
pkill -f "motion-security/motion_detect.py" 2>/dev/null
sleep 1

# --- Start the detector under caffeinate, in the background ---
#   -i  prevent system idle sleep      -m  prevent disk idle sleep
#   -s  prevent system sleep (on AC)   (no -d, so the display may sleep)
# Tuning knobs (override before calling, e.g. SM_THRESHOLD=2500 ./start.sh):
#   SM_THRESHOLD (1500), SM_NOISE_LEVEL (32), SM_MIN_FRAMES (2),
#   SM_HEARTBEAT_SECONDS (1800)
caffeinate -ims "$SM_DIR/.venv/bin/python" "$SM_DIR/motion_detect.py" \
    >>"$SM_DIR/motion_stdout.log" 2>&1 &
WRAP_PID=$!
echo "$WRAP_PID" > "$PIDFILE"
echo "motion detector started under caffeinate (PID $WRAP_PID)"
echo "system will stay awake; display may sleep; capture continues through screen lock"

echo ""
echo "Security monitor active. Snapshots syncing to iCloud."
echo "  Local motion snapshots : $SM_DIR/snapshots/"
echo "  Local heartbeats       : $SM_DIR/heartbeat/  (every 30 min)"
echo "  Logs                   : $SM_DIR/motion.log"
echo ""
echo "Now lock the screen (Ctrl+Cmd+Q) and leave. Keep the lid OPEN and power connected."
echo "To stop: $SM_DIR/stop.sh"
