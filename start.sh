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

# Colorize warnings/errors only when stderr is a real terminal — otherwise
# (redirected to a log or piped) emit plain text, no escape codes.
if [ -t 2 ]; then
    C_RED=$'\033[1;31m'; C_YEL=$'\033[1;33m'; C_RST=$'\033[0m'
else
    C_RED=''; C_YEL=''; C_RST=''
fi

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

# --- Auto-setup offer: create the venv if it's missing and uv is available ---
# Only when stdin is a terminal (don't block on prompts in non-interactive runs).
if [ ! -d "$SM_DIR/.venv" ] && command -v uv >/dev/null 2>&1 && [ -t 0 ]; then
    printf '.venv not found. uv is available — run setup now? [y/N] '
    read -r REPLY
    case "$REPLY" in
        [yY]|[yY][eE][sS])
            echo "Creating virtualenv and installing dependencies..."
            if uv venv "$SM_DIR/.venv" && \
               uv pip install --python "$SM_DIR/.venv/bin/python" opencv-python-headless numpy; then
                echo "Setup complete."
            else
                echo "${C_RED}Setup failed. Install dependencies manually, then re-run.${C_RST}" >&2
                exit 1
            fi
            ;;
    esac
fi

# --- Resolve a Python interpreter ---
# Order: local venv -> caller's activated venv -> python3/python on PATH.
PYTHON_BIN=""
if [ -x "$SM_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SM_DIR/.venv/bin/python"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PYTHON_BIN="$VIRTUAL_ENV/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "${C_RED}Error: no Python interpreter found.${C_RST}" >&2
    echo "  Create the local venv:  uv venv .venv && uv pip install --python .venv/bin/python opencv-python-headless numpy" >&2
    echo "  Or install Python 3 and make it available on your PATH." >&2
    exit 1
fi
echo "Using Python: $PYTHON_BIN"

# --- Preflight checks ---
# Fatal: OpenCV must be importable in the chosen interpreter.
if ! "$PYTHON_BIN" -c "import cv2" >/dev/null 2>&1; then
    echo "${C_RED}Error: OpenCV (cv2) is not importable in $PYTHON_BIN.${C_RST}" >&2
    echo "  Install it with:  $PYTHON_BIN -m pip install opencv-python-headless numpy" >&2
    echo "  (or recreate the venv:  uv venv .venv && uv pip install --python .venv/bin/python opencv-python-headless numpy)" >&2
    exit 1
fi

# Non-fatal: ffmpeg only powers the voice intercom.
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "${C_YEL}Warning: ffmpeg not found — voice intercom will not work.${C_RST}" >&2
    echo "  Install it with:  brew install ffmpeg" >&2
fi

# Non-fatal: caffeinate is a macOS built-in; warn if somehow missing.
if ! command -v caffeinate >/dev/null 2>&1; then
    echo "${C_YEL}Warning: caffeinate not found — the system may sleep and pause capture.${C_RST}" >&2
fi

# Non-fatal: no Telegram creds means local-only (no phone alerts / intercom).
if [ -z "${SM_TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${SM_TELEGRAM_CHAT_ID:-}" ]; then
    echo "${C_YEL}Warning: Telegram not configured — running local-only (no phone alerts, heartbeats, or voice intercom).${C_RST}" >&2
    echo "  Setup:  cp secrets.env.example secrets.env  then add your bot token + chat id (see README 'Telegram')." >&2
fi

# --- Start the detector under caffeinate, in the background ---
#   -i  prevent system idle sleep      -m  prevent disk idle sleep
#   -s  prevent system sleep (on AC)   (no -d, so the display may sleep)
# Tuning knobs (override before calling, e.g. SM_THRESHOLD=2500 ./start.sh):
#   SM_THRESHOLD (1500), SM_NOISE_LEVEL (32), SM_MIN_FRAMES (2),
#   SM_EVENT_INTERVAL (1.5), SM_EVENT_TAIL (10), SM_HEARTBEAT_SECONDS (1800)
caffeinate -ims "$PYTHON_BIN" "$SM_DIR/motion_detect.py" \
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
