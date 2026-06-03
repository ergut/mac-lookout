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
import tempfile
import threading
import subprocess
from datetime import datetime

import cv2

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
# Adaptive event-burst capture: when motion is confirmed we open an "event
# window" and capture a *sequence*. It is DENSE at the start of an event (to
# catch a face / identity in the first seconds) and then automatically SLOWS
# DOWN if activity keeps going (e.g. housekeeping for two minutes), so you don't
# get hundreds of near-identical frames. Capturing continues EVENT_TAIL seconds
# after motion stops.
EVENT_FAST_INTERVAL = float(os.environ.get("SM_EVENT_FAST_INTERVAL", "0.6"))  # gap during the fast phase
EVENT_FAST_WINDOW   = float(os.environ.get("SM_EVENT_FAST_WINDOW", "10.0"))   # how long the fast phase lasts
EVENT_SLOW_INTERVAL = float(os.environ.get("SM_EVENT_SLOW_INTERVAL", "3.0"))  # gap during sustained activity
EVENT_TAIL          = float(os.environ.get("SM_EVENT_TAIL", "10.0"))          # keep capturing after motion stops
WARMUP_FRAMES  = int(os.environ.get("SM_WARMUP", "30"))             # frames to let exposure settle
BG_ALPHA       = float(os.environ.get("SM_BG_ALPHA", "0.05"))       # running-avg learning rate
JPEG_QUALITY   = int(os.environ.get("SM_JPEG_QUALITY", "90"))

# Face detection (uses the Haar cascade bundled with OpenCV — no extra deps).
# When a face is found in an event frame, that frame is captioned and pushed to
# Telegram even if the throttle would otherwise skip it. Set SM_FACE_DETECT=0 off.
FACE_DETECT = os.environ.get("SM_FACE_DETECT", "1") != "0"

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
# Front-load alerts: push the first N frames of a new event to Telegram with no
# throttle (so an approaching person/face actually reaches your phone in the
# first seconds), THEN fall back to one push per TELEGRAM_MIN_INTERVAL seconds
# for the rest of a long event. Every frame is still saved locally + iCloud.
TELEGRAM_BURST        = int(os.environ.get("SM_TELEGRAM_BURST", "4"))
TELEGRAM_MIN_INTERVAL = float(os.environ.get("SM_TELEGRAM_MIN_INTERVAL", "30"))

# Optional mask image: white = watch this region, black = ignore. Same size as frame.
MASK_FILE = os.environ.get("SM_MASK_FILE", os.path.join(BASE_DIR, "mask.png"))

