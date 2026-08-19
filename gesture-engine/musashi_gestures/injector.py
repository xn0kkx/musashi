"""Virtual input devices via /dev/uinput.

Two devices, because a touchscreen has no hover: without a visible cursor the
user has no way to aim before a tap lands. So we run a plain absolute pointer
purely for aiming (draws the cursor, never presses) alongside a real
multi-touch touchscreen that delivers the actual taps/drags/swipes.

TouchInjector uses INPUT_PROP_DIRECT + ABS_MT_* (protocol B) — exactly what
udev's input_id builtin uses to set ID_INPUT_TOUCHSCREEN=1, which is what
makes libinput/phoc treat this as a touchscreen instead of a tablet.
"""
import threading

from evdev import UInput, AbsInfo, ecodes as e

ABS_MAX = 65535
MAX_SLOTS = 4


def _clamp(v: float) -> int:
    return int(max(0.0, min(1.0, v)) * ABS_MAX)


class PointerInjector:
    """Absolute pointer, aiming only: moves the cursor, never clicks.

    Advertises BTN_LEFT in its capabilities (never emitted) purely so udev's
    input_id builtin sets ID_INPUT_MOUSE — classification is capability-
    based, not event-based, and a device with ABS_X/ABS_Y but no EV_KEY at
    all doesn't match any of udev's/libinput's known device types and won't
    get a cursor drawn for it.
    """

    def __init__(self):
        caps = {
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
                (e.ABS_Y, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
            ],
            e.EV_KEY: [e.BTN_LEFT],
        }
        self.ui = UInput(caps, name="musashi-gesture-pointer", version=0x1)

    def move(self, x: float, y: float):
        """x, y in [0, 1] -> absolute screen position."""
        self.ui.write(e.EV_ABS, e.ABS_X, _clamp(x))
        self.ui.write(e.EV_ABS, e.ABS_Y, _clamp(y))
        self.ui.syn()

    def close(self):
        try:
            self.ui.close()
        except Exception:
            pass


class TouchInjector:
    """Real multi-touch touchscreen, protocol B.

    Coordinates are normalized [0, 1]. One SYN_REPORT per logical frame
    (down/move/up), not per individual write — matches how real touch
    hardware reports.
    """

    def __init__(self):
        caps = {
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
                (e.ABS_Y, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
                (e.ABS_MT_SLOT, AbsInfo(value=0, min=0, max=MAX_SLOTS - 1, fuzz=0, flat=0, resolution=0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(value=-1, min=0, max=65535, fuzz=0, flat=0, resolution=0)),
                (e.ABS_MT_POSITION_X, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
                (e.ABS_MT_POSITION_Y, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
            ],
            e.EV_KEY: [e.BTN_TOUCH],
        }
        self.ui = UInput(
            caps,
            name="musashi-gesture-touch",
            input_props=[e.INPUT_PROP_DIRECT],
            vendor=0x1d6b, product=0x0002, version=0x1,
        )
        self._lock = threading.Lock()
        self._next_id = 0
        self._active = set()

    def _new_tracking_id(self) -> int:
        self._next_id = (self._next_id + 1) % 65536
        return self._next_id

    def down(self, slot: int, x: float, y: float):
        """Start a new contact in `slot` at (x, y) in [0, 1]."""
        with self._lock:
            first = not self._active
            self._active.add(slot)
            self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
            self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, self._new_tracking_id())
            self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, _clamp(x))
            self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, _clamp(y))
            if first:
                self.ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
            self.ui.write(e.EV_ABS, e.ABS_X, _clamp(x))
            self.ui.write(e.EV_ABS, e.ABS_Y, _clamp(y))
            self.ui.syn()

    def move(self, slot: int, x: float, y: float):
        """Update the position of an already-down contact in `slot`."""
        with self._lock:
            if slot not in self._active:
                return
            self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
            self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, _clamp(x))
            self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, _clamp(y))
            self.ui.write(e.EV_ABS, e.ABS_X, _clamp(x))
            self.ui.write(e.EV_ABS, e.ABS_Y, _clamp(y))
            self.ui.syn()

    def up(self, slot: int):
        """End the contact in `slot`."""
        with self._lock:
            if slot not in self._active:
                return
            self._active.discard(slot)
            self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
            self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
            if not self._active:
                self.ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
            self.ui.syn()

    def release_all(self):
        """Lift every active contact — used when the hand disappears."""
        for slot in list(self._active):
            self.up(slot)

    def close(self):
        try:
            self.release_all()
        except Exception:
            pass
        try:
            self.ui.close()
        except Exception:
            pass
