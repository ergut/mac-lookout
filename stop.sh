#!/bin/bash

# Stop the security monitor: stop the detector (releasing the camera cleanly)
# and tear down the caffeinate wrapper.

set -u
SM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SM_DIR/motion.pid"

# 1. SIGTERM the Python detector so it releases the camera and exits gracefully.
if pkill -f "motion-security/motion_detect.py" 2>/dev/null; then
    echo "motion detector stopped"
else
    echo "motion detector was not running"
fi

# 2. Tear down the caffeinate wrapper (PIDFILE holds its PID).
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    echo "caffeinate wrapper stopped (PID $(cat "$PIDFILE"))"
fi
rm -f "$PIDFILE"
# Belt-and-braces: kill any caffeinate still guarding the detector.
pkill -f "caffeinate -ims" 2>/dev/null

echo "Security monitor stopped."
