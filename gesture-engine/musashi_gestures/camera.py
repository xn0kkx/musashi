"""Threaded webcam capture: always hands out the freshest frame, drops the rest.

The producer thread owns the cv2.VideoCapture exclusively — including the
watchdog reopen path, so no cross-thread release() races. The XIFT camera's
isochronous stream is known to die silently (controls keep answering, frames
just stop — see ROADMAP.md); without the reopen the engine would block on a
dead capture forever with no log and no recovery short of a VM reboot.
"""
import collections
import logging
import threading
import time

import cv2

log = logging.getLogger(__name__)

# Producer-side failure handling. OpenCV's V4L2 backend surfaces a stalled
# stream as read() -> False (after its internal ~10s select timeout) or, with
# the device node gone, as an immediate False — hence both the stall window
# and the small sleep so a vanished device doesn't busy-spin the thread.
_FAIL_SLEEP = 0.05


class Camera:
    def __init__(self, device: int, width: int, height: int, fps: int,
                 fourcc: str, reopen_after: float = 3.0):
        self._params = (device, width, height, fps, fourcc)
        self._reopen_after = reopen_after
        self.cap = self._open()

        self._frame = None
        self._seq = 0
        self._read_ms = collections.deque(maxlen=400)
        self.last_capture_ts = 0.0
        self.last_seq = 0
        self.reopens = 0
        self._cond = threading.Condition()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _open(self):
        device, width, height, fps, fourcc = self._params
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open camera device {device}")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        got_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        log.info(
            "camera negotiated: fps=%s fourcc=%s %sx%s",
            cap.get(cv2.CAP_PROP_FPS),
            "".join(chr((got_fourcc >> 8 * i) & 0xFF) for i in range(4)),
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        return cap

    def _reopen(self):
        try:
            self.cap.release()
        except Exception:
            pass
        while self._running:
            try:
                self.cap = self._open()
            except RuntimeError as exc:
                log.warning("camera reopen failed (%s), retrying in 1s", exc)
                time.sleep(1.0)
                continue
            self.reopens += 1
            log.warning("camera reopened (total reopens=%d)", self.reopens)
            return

    def _loop(self):
        last_ok = time.monotonic()
        while self._running:
            t0 = time.monotonic()
            ok, frame = self.cap.read()
            t1 = time.monotonic()
            if not ok:
                if self._running and t1 - last_ok > self._reopen_after:
                    log.warning("camera stalled for %.1fs — reopening device",
                                t1 - last_ok)
                    self._reopen()
                    last_ok = time.monotonic()
                else:
                    time.sleep(_FAIL_SLEEP)
                continue
            last_ok = t1
            with self._cond:
                self._seq += 1
                self._read_ms.append((t1 - t0) * 1000.0)
                self._frame = (frame, t1, self._seq)
                self._cond.notify_all()

    def read(self, timeout: float = 1.0):
        """Return the freshest unconsumed frame, or None after the timeout.

        Returns immediately when a frame is already pending — the consumer
        must never sleep until the *next* capture while one is waiting, or
        the loop rate quantizes to integer divisors of the camera's rate
        whenever processing runs longer than one frame period.
        """
        with self._cond:
            self._cond.wait_for(lambda: self._frame is not None, timeout)
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
