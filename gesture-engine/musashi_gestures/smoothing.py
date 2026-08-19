"""One Euro filter (Casiez et al. 2012): low lag when moving, low jitter when still."""
import math


class LowPass:
    def __init__(self):
        self.y = None

    def apply(self, x: float, alpha: float) -> float:
        self.y = x if self.y is None else alpha * x + (1 - alpha) * self.y
        return self.y


class OneEuro:
    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = LowPass()
        self._dx = LowPass()
        self._t_prev = None
        self._x_prev = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def apply(self, x: float, t: float) -> float:
        if self._t_prev is None:
            self._t_prev, self._x_prev = t, x
            self._x.apply(x, 1.0)
            self._dx.apply(0.0, 1.0)
            return x
        dt = max(t - self._t_prev, 1e-6)
        dx = (x - self._x_prev) / dt
        edx = self._dx.apply(dx, self._alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        out = self._x.apply(x, self._alpha(cutoff, dt))
        self._t_prev, self._x_prev = t, x
        return out


class PointFilter:
    """One Euro on an (x, y) pair."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float):
        self.fx = OneEuro(min_cutoff, beta, d_cutoff)
        self.fy = OneEuro(min_cutoff, beta, d_cutoff)

    def apply(self, x: float, y: float, t: float) -> tuple[float, float]:
        return self.fx.apply(x, t), self.fy.apply(y, t)
