"""Threaded webcam capture: always hands out the freshest frame, drops the rest."""
import threading

import cv2


class Camera:
    def __init__(self, device: int, width: int, height: int, fps: int, fourcc: str):
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera device {device}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)

        self._frame = None
        self._cond = threading.Condition()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            ok, frame = self.cap.read()
            if not ok:
                continue
            with self._cond:
                self._frame = frame
                self._cond.notify_all()

    def read(self, timeout: float = 1.0):
        """Block until a new frame arrives (or timeout); returns BGR frame or None."""
        with self._cond:
            self._cond.wait(timeout)
            frame, self._frame = self._frame, None
            return frame

    def close(self):
        self._running = False
        self._thread.join(timeout=2)
        self.cap.release()
