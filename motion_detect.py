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
import time
import signal
import shutil
import logging
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
COOLDOWN_SECONDS = float(os.environ.get("SM_COOLDOWN", "2.0"))        # min gap between saved snapshots
WARMUP_FRAMES    = int(os.environ.get("SM_WARMUP", "30"))            # frames to let exposure settle
BG_ALPHA         = float(os.environ.get("SM_BG_ALPHA", "0.05"))      # running-avg learning rate
JPEG_QUALITY     = int(os.environ.get("SM_JPEG_QUALITY", "90"))

# Heartbeat: the detector holds the camera open, so it also emits the periodic
# proof-of-life snapshot itself (a separate ffmpeg grab can't share the camera).
# Set SM_HEARTBEAT_SECONDS=0 to disable. Default 1800s = 30 min.
HEARTBEAT_SECONDS = int(os.environ.get("SM_HEARTBEAT_SECONDS", "1800"))

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
        "Motion detector started. cam=%s %dx%d threshold=%d noise=%d min_frames=%d heartbeat=%ds",
        CAMERA_INDEX, WIDTH, HEIGHT, THRESHOLD, NOISE_LEVEL, MINIMUM_MOTION_FRAMES,
        HEARTBEAT_SECONDS,
    )

    background = None          # float32 running-average frame
    mask = None
    motion_streak = 0
    last_save = 0.0
    last_heartbeat = 0.0       # 0 => emit one immediately after warm-up
    frame_count = 0
    min_interval = 1.0 / max(FRAMERATE, 1)

    while _running:
        ok, frame = cap.read()
        if not ok or frame is None:
            log.warning("Frame grab failed; retrying in 1s.")
            time.sleep(1)
            continue

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

        # Periodic proof-of-life heartbeat from the same camera stream.
        now = time.time()
        if HEARTBEAT_SECONDS > 0 and (now - last_heartbeat) >= HEARTBEAT_SECONDS:
            hb = save_heartbeat(frame)
            log.info("Heartbeat saved -> %s", os.path.basename(hb))
            last_heartbeat = now

        if changed_area >= THRESHOLD:
            motion_streak += 1
        else:
            motion_streak = 0

        if motion_streak >= MINIMUM_MOTION_FRAMES and (now - last_save) >= COOLDOWN_SECONDS:
            path = save_snapshot(frame)
            log.info("Motion detected (area=%d) -> %s", int(changed_area), os.path.basename(path))
            last_save = now
            motion_streak = 0

        time.sleep(min_interval)

    cap.release()
    log.info("Motion detector stopped.")


if __name__ == "__main__":
    main()
