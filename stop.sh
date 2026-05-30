#!/bin/bash

# Stop the security monitor: stop the detector (releasing the camera cleanly)
# and tear down the caffeinate wrapper.

set -u
SM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SM_DIR/motion.pid"

# The detector's full script path appears in BOTH the python and the wrapping
# caffeinate command lines, so this single pattern matches both. Deriving it
# from SM_DIR keeps stop working even if the folder is renamed/moved.
PATTERN="$SM_DIR/motion_detect.py"

# 1. SIGTERM the detector (and its caffeinate wrapper) so the camera is
#    released cleanly and the process exits gracefully.
if pkill -TERM -f "$PATTERN" 2>/dev/null; then
    echo "motion detector stopped"
else
    echo "motion detector was not running"
fi

# 2. Belt-and-braces: if the PID file points at a still-living process, kill it.
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
fi
rm -f "$PIDFILE"

echo "Security monitor stopped."
