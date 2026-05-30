# Hotel Room Security Monitor — Implementation Spec

## Overview

Set up the MacBook's built-in FaceTime camera as a motion-triggered security camera using `motion` (open source). On motion detection, save a JPEG snapshot and sync it to iCloud Drive. Also capture a periodic "heartbeat" snapshot every 30 minutes regardless of motion, to confirm the system is running.

---

## Prerequisites

Install `motion` via Homebrew:

```bash
brew install motion
```

Verify the FaceTime camera is detected:

```bash
system_profiler SPCameraDataType
```

---

## Directory Structure

Create the following directories:

```
~/SecurityMonitor/
├── snapshots/          # motion-triggered images land here
├── heartbeat/          # 30-min periodic snapshots land here
├── motion.conf         # motion config file
└── heartbeat.sh        # periodic snapshot script
```

iCloud sync targets:
```
~/Library/Mobile Documents/com~apple~CloudDocs/SecurityMonitor/snapshots/
~/Library/Mobile Documents/com~apple~CloudDocs/SecurityMonitor/heartbeat/
```

Create all directories:

```bash
mkdir -p ~/SecurityMonitor/snapshots
mkdir -p ~/SecurityMonitor/heartbeat
mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/SecurityMonitor/snapshots
mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/SecurityMonitor/heartbeat
```

---

## motion Configuration

Create `~/SecurityMonitor/motion.conf` with the following content:

```conf
# Camera
videodevice /dev/video0
width 1280
height 720
framerate 15

# Motion detection — images only, no video
output_pictures best
movie_output off
picture_filename %Y%m%d_%H%M%S_motion

# Where to save snapshots locally
target_dir /Users/YOURUSERNAME/SecurityMonitor/snapshots

# Motion sensitivity
threshold 1500
noise_level 32
minimum_motion_frames 2

# Ignore small areas (reduces false triggers from curtains/light changes)
# Focus detection on the center-left area facing the door
# Adjust these coordinates based on where your door is in the frame
mask_file                       # leave blank unless you create a mask image

# On each motion picture saved, copy it to iCloud
on_picture_save cp %f /Users/YOURUSERNAME/Library/Mobile\ Documents/com~apple~CloudDocs/SecurityMonitor/snapshots/

# Logging
log_level 5
log_file /Users/YOURUSERNAME/SecurityMonitor/motion.log

# Daemon mode — runs in background
daemon off
```

> **Note:** Replace `YOURUSERNAME` with your actual macOS username throughout. Run `whoami` to confirm.

---

## Heartbeat Script

Create `~/SecurityMonitor/heartbeat.sh`:

```bash
#!/bin/bash

# Captures a snapshot every 30 minutes as proof-of-life
# Uses ffmpeg (available via Homebrew) to grab a single frame from FaceTime camera

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOCAL_DIR="$HOME/SecurityMonitor/heartbeat"
ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/SecurityMonitor/heartbeat"
FILENAME="heartbeat_${TIMESTAMP}.jpg"

# Capture single frame from FaceTime camera
ffmpeg -f avfoundation -i "0" -vframes 1 -q:v 2 "$LOCAL_DIR/$FILENAME" -y 2>/dev/null

# Copy to iCloud
cp "$LOCAL_DIR/$FILENAME" "$ICLOUD_DIR/$FILENAME"

echo "$(date): Heartbeat saved — $FILENAME"
```

Make it executable:

```bash
chmod +x ~/SecurityMonitor/heartbeat.sh
```

Install ffmpeg if not present:

```bash
brew install ffmpeg
```

---

## Heartbeat Scheduling via launchd

Create a launchd plist to run the heartbeat every 30 minutes.

Create `~/Library/LaunchAgents/com.user.securityheartbeat.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.securityheartbeat</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/YOURUSERNAME/SecurityMonitor/heartbeat.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOURUSERNAME/SecurityMonitor/heartbeat.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOURUSERNAME/SecurityMonitor/heartbeat_error.log</string>
</dict>
</plist>
```

Load the agent:

```bash
launchctl load ~/Library/LaunchAgents/com.user.securityheartbeat.plist
```

---

## Starting Motion

Run motion manually in the foreground first to verify it works:

```bash
motion -c ~/SecurityMonitor/motion.conf
```

Walk in front of the camera. You should see JPEG files appear in `~/SecurityMonitor/snapshots/` and mirror to iCloud.

Once confirmed working, run in background:

```bash
motion -c ~/SecurityMonitor/motion.conf &
```

To stop:

```bash
pkill motion
```

---

## Startup Script (Optional)

To start everything with one command before leaving the hotel room, create `~/SecurityMonitor/start.sh`:

```bash
#!/bin/bash

echo "Starting security monitor..."

# Kill any existing motion instance
pkill motion 2>/dev/null

# Start motion in background
motion -c ~/SecurityMonitor/motion.conf &
MOTION_PID=$!
echo "motion started (PID $MOTION_PID)"

# Ensure heartbeat launchd agent is loaded
launchctl load ~/Library/LaunchAgents/com.user.securityheartbeat.plist 2>/dev/null

# Take an immediate heartbeat snapshot to confirm system is live
~/SecurityMonitor/heartbeat.sh

echo "Security monitor active. Snapshots syncing to iCloud."
echo "To stop: pkill motion"
```

```bash
chmod +x ~/SecurityMonitor/start.sh
```

---

## Camera Access Note

macOS requires explicit camera permission. The first time `motion` tries to access the FaceTime camera, macOS may block it or show no output. If that happens:

1. Go to **System Settings → Privacy & Security → Camera**
2. Ensure Terminal (or whichever app runs motion) has camera access enabled

Alternatively, test camera access with:

```bash
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep -i camera
```

---

## Tuning Tips

- **Too many false triggers** (light changes, shadows): increase `threshold` (try 2500–4000)
- **Missing real motion**: decrease `threshold` (try 800–1200)
- **Hotel room door position**: after first test run, check which part of the frame shows the door. If it's off to one side, consider creating a motion mask image to focus detection only on the door area.
- **iCloud upload speed**: on slow hotel Wi-Fi, snapshots may lag. The heartbeat images are small (~50–100KB) and will upload fast. Motion snapshots at 1280x720 are ~200–400KB — should still upload within seconds.

---

## Cleanup After Trip

```bash
# Stop motion
pkill motion

# Unload heartbeat agent
launchctl unload ~/Library/LaunchAgents/com.user.securityheartbeat.plist

# Optional: remove local snapshots after confirming iCloud has them
rm -rf ~/SecurityMonitor/snapshots/*
rm -rf ~/SecurityMonitor/heartbeat/*
```