"""Debug/UX preview window: camera frame + hand skeleton + gesture state.

Uses cv2.imshow (runs over Xwayland inside the guest). Failure to open a
window is non-fatal — the engine keeps injecting events without preview.
"""
import cv2

# Standard 21-landmark hand topology (wrist=0 .. pinky tip=20).
_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),        # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),        # index
    (0, 17),                                # palm base -> pinky base
    (5, 9), (9, 10), (10, 11), (11, 12),   # middle
    (9, 13), (13, 14), (14, 15), (15, 16), # ring
    (13, 17), (17, 18), (18, 19), (19, 20),# pinky
)


class Overlay:
    def __init__(self, scale: float = 0.5):
        self.scale = scale
        self.ok = True

    def show(self, frame, landmarks, state: str):
        if not self.ok:
            return
        try:
            h, w = frame.shape[:2]
            if landmarks:
                pts = [(int(x * w), int(y * h)) for x, y, _ in landmarks]
                for a, b in _CONNECTIONS:
                    cv2.line(frame, pts[a], pts[b], (0, 255, 0), 2)
                for p in pts:
                    cv2.circle(frame, p, 3, (0, 0, 255), -1)
            cv2.putText(frame, state, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        1.0, (255, 255, 0), 2)
            small = cv2.resize(frame, None, fx=self.scale, fy=self.scale)
            cv2.imshow("musashi gestures", small)
            cv2.waitKey(1)
        except cv2.error:
            self.ok = False

    def close(self):
        if self.ok:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
