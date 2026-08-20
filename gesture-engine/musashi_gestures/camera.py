"""Threaded webcam capture: always hands out the freshest frame, drops the rest."""
import collections
import logging
import threading
import time

import cv2

log = logging.getLogger(__name__)


class Camera:
    def __init__(self, device: int, width: int, height: int, fps: int, fourcc: str):
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera device {device}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        got_fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        log.info(
            "camera negotiated: fps=%s fourcc=%s %sx%s",
            self.cap.get(cv2.CAP_PROP_FPS),
            "".join(chr((got_fourcc >> 8 * i) & 0xFF) for i in range(4)),
            int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )

        self._frame = None
        self._seq = 0
        self._read_ms = collections.deque(maxlen=400)
        self.last_capture_ts = 0.0
        self.last_seq = 0
        self._cond = threading.Condition()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            t0 = time.monotonic()
            ok, frame = self.cap.read()
            t1 = time.monotonic()
            if not ok:
                continue
            with self._cond:
                self._seq += 1
                self._read_ms.append((t1 - t0) * 1000.0)
                self._frame = (frame, t1, self._seq)
                self._cond.notify_all()

    def read(self, timeout: float = 1.0):
        """Block until a new frame arrives (or timeout); returns BGR frame or None."""
        with self._cond:
            self._cond.wait(timeout)
            item, self._frame = self._frame, None
            if item is None:
                return None
            frame, self.last_capture_ts, self.last_seq = item
            return frame

    def take_producer_stats(self):
        """Total frames produced so far, plus per-frame cap.read() ms since last call."""
        with self._cond:
            stats = self._seq, list(self._read_ms)
            self._read_ms.clear()
            return stats

    def close(self):
        self._running = False
        self._thread.join(timeout=2)
        self.cap.release()
