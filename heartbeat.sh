#!/bin/bash

# Standalone one-shot snapshot using ffmpeg. Handy for a quick camera test.
#
# NOTE: the running motion detector holds the camera exclusively, so this will
# fail ("Could not lock device") while start.sh is active. The 30-min heartbeat
# during monitoring is emitted by the detector itself, not by this script.

SM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOCAL_DIR="$SM_DIR/heartbeat"
ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/mac-lookout/heartbeat"
FILENAME="heartbeat_${TIMESTAMP}.jpg"
FFMPEG="$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)"

mkdir -p "$LOCAL_DIR" "$ICLOUD_DIR"

# avfoundation device "0" = MacBook Pro Camera.
"$FFMPEG" -f avfoundation -framerate 30 -i "0" -frames:v 1 -update 1 -q:v 2 \
    "$LOCAL_DIR/$FILENAME" -y 2>>"$SM_DIR/heartbeat_ffmpeg.log"

if [ -f "$LOCAL_DIR/$FILENAME" ]; then
    cp "$LOCAL_DIR/$FILENAME" "$ICLOUD_DIR/$FILENAME"
    echo "$(date): Heartbeat saved — $FILENAME"
else
    echo "$(date): Heartbeat FAILED — no frame (camera busy with detector, or permission)"
fi
