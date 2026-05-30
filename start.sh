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
#
# Usage:  ./start.sh [ARM_DELAY_MINUTES]
#   ./start.sh         -> default 5 min delay (time to leave the room)
#   ./start.sh 0       -> arm immediately (handy for testing)
#   ./start.sh 2       -> wait 2 minutes before detecting

set -u
SM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SM_DIR/motion.pid"

# Load secrets (Telegram bot token + chat id) from secrets.env if present.
# This file is gitignored; see secrets.env.example for the format.
if [ -f "$SM_DIR/secrets.env" ]; then
    set -a; . "$SM_DIR/secrets.env"; set +a
fi

# Optional first arg = arming delay in MINUTES (default 5) -> SM_ARM_DELAY (seconds).
# Pass 0 to arm immediately (testing). Env SM_ARM_DELAY still overrides if set.
DELAY_MIN="${1:-5}"
export SM_ARM_DELAY="$(awk "BEGIN{print $DELAY_MIN * 60}")"

echo "Starting security monitor... (base: $SM_DIR)"
if [ "$DELAY_MIN" != "0" ]; then
    echo "Arming delay: ${DELAY_MIN} min — detection starts after you leave."
else
    echo "Arming immediately (no delay)."
fi

# --- Stop any existing detector ---
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    sleep 1
fi
pkill -f "$SM_DIR/motion_detect.py" 2>/dev/null
sleep 1

# --- Start the detector under caffeinate, in the background ---
#   -i  prevent system idle sleep      -m  prevent disk idle sleep
#   -s  prevent system sleep (on AC)   (no -d, so the display may sleep)
# Tuning knobs (override before calling, e.g. SM_THRESHOLD=2500 ./start.sh):
#   SM_THRESHOLD (1500), SM_NOISE_LEVEL (32), SM_MIN_FRAMES (2),
#   SM_EVENT_INTERVAL (1.5), SM_EVENT_TAIL (10), SM_HEARTBEAT_SECONDS (1800)
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