LOCAL_DIR  = os.path.join(BASE_DIR, "snapshots")
ICLOUD_DIR = os.path.join(
    HOME, "Library", "Mobile Documents", "com~apple~CloudDocs",
    "mac-lookout", "snapshots",
)
HB_LOCAL_DIR  = os.path.join(BASE_DIR, "heartbeat")
HB_ICLOUD_DIR = os.path.join(
    HOME, "Library", "Mobile Documents", "com~apple~CloudDocs",
    "mac-lookout", "heartbeat",
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
    # Millisecond precision: the fast phase saves multiple frames per second,
    # so a 1-second timestamp would collide and overwrite frames.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
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
    """Blocking curl call to the Telegram Bot API. Run via a daemon thread.

    The token-bearing URL is fed to curl via stdin (-K -), NOT as an argv
    element, so the bot token never shows up in `ps`/process listings.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    cmd = ["curl", "-s", "--max-time", "20"]
    for key, value in fields.items():
        cmd += ["-F", f"{key}={value}"]
    if photo_path:
        cmd += ["-F", f"photo=@{photo_path}"]
    cmd += ["-K", "-"]
    try:
        result = subprocess.run(cmd, input=f'url="{url}"\n',
                                capture_output=True, text=True, timeout=25)
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


_face_cascade = None


def detect_face(frame):
    """Return True if a frontal face is found (Haar cascade bundled with cv2)."""
    global _face_cascade
    if not FACE_DETECT:
        return False
    if _face_cascade is None:
        path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        _face_cascade = cv2.CascadeClassifier(path)
    if _face_cascade.empty():
        return False
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = _face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                           minSize=(60, 60))
    return len(faces) > 0


def play_say(message):
    """Speak text in the room via the macOS `say` command (non-blocking)."""
    def run():
        try:
            subprocess.run(["say", message], timeout=60)
        except Exception as e:
            log.warning("say failed: %s", e)
    threading.Thread(target=run, daemon=True).start()


def play_alarm():
    """Play an alarm sound in the room via `afplay` (non-blocking)."""
    def run():
        sound = os.path.join(BASE_DIR, "alarm.wav")
        if not os.path.exists(sound):
            sound = "/System/Library/Sounds/Sosumi.aiff"   # built-in fallback
        try:
            for _ in range(5):
                subprocess.run(["afplay", sound], timeout=30)
        except Exception as e:
            log.warning("alarm failed: %s", e)
    threading.Thread(target=run, daemon=True).start()


def _telegram_get_file_path(file_id):
    """Resolve a Telegram file_id to its server file_path via getFile."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
    cmd = ["curl", "-s", "--max-time", "20", "-F", f"file_id={file_id}", "-K", "-"]
    try:
        r = subprocess.run(cmd, input=f'url="{url}"\n',
                           capture_output=True, text=True, timeout=25)
        data = json.loads(r.stdout or "{}")
        if data.get("ok"):
            return (data.get("result") or {}).get("file_path")
        log.warning("Telegram getFile failed: %s", r.stdout[:160])
    except Exception as e:
        log.warning("Telegram getFile error: %s", e)
    return None


def play_voice(file_id):
    """Download a Telegram voice/audio message and play it aloud in the room.

    Telegram voice notes are OGG/Opus, which afplay can't play, so we transcode
    to WAV with ffmpeg (already a dependency) and then afplay it. Non-blocking.
    """
    def run():
        file_path = _telegram_get_file_path(file_id)
        if not file_path:
            telegram_text("Couldn't fetch that audio — try again.")
            return
        src = os.path.join(tempfile.gettempdir(), "sm_voice_in")
        wav = os.path.join(tempfile.gettempdir(), "sm_voice_in.wav")
        dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        try:
            subprocess.run(["curl", "-s", "--max-time", "30", "-o", src, "-K", "-"],
                           input=f'url="{dl_url}"\n', text=True, timeout=40)
            conv = subprocess.run([ffmpeg, "-y", "-i", src, wav],
                                  capture_output=True, timeout=40)
            if conv.returncode != 0 or not os.path.exists(wav):
                log.warning("voice transcode failed: %s", conv.stderr[-160:] if conv.stderr else "?")
                telegram_text("Couldn't play that audio (transcode failed).")
                return
            subprocess.run(["afplay", wav], timeout=180)
            log.info("Played voice message in the room.")
        except Exception as e:
            log.warning("voice playback failed: %s", e)
    threading.Thread(target=run, daemon=True).start()


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
    "/say <text> — speak text aloud in the room\n"
    "/alarm — sound an alarm in the room\n"
    "🎙️ send a voice message — play it aloud in the room\n"
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
    cmd += ["-K", "-"]   # URL (with token) via stdin, not argv
    try:
        result = subprocess.run(cmd, input=f'url="{url}"\n',
                                capture_output=True, text=True, timeout=timeout + 15)
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
    log.info("Telegram command listener active (send /help for commands).")
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
            # A voice note (or audio file) -> play it aloud in the room (intercom).
            media = msg.get("voice") or msg.get("audio")
            if media and media.get("file_id"):
                play_voice(media["file_id"])
                telegram_text("🔊 Playing your message in the room.")
                continue
            raw = (msg.get("text") or "").strip()   # keep original case for /say
            parts = raw.split()
            cmd = parts[0].lower() if parts else ""
            if cmd in ("/photo", "/snap", "/pic"):
                send_ondemand_photo()
            elif cmd == "/status":
                telegram_text(_status_text())
            elif cmd == "/say":
                spoken = raw.split(None, 1)[1].strip() if len(parts) > 1 else ""
                if spoken:
                    play_say(spoken)
                    telegram_text(f"🔊 Speaking in the room: {spoken}")
                else:
                    telegram_text("Usage: /say <text to speak in the room>")
            elif cmd == "/alarm":
                play_alarm()
                telegram_text("🚨 Sounding alarm in the room.")
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
        "fast=%.1fs/%.0fs slow=%.1fs tail=%.0fs heartbeat=%ds arm_delay=%.0fs face=%s",
        CAMERA_INDEX, WIDTH, HEIGHT, THRESHOLD, NOISE_LEVEL, MINIMUM_MOTION_FRAMES,
        EVENT_FAST_INTERVAL, EVENT_FAST_WINDOW, EVENT_SLOW_INTERVAL, EVENT_TAIL,
        HEARTBEAT_SECONDS, ARM_DELAY, "on" if FACE_DETECT else "off",
    )
    log.info("Telegram alerts: %s", "ENABLED" if TELEGRAM_ENABLED else "disabled (no token/chat id)")

    background = None          # float32 running-average frame
    mask = None
    motion_streak = 0
    last_save = 0.0
    last_heartbeat = 0.0       # 0 => emit one immediately after warm-up
    event_until = 0.0          # keep saving burst frames until this time
    event_start = 0.0          # when the current event began (for fast/slow phase)
    event_tg_count = 0         # photos pushed to Telegram in the current event
    event_face_pushed = False  # whether a face frame was already pushed this event
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
        # window.
        if changed_area >= THRESHOLD:
            motion_streak += 1
        else:
            motion_streak = 0

        event_was_active = now < event_until   # was an event already open?
        if motion_streak >= MINIMUM_MOTION_FRAMES:
            if not event_was_active:
                # A NEW event begins: reset the fast-phase clock and per-event
                # push counters so the first seconds are captured densely.
                event_start = now
                event_tg_count = 0
                event_face_pushed = False
                STATE["events"] += 1
                log.info("Motion event started.")
            event_until = now + EVENT_TAIL

        if now < event_until:
            # Dense at the start of the event (catch the face), then slower for
            # sustained activity (e.g. housekeeping) so we don't flood storage.
            in_fast_phase = (now - event_start) < EVENT_FAST_WINDOW
            interval = EVENT_FAST_INTERVAL if in_fast_phase else EVENT_SLOW_INTERVAL
            if (now - last_save) >= interval:
                path = save_snapshot(frame)
                last_save = now
                face = detect_face(frame)
                # Front-load Telegram: push the first TELEGRAM_BURST frames of an
                # event unthrottled; always push the first face frame; otherwise
                # throttle. Every frame is saved locally + iCloud regardless.
                push = (event_tg_count < TELEGRAM_BURST or
                        (now - last_telegram) >= TELEGRAM_MIN_INTERVAL)
                if face and not event_face_pushed:
                    push = True
                    event_face_pushed = True
                tag = "👤 Face detected" if face else "⚠️ Motion"
                log.info("%s (area=%d) -> %s", tag, int(changed_area), os.path.basename(path))
                if push:
                    telegram_photo(path, caption=f"{tag} {datetime.now():%H:%M:%S}")
                    last_telegram = now
                    event_tg_count += 1

        time.sleep(min_interval)

    cap.release()
    log.info("Motion detector stopped.")


if __name__ == "__main__":
    main()
