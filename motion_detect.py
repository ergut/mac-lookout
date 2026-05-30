#!/usr/bin/env python3
"""
macOS-native motion-triggered security camera.

Replaces the Linux-only `motion` daemon from the original spec. Captures from the
built-in FaceTime camera via OpenCV/AVFoundation, detects motion by frame
differencing against a running-average background, and on a confirmed trigger
saves a timestamped JPEG locally and mirrors it to iCloud Drive.

Tuning knobs below map onto the original motion.conf settings:
    THRESHOLD            <- threshold            (min changed-pixel AREA to count)
    NOISE_LEVEL          <- noise_level          (per-pixel diff intensity floor)
    MINIMUM_MOTION_FRAMES<- minimum_motion_frames(consecutive frames before save)

All knobs are overridable via environment variables (see start.sh).
"""

import os
import sys
import json
import time
import signal
import shutil
import logging
import threading
import subprocess
from datetime import datetime

import cv2
import numpy as np

HOME = os.path.expanduser("~")
# Base dir = the folder this script lives in (the project folder), overridable.
BASE_DIR = os.environ.get("SM_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))

# ---- Configuration (env-overridable) ---------------------------------------
CAMERA_INDEX = int(os.environ.get("SM_CAMERA_INDEX", "0"))          # 0 = MacBook Pro Camera
WIDTH        = int(os.environ.get("SM_WIDTH", "1280"))
HEIGHT       = int(os.environ.get("SM_HEIGHT", "720"))
FRAMERATE    = int(os.environ.get("SM_FRAMERATE", "15"))

# Detection tuning (analogues of the original motion.conf knobs)
THRESHOLD             = int(os.environ.get("SM_THRESHOLD", "1500"))   # min total changed area (px)
NOISE_LEVEL           = int(os.environ.get("SM_NOISE_LEVEL", "32"))   # binary diff threshold 0-255
MINIMUM_MOTION_FRAMES = int(os.environ.get("SM_MIN_FRAMES", "2"))     # consecutive frames to confirm

# Behaviour
# Event-burst capture: when motion is confirmed we open an "event window" and
# keep saving a frame every EVENT_INTERVAL seconds for as long as motion
# continues, plus EVENT_TAIL seconds after it stops. This yields a *sequence*
# of the intruder (far more useful than a single frame) without timelapsing an
# empty room 24/7.
EVENT_INTERVAL = float(os.environ.get("SM_EVENT_INTERVAL", "1.5"))   # gap between burst frames
EVENT_TAIL     = float(os.environ.get("SM_EVENT_TAIL", "10.0"))      # keep capturing after motion stops
WARMUP_FRAMES  = int(os.environ.get("SM_WARMUP", "30"))             # frames to let exposure settle
BG_ALPHA       = float(os.environ.get("SM_BG_ALPHA", "0.05"))       # running-avg learning rate
JPEG_QUALITY   = int(os.environ.get("SM_JPEG_QUALITY", "90"))

# Arming delay: seconds to wait before detection begins, so you can leave the
# room without tripping it. The camera stays on and keeps the baseline current
# (and heartbeats still fire) during the countdown. start.sh exposes this as a
# minutes argument: `./start.sh 5`.
ARM_DELAY = float(os.environ.get("SM_ARM_DELAY", "0"))

# Heartbeat: the detector holds the camera open, so it also emits the periodic
# proof-of-life snapshot itself (a separate ffmpeg grab can't share the camera).
# Set SM_HEARTBEAT_SECONDS=0 to disable. Default 1800s = 30 min.
HEARTBEAT_SECONDS = int(os.environ.get("SM_HEARTBEAT_SECONDS", "1800"))

# Telegram push: if a bot token + chat id are set, each motion event is pushed
# off-device instantly as a photo (real-time alert + off-device copy that does
# not depend on iCloud sync). Credentials come from secrets.env (gitignored).
TELEGRAM_TOKEN   = os.environ.get("SM_TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT    = os.environ.get("SM_TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT)
# Min seconds between photo pushes during one continuous event (avoid flooding
# your phone; every frame is still saved locally + iCloud regardless).
TELEGRAM_MIN_INTERVAL = float(os.environ.get("SM_TELEGRAM_MIN_INTERVAL", "30"))

# Optional mask image: white = watch this region, black = ignore. Same size as frame.
MASK_FILE = os.environ.get("SM_MASK_FILE", os.path.join(BASE_DIR, "mask.png"))

LOCAL_DIR  = os.path.join(BASE_DIR, "snapshots")
ICLOUD_DIR = os.path.join(
    HOME, "Library", "Mobile Documents", "com~apple~CloudDocs",
    "SecurityMonitor", "snapshots",
)
HB_LOCAL_DIR  = os.path.join(BASE_DIR, "heartbeat")
HB_ICLOUD_DIR = os.path.join(
    HOME, "Library", "Mobile Documents", "com~apple~CloudDocs",
    "SecurityMonitor", "heartbeat",
)
LOG_FILE = os.path.join(BASE_DIR, "motion.log")

# ---- Logging ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("motion")

_running = True

# Shared state for the Telegram command listener (running in a daemon thread).
_frame_lock = threading.Lock()
_latest_frame = None          # most recent BGR frame, for on-demand /photo
STATE = {"armed": False, "events": 0, "started": 0.0, "arm_time": 0.0, "pause_until": 0.0}


def _set_latest_frame(frame):
    global _latest_frame
    with _frame_lock:
        _latest_frame = frame


def _get_latest_frame():
    with _frame_lock:
        return None if _latest_frame is None else _latest_frame.copy()


def _stop(signum, _frame):
    global _running
    log.info("Received signal %s — shutting down.", signum)
    _running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def open_camera():
    """Open the camera via AVFoundation, falling back to the default backend."""
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        log.warning("AVFoundation backend failed; trying default backend.")
        cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FRAMERATE)
    return cap


