"""MediaPipe HandLandmarker (Tasks API) wrapper: BGR frame in, 21 normalized landmarks out."""
import importlib.resources
import time

import cv2
import mediapipe as mp
from mediapipe.tasks.python import vision as mp_vision
from mediapipe.tasks.python.core.base_options import BaseOptions

_MODEL = importlib.resources.files("musashi_gestures").joinpath("models/hand_landmarker.task")


class HandTracker:
    def __init__(self, min_hand_detection_confidence: float,
                 min_hand_presence_confidence: float, min_tracking_confidence: float):
        with importlib.resources.as_file(_MODEL) as model_path:
            options = mp_vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(model_path)),
                running_mode=mp_vision.RunningMode.VIDEO,
                num_hands=1,
                min_hand_detection_confidence=min_hand_detection_confidence,
                min_hand_presence_confidence=min_hand_presence_confidence,
                min_tracking_confidence=min_tracking_confidence,
            )
            self._landmarker = mp_vision.HandLandmarker.create_from_options(options)
        self._timestamp_ms = 0

    def process(self, frame_bgr):
        """Returns a list of 21 (x, y, z) tuples in normalized coords, or None."""
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        # Tasks API VIDEO mode requires a strictly increasing timestamp; the
        # max(...) guard handles two frames landing in the same millisecond.
        ts = int(time.monotonic() * 1000)
        self._timestamp_ms = max(ts, self._timestamp_ms + 1)
        result = self._landmarker.detect_for_video(image, self._timestamp_ms)
        if not result.hand_landmarks:
            return None
        return [(p.x, p.y, p.z) for p in result.hand_landmarks[0]]

    def close(self):
        self._landmarker.close()
