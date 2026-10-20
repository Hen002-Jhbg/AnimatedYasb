"""Shared motion for popups and context menus.

One curve and one duration scale, so every popup and menu on the bar moves the same way.
WindowMotion animates the top-level window itself (opacity + position), so whatever the window
contains moves with it and nothing is resized or re-laid out while it runs.
"""

import ctypes
import logging
import os

from PyQt6.QtCore import QElapsedTimer, QEasingCurve, QEvent, QObject, QPoint, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget

POPUP_ENTER_MS = 200
POPUP_EXIT_MS = 150
MENU_ENTER_MS = 150
MENU_EXIT_MS = 125

# How far a popup or menu travels while it fades in, in pixels
SLIDE_PX = 8
# Exits move half as far: they still show direction but release attention quietly
EXIT_SLIDE_PX = 4

# Qt's own animation timer ticks every 16 ms
DEFAULT_TICK_MS = 16

_SPI_GETCLIENTAREAANIMATION = 0x1042

# Longest wait for a newly shown window's first paint before the animation clock starts anyway
_FIRST_PAINT_WAIT_MS = 50

# Create this file to log tick timing of every animation to yasb.log
_DEBUG_FLAG = os.path.join(os.path.expanduser("~"), ".config", "yasb", "motion-debug")


def tick_interval_ms(widget: QWidget) -> int:
    """Timer interval that lands at least one update in every frame of the widget's screen.

    Qt's animation timer is fixed at 16 ms (60 Hz); on a 165 Hz screen that holds each step for
    about three frames. Ticking at 2/3 of the frame time keeps one update per frame.
    """
    screen = widget.screen()
    hz = screen.refreshRate() if screen else 0.0
    if hz <= 0:
        return DEFAULT_TICK_MS
    return max(1, min(DEFAULT_TICK_MS, int(1000.0 / hz / 1.5)))


def ease_out() -> QEasingCurve:
    """Strong ease-out: cubic-bezier(0.23, 1, 0.32, 1)."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.23, 1.0), QPointF(0.32, 1.0), QPointF(1.0, 1.0))
    return curve


def animations_enabled() -> bool:
    """False when Windows "Animation effects" is off (Settings > Accessibility > Visual effects)."""
    enabled = ctypes.c_int(1)
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(_SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
    except Exception:
        return True
    return bool(enabled.value) if ok else True


class WindowMotion(QObject):
    """Fades and slides a top-level window between a resting position and an offset from it.

    Starting a new animation (or retargeting) picks up from the current opacity and position,
    so reversing mid-way never jumps. The clock starts only after a newly shown window has
    painted its first frame, so a slow first paint cannot swallow the start of the motion.
    """

    # True when an enter animation completed, False when an exit animation completed
    finished = pyqtSignal(bool)

    def __init__(self, window: QWidget):
        super().__init__(window)
        self._window = window
        self._curve = ease_out()
        self._clock = QElapsedTimer()
        self._wait = QElapsedTimer()
        self._awaiting_paint = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(DEFAULT_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._entering = True
        self._duration = 0
        self._ticks = 0
        self._progress = 1.0
        self._from_opacity = 1.0
        self._to_opacity = 1.0
        self._from_pos = QPoint()
        self._to_pos = QPoint()
        self._rest_pos = QPoint()
        window.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self._window:
            if event.type() == QEvent.Type.Show:
                self._awaiting_paint = True
            elif event.type() == QEvent.Type.Paint:
                self._awaiting_paint = False
        return False

    def is_running(self) -> bool:
        return self._timer.isActive()

    def is_exiting(self) -> bool:
        return self._timer.isActive() and not self._entering

    def enter(self, rest_pos: QPoint, offset: QPoint, duration_ms: int) -> None:
        """Fade in while sliding from rest_pos + offset to rest_pos."""
        if not animations_enabled():
            offset = QPoint(0, 0)
        if self.is_exiting():
            start_opacity = self._window.windowOpacity()
            start_pos = self._window.pos()
        else:
            start_opacity = 0.0
            start_pos = rest_pos + offset
        self._rest_pos = QPoint(rest_pos)
        self._start(True, start_opacity, 1.0, start_pos, rest_pos, duration_ms)

    def exit(self, offset: QPoint, duration_ms: int) -> None:
        """Fade out while sliding from the current position to the resting position + offset."""
        if not animations_enabled():
            offset = QPoint(0, 0)
        if not self.is_running():
            self._rest_pos = self._window.pos()
        self._start(False, self._window.windowOpacity(), 0.0, self._window.pos(), self._rest_pos + offset, duration_ms)

    def retarget(self, rest_pos: QPoint) -> None:
        """Move the resting position while running (the window was resized and re-anchored)."""
        if not self.is_running():
            return
        delta = rest_pos - self._rest_pos
        self._rest_pos = QPoint(rest_pos)
        self._from_pos += delta
        self._to_pos += delta
        self._apply(self._progress)

    def stop(self) -> None:
        """Stop where it is, without emitting finished."""
        self._timer.stop()

    def _start(
        self, entering: bool, from_opacity: float, to_opacity: float, from_pos: QPoint, to_pos: QPoint, duration_ms: int
    ) -> None:
        self._timer.stop()
        self._entering = entering
        self._from_opacity = from_opacity
        self._to_opacity = to_opacity
        self._from_pos = QPoint(from_pos)
        self._to_pos = QPoint(to_pos)
        self._duration = max(0, int(duration_ms))
        if self._duration == 0:
            self._apply(1.0)
            self.finished.emit(entering)
            return
        self._apply(0.0)
        self._timer.setInterval(tick_interval_ms(self._window))
        self._ticks = 0
        if self._awaiting_paint:
            # Newly shown window: _tick() starts the clock once its first frame is painted
            self._clock.invalidate()
            self._wait.start()
        else:
            self._clock.start()
        self._timer.start()

    def _tick(self) -> None:
        if not self._clock.isValid():
            if self._awaiting_paint and self._wait.elapsed() < _FIRST_PAINT_WAIT_MS:
                return
            self._clock.start()
            return
        self._ticks += 1
        t = min(1.0, self._clock.elapsed() / self._duration)
        self._apply(t)
        if t >= 1.0:
            self._timer.stop()
            if os.path.exists(_DEBUG_FLAG):
                logging.info(
                    "motion %s %d ms: %d ticks at %d ms interval, avg %.1f ms per tick",
                    "enter" if self._entering else "exit",
                    self._duration,
                    self._ticks,
                    self._timer.interval(),
                    self._clock.elapsed() / max(1, self._ticks),
                )
            self.finished.emit(self._entering)

    def _apply(self, t: float) -> None:
        self._progress = t
        eased = self._curve.valueForProgress(t)
        opacity = self._from_opacity + (self._to_opacity - self._from_opacity) * eased
        x = round(self._from_pos.x() + (self._to_pos.x() - self._from_pos.x()) * eased)
        y = round(self._from_pos.y() + (self._to_pos.y() - self._from_pos.y()) * eased)
        self._window.setWindowOpacity(opacity)
        if self._window.x() != x or self._window.y() != y:
            self._window.move(x, y)