def load_mask(shape):
    """Load and binarize the optional ROI mask, resized to the frame shape."""
    if not os.path.exists(MASK_FILE):
        return None
    m = cv2.imread(MASK_FILE, cv2.IMREAD_GRAYSCALE)
    if m is None:
        log.warning("Mask file %s could not be read; ignoring.", MASK_FILE)
        return None
    m = cv2.resize(m, (shape[1], shape[0]))
    _, m = cv2.threshold(m, 127, 255, cv2.THRESH_BINARY)
    log.info("Loaded ROI mask from %s", MASK_FILE)
    return m


def save_snapshot(frame):
    """Write a JPEG locally and mirror it to iCloud. Returns the local path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_motion.jpg"
    local_path = os.path.join(LOCAL_DIR, filename)
    cv2.imwrite(local_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    try:
        shutil.copy2(local_path, os.path.join(ICLOUD_DIR, filename))
    except OSError as e:
        log.error("iCloud copy failed for %s: %s", filename, e)
    return local_path


def save_heartbeat(frame):
    """Write a proof-of-life JPEG to the heartbeat dirs (local + iCloud)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"heartbeat_{ts}.jpg"
    local_path = os.path.join(HB_LOCAL_DIR, filename)
    cv2.imwrite(local_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    try:
        shutil.copy2(local_path, os.path.join(HB_ICLOUD_DIR, filename))
    except OSError as e:
        log.error("iCloud heartbeat copy failed for %s: %s", filename, e)
    return local_path


def _telegram_call(endpoint, fields, photo_path=None):
    """Blocking curl call to the Telegram Bot API. Run via a daemon thread."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    cmd = ["curl", "-s", "--max-time", "20"]
    for key, value in fields.items():
        cmd += ["-F", f"{key}={value}"]
    if photo_path:
        cmd += ["-F", f"photo=@{photo_path}"]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if '"ok":true' not in result.stdout:
            log.warning("Telegram %s failed: %s", endpoint, result.stdout[:200] or result.stderr[:200])
    except Exception as e:  # network hiccup must never crash the detector
        log.warning("Telegram %s error: %s", endpoint, e)


def telegram_photo(path, caption):
    if not TELEGRAM_ENABLED:
        return
    threading.Thread(
        target=_telegram_call,
        args=("sendPhoto", {"chat_id": TELEGRAM_CHAT, "caption": caption}, path),
        daemon=True,
    ).start()


def telegram_text(text):
    if not TELEGRAM_ENABLED:
        return
    threading.Thread(
        target=_telegram_call,
        args=("sendMessage", {"chat_id": TELEGRAM_CHAT, "text": text}),
        daemon=True,
    ).start()


def send_ondemand_photo(reason="On-demand"):
    """Grab the most recent frame and push it to Telegram immediately."""
    frame = _get_latest_frame()
    if frame is None:
        telegram_text("📷 Camera not ready yet — try again in a moment.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_ondemand.jpg"
    local_path = os.path.join(LOCAL_DIR, filename)
    cv2.imwrite(local_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    try:
        shutil.copy2(local_path, os.path.join(ICLOUD_DIR, filename))
    except OSError as e:
        log.error("iCloud copy failed for %s: %s", filename, e)
    log.info("On-demand photo requested -> %s", filename)
    # Send synchronously here (we're already in the listener thread).
    _telegram_call("sendPhoto", {"chat_id": TELEGRAM_CHAT,
                                 "caption": f"📸 {reason} {datetime.now():%H:%M:%S}"},
                   local_path)


HELP_TEXT = (
    "🛡️ Security monitor commands:\n"
    "/photo — grab a picture right now\n"
    "/status — armed/paused state, event count, uptime\n"
    "/pause [min] — pause detection (default 10 min), auto-rearms\n"
    "/resume — resume detection now\n"
    "/help — show this message"
)


def _status_text():
    now = time.time()
    pause_left = int(STATE.get("pause_until", 0.0) - now)
    if pause_left > 0:
        mode = f"⏸ PAUSED ({pause_left // 60}m {pause_left % 60}s left)"
    elif STATE.get("armed"):
        mode = "🔒 ARMED"
    else:
        rem = int(STATE.get("arm_time", now) - now)
        mode = f"🟡 DISARMED (arming in {rem}s)" if rem > 0 else "🟡 starting…"
    up = int(now - STATE.get("started", now))
    return (f"📊 Status: {mode}\n"
            f"Motion events: {STATE.get('events', 0)}\n"
            f"Uptime: {up // 3600}h {(up % 3600) // 60}m")


def _telegram_get_updates(offset, timeout):
    """Long-poll Telegram for incoming commands. Returns a list of updates."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    cmd = ["curl", "-s", "--max-time", str(timeout + 10), "-F", f"timeout={timeout}"]
    if offset is not None:
        cmd += ["-F", f"offset={offset}"]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        data = json.loads(result.stdout or "{}")
        if not data.get("ok"):
            if "Conflict" in data.get("description", ""):
                time.sleep(3)   # another poller is running; back off
            return []
        return data.get("result", [])
    except Exception as e:
        log.warning("Telegram getUpdates error: %s", e)
        time.sleep(3)
        return []


def telegram_listener():
    """Daemon loop: handle /photo and /status from the owner chat only."""
    log.info("Telegram command listener active (/photo, /status).")
    # Drain stale updates so old commands aren't replayed on startup.
    offset = None
    for u in _telegram_get_updates(None, timeout=0):
        offset = u["update_id"] + 1
    while _running:
        for u in _telegram_get_updates(offset, timeout=25):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != str(TELEGRAM_CHAT):
                continue   # SECURITY: ignore anyone but the configured owner
            text = (msg.get("text") or "").strip().lower()
            parts = text.split()
            cmd = parts[0] if parts else ""
            if cmd in ("/photo", "/snap", "/pic"):
                send_ondemand_photo()
            elif cmd == "/status":
                telegram_text(_status_text())
            elif cmd == "/pause":
                mins = 10.0
                if len(parts) > 1:
                    try:
                        mins = float(parts[1])
                    except ValueError:
                        telegram_text("Usage: /pause <minutes>  (e.g. /pause 10)")
                        continue
                STATE["pause_until"] = time.time() + mins * 60
                telegram_text(f"⏸ Paused for {mins:g} min — detection off. Auto-rearms after, "
                              f"or send /resume.")
            elif cmd == "/resume":
                if STATE.get("pause_until", 0.0) > time.time():
                    STATE["pause_until"] = 0.0
                    telegram_text("▶️ Resumed — detection on.")
                else:
                    telegram_text("Already active — nothing to resume.")
            elif cmd in ("/start", "/help"):
                telegram_text(HELP_TEXT)


def main():
    for d in (LOCAL_DIR, ICLOUD_DIR, HB_LOCAL_DIR, HB_ICLOUD_DIR):
        os.makedirs(d, exist_ok=True)

    cap = open_camera()
    if cap is None:
        log.error(
            "Could not open camera index %s. Grant camera access to the "
            "controlling terminal in System Settings -> Privacy & Security -> Camera.",
            CAMERA_INDEX,
        )
        sys.exit(1)

    log.info(
        "Motion detector started. cam=%s %dx%d threshold=%d noise=%d min_frames=%d "
        "event_interval=%.1fs tail=%.0fs heartbeat=%ds arm_delay=%.0fs",
        CAMERA_INDEX, WIDTH, HEIGHT, THRESHOLD, NOISE_LEVEL, MINIMUM_MOTION_FRAMES,
        EVENT_INTERVAL, EVENT_TAIL, HEARTBEAT_SECONDS, ARM_DELAY,
    )
    log.info("Telegram alerts: %s", "ENABLED" if TELEGRAM_ENABLED else "disabled (no token/chat id)")

    background = None          # float32 running-average frame
    mask = None
    motion_streak = 0
    last_save = 0.0
    last_heartbeat = 0.0       # 0 => emit one immediately after warm-up
    event_until = 0.0          # keep saving burst frames until this time
    last_telegram = 0.0        # throttle photo pushes during a long event
    last_countdown_log = 0.0
    armed_announced = ARM_DELAY <= 0
    paused_prev = False
    arm_time = time.time() + ARM_DELAY
    frame_count = 0
    min_interval = 1.0 / max(FRAMERATE, 1)

    # Publish initial state and start the Telegram command listener.
    STATE.update({"armed": armed_announced, "events": 0,
                  "started": time.time(), "arm_time": arm_time})
    if TELEGRAM_ENABLED:
        threading.Thread(target=telegram_listener, daemon=True).start()

    while _running:
        ok, frame = cap.read()
        if not ok or frame is None:
            log.warning("Frame grab failed; retrying in 1s.")
            time.sleep(1)
            continue

        _set_latest_frame(frame)   # keep newest frame available for /photo
        frame_count += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if background is None:
            background = gray.astype("float32")
            mask = load_mask(gray.shape)
            continue

        # During exposure warm-up, hard-reset the background to the settling
        # image and skip detection, so the first real comparison is against a
        # stable frame (avoids a spurious snapshot at startup).
        if frame_count <= WARMUP_FRAMES:
            background = gray.astype("float32")
            time.sleep(min_interval)
            continue

        now = time.time()

        # Periodic proof-of-life heartbeat (fires whether armed or not, so you
        # get a snapshot as you leave during the arming countdown).
        if HEARTBEAT_SECONDS > 0 and (now - last_heartbeat) >= HEARTBEAT_SECONDS:
            hb = save_heartbeat(frame)
            log.info("Heartbeat saved -> %s", os.path.basename(hb))
            last_heartbeat = now

        # Suppress detection during (a) the initial arming delay so you can walk
        # out, or (b) an active /pause. In both cases keep the baseline current.
        paused = now < STATE.get("pause_until", 0.0)
        if now < arm_time or paused:
            background = gray.astype("float32")
            if now < arm_time and now - last_countdown_log >= 30:
                log.info("Disarmed — detection starts in %ds (leave the room).",
                         int(arm_time - now))
                last_countdown_log = now
            paused_prev = paused
            time.sleep(min_interval)
            continue

        # Just came out of a pause -> let you know detection is back on.
        if paused_prev:
            paused_prev = False
            if armed_announced:
                log.info("Pause ended — ARMED.")
                telegram_text("▶️ Pause ended — monitor ARMED again.")

        if not armed_announced:
            log.info("ARMED — motion detection active.")
            telegram_text("🔒 Security monitor ARMED — watching the room.")
            armed_announced = True
            STATE["armed"] = True

        # Difference against the running-average background.
        delta = cv2.absdiff(gray, cv2.convertScaleAbs(background))
        cv2.accumulateWeighted(gray, background, BG_ALPHA)

        _, thresh = cv2.threshold(delta, NOISE_LEVEL, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)
        if mask is not None:
            thresh = cv2.bitwise_and(thresh, thresh, mask=mask)

        # Total changed area across all contours.
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        changed_area = sum(cv2.contourArea(c) for c in contours)

        # Confirm motion over consecutive frames, then open/extend the event
        # window. While the window is open we save a frame every EVENT_INTERVAL,
        # giving a sequence of the intruder rather than a single frame.
        if changed_area >= THRESHOLD:
            motion_streak += 1
        else:
            motion_streak = 0

        event_was_active = now < event_until   # was an event already open?
        if motion_streak >= MINIMUM_MOTION_FRAMES:
            event_until = now + EVENT_TAIL

        if now < event_until and (now - last_save) >= EVENT_INTERVAL:
            path = save_snapshot(frame)
            log.info("Motion event (area=%d) -> %s", int(changed_area), os.path.basename(path))
            last_save = now
            STATE["events"] += 1
            # Push to Telegram on a NEW event, then throttle during a long one.
            if not event_was_active or (now - last_telegram) >= TELEGRAM_MIN_INTERVAL:
                telegram_photo(path, caption=f"⚠️ Motion {datetime.now():%H:%M:%S}")
                last_telegram = now

        time.sleep(min_interval)

    cap.release()
    log.info("Motion detector stopped.")


if __name__ == "__main__":
    main()
