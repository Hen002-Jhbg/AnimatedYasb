"""Shared motion for popups, context menus and the rest of the bar.

One curve and one duration scale, so everything on the bar moves the same way.
WindowMotion animates the top-level window itself (opacity + position), so whatever the window
contains moves with it and nothing is resized or re-laid out while it runs. ValueMotion animates
a single number (a scroll offset, a height) on the same curve and ticker. SpringMotion moves a
number on a critically damped spring for motion that is retargeted often, like a carousel.
SnapshotMotion plays a popup's entrance and exit on a copy of it drawn at sub-pixel position and
scale, because a real window can only sit on whole pixels.
"""

import ctypes
import logging
import math
import os
from collections.abc import Callable

from PyQt6.QtCore import (
    QElapsedTimer,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

POPUP_ENTER_MS = 200
POPUP_EXIT_MS = 150
MENU_ENTER_MS = 150
MENU_EXIT_MS = 125
TOOLTIP_ENTER_MS = 125
TOOLTIP_EXIT_MS = 100
# A section inside a popup opening or closing (the volume menu's app list)
EXPAND_MS = 200
COLLAPSE_MS = 150

# How far a popup or menu travels while it fades in, in pixels
SLIDE_PX = 8
# Exits move half as far: they still show direction but release attention quietly
EXIT_SLIDE_PX = 4
# Tooltips are small, so they travel less
TOOLTIP_SLIDE_PX = 4

# A tooltip shown this soon after another (pointer moving along a row of icons) appears at once
TOOLTIP_WARM_MS = 300

# Snapshot popups also grow slightly from the side facing the bar, and shrink a hair on the way out
ENTER_SCALE = 0.97
EXIT_SCALE = 0.99
# Room around the snapshot so the slide never clips it, in pixels
SNAPSHOT_MARGIN_PX = 12

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


def ease_drawer() -> QEasingCurve:
    """iOS-like drawer curve: cubic-bezier(0.32, 0.72, 0, 1), for panels that slide in from a screen edge."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.32, 0.72), QPointF(0.0, 1.0), QPointF(1.0, 1.0))
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


class ValueMotion(QObject):
    """Animates one number on the shared curve and ticker, for motion that is not a whole window.

    start() while running continues from the current value, so retargeting never jumps.
    With Windows "Animation effects" off it jumps straight to the end value.
    """

    valueChanged = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, widget: QWidget, curve: QEasingCurve | None = None):
        super().__init__(widget)
        self._widget = widget
        self._curve = curve if curve is not None else ease_out()
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._from = 0.0
        self._to = 0.0
        self._value = 0.0
        self._duration = 0
        self._ticks = 0

    def value(self) -> float:
        return self._value

    def is_running(self) -> bool:
        return self._timer.isActive()

    def stop(self) -> None:
        """Stop where it is, without emitting finished."""
        self._timer.stop()

    def start(self, start: float, end: float, duration_ms: int) -> None:
        self._timer.stop()
        self._from = float(start)
        self._to = float(end)
        self._duration = max(0, int(duration_ms)) if animations_enabled() else 0
        if self._duration == 0:
            self._set(self._to)
            self.finished.emit()
            return
        self._set(self._from)
        self._timer.setInterval(tick_interval_ms(self._widget))
        self._ticks = 0
        self._clock.start()
        self._timer.start()

    def _tick(self) -> None:
        self._ticks += 1
        t = min(1.0, self._clock.elapsed() / self._duration)
        self._set(self._from + (self._to - self._from) * self._curve.valueForProgress(t))
        if t >= 1.0:
            self._timer.stop()
            if os.path.exists(_DEBUG_FLAG):
                logging.info(
                    "value motion %d ms: %d ticks at %d ms interval, avg %.1f ms per tick",
                    self._duration,
                    self._ticks,
                    self._timer.interval(),
                    self._clock.elapsed() / max(1, self._ticks),
                )
            self.finished.emit()

    def _set(self, value: float) -> None:
        self._value = value
        self.valueChanged.emit(value)


class SpringMotion(QObject):
    """Moves one number toward a target on a critically damped spring (no overshoot).

    Unlike ValueMotion it keeps its velocity when the target changes mid-way, so a run of quick
    steps (an arrow key held down) glides as one continuous motion instead of restarting each time,
    and a single step starts softly instead of jumping. `response` is Apple's term for the spring:
    roughly how long the move takes, in seconds. With Windows "Animation effects" off it jumps.
    """

    valueChanged = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, widget: QWidget, response: float, precision: float = 0.001):
        super().__init__(widget)
        self._widget = widget
        self._omega = 2.0 * math.pi / response
        self._precision = precision
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._value = 0.0
        self._velocity = 0.0
        self._target = 0.0
        self._last_ms = 0
        self._ticks = 0

    def value(self) -> float:
        return self._value

    def velocity(self) -> float:
        return self._velocity

    def is_running(self) -> bool:
        return self._timer.isActive()

    def stop(self) -> None:
        """Stop where it is, at rest, without emitting finished."""
        self._timer.stop()
        self._velocity = 0.0

    def animate_to(self, target: float, start: float) -> None:
        """Head for target. While moving, keep the current position and velocity; at rest, start from start."""
        if not self.is_running():
            self._value = float(start)
            self._velocity = 0.0
        self._target = float(target)
        if not animations_enabled():
            self.stop()
            self._set(self._target)
            self.finished.emit()
            return
        if not self.is_running():
            self._timer.setInterval(tick_interval_ms(self._widget))
            self._ticks = 0
            self._last_ms = 0
            self._clock.start()
            self._timer.start()

    def _tick(self) -> None:
        now = self._clock.elapsed()
        dt = (now - self._last_ms) / 1000.0
        self._last_ms = now
        self._ticks += 1
        # Exact critically damped step from the current offset and velocity (stable for any dt)
        w = self._omega
        x0 = self._value - self._target
        b = self._velocity + w * x0
        decay = math.exp(-w * dt)
        x = (x0 + b * dt) * decay
        self._velocity = (self._velocity - w * b * dt) * decay
        if abs(x) < self._precision and abs(self._velocity) < self._precision * w:
            self.stop()
            self._set(self._target)
            if os.path.exists(_DEBUG_FLAG):
                logging.info(
                    "spring motion %d ms: %d ticks at %d ms interval, avg %.1f ms per tick",
                    now,
                    self._ticks,
                    self._timer.interval(),
                    now / max(1, self._ticks),
                )
            self.finished.emit()
            return
        self._set(self._target + x)

    def _set(self, value: float) -> None:
        self._value = value
        self.valueChanged.emit(value)


class SnapshotGhost(QWidget):
    """A click-through copy of a window, painted at a sub-pixel offset, scale and opacity.

    Its state is (opacity, scale, dx, dy). The copy is taken when the ghost is created or refreshed;
    it only has to stand in for the real window for the ~200 ms an entrance or exit takes.
    """

    finished = pyqtSignal()

    def __init__(self, window: QWidget, origin: QPointF):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._curve = ease_out()
        self._clock = QElapsedTimer()
        self._wait = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._painted = False
        self._duration = 0
        self._ticks = 0
        self._state = (0.0, 1.0, 0.0, 0.0)
        self._from = self._state
        self._to = self._state
        self._origin = QPointF(origin)
        self.refresh(window)

    def refresh(self, window: QWidget) -> None:
        """Copy window again and sit over its current place, keeping the current state."""
        self._pixmap = window.grab()
        rest = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        margin = SNAPSHOT_MARGIN_PX
        self.setGeometry(rest.adjusted(-margin, -margin, margin, margin))
        self.update()

    def set_origin(self, origin: QPointF) -> None:
        self._origin = QPointF(origin)

    def state(self) -> tuple[float, float, float, float]:
        return self._state

    def is_running(self) -> bool:
        return self._timer.isActive()

    def stop(self) -> None:
        self._timer.stop()

    def jump(self, state: tuple[float, float, float, float]) -> None:
        self._timer.stop()
        self._state = state
        self.update()

    def animate(self, to_state: tuple[float, float, float, float], duration_ms: int) -> None:
        """Move from the current state to to_state on the shared curve; emits finished at the end."""
        self._timer.stop()
        self._from = self._state
        self._to = to_state
        self._duration = max(0, int(duration_ms))
        if self._duration == 0:
            self.jump(to_state)
            self.finished.emit()
            return
        self._timer.setInterval(tick_interval_ms(self))
        self._ticks = 0
        self._clock.invalidate()
        self._wait.start()
        self._timer.start()

    def _tick(self) -> None:
        if not self._clock.isValid():
            # A new window needs its first paint before the clock starts, or the start is skipped
            if not self._painted and self._wait.elapsed() < _FIRST_PAINT_WAIT_MS:
                return
            self._clock.start()
            return
        self._ticks += 1
        t = min(1.0, self._clock.elapsed() / self._duration)
        eased = self._curve.valueForProgress(t)
        self._state = tuple(a + (b - a) * eased for a, b in zip(self._from, self._to))
        self.update()
        if t >= 1.0:
            self._timer.stop()
            if os.path.exists(_DEBUG_FLAG):
                logging.info(
                    "snapshot motion %d ms: %d ticks at %d ms interval, avg %.1f ms per tick",
                    self._duration,
                    self._ticks,
                    self._timer.interval(),
                    self._clock.elapsed() / max(1, self._ticks),
                )
            self.finished.emit()

    def paintEvent(self, event) -> None:
        self._painted = True
        opacity, scale, dx, dy = self._state
        if opacity <= 0.0 or self._pixmap.isNull():
            return
        margin = float(SNAPSHOT_MARGIN_PX)
        pivot_x = margin + self._origin.x()
        pivot_y = margin + self._origin.y()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(opacity)
        painter.translate(pivot_x + dx, pivot_y + dy)
        painter.scale(scale, scale)
        painter.translate(-pivot_x, -pivot_y)
        painter.drawPixmap(QPointF(margin, margin), self._pixmap)
        painter.end()


class SnapshotMotion(QObject):
    """WindowMotion's interface, but the window never moves: a SnapshotGhost plays the motion.

    A real window sits on whole pixels, so moving it turns an 8 px slide into eight separate 1 px
    jumps, the last ones several frames apart. Here the real window stays at rest and invisible
    (opacity 0, still taking input) while the ghost draws a copy of it sliding at sub-pixel precision
    and growing from the side facing the bar. When the entrance ends, the real window takes over at
    exactly the same pixels. On exit the copy fades while the real window waits, invisible.
    """

    # True when an enter animation completed, False when an exit animation completed
    finished = pyqtSignal(bool)

    def __init__(self, window: QWidget, origin: Callable[[], QPointF]):
        super().__init__(window)
        self._window = window
        self._origin = origin
        self._ghost: SnapshotGhost | None = None
        self._entering = True

    def is_running(self) -> bool:
        return self._ghost is not None and self._ghost.is_running()

    def is_exiting(self) -> bool:
        return self.is_running() and not self._entering

    def enter(self, rest_pos: QPoint, offset: QPoint, duration_ms: int) -> None:
        """Fade in, slide from rest_pos + offset and grow to full size, without moving the window."""
        if self._window.pos() != rest_pos:
            self._window.move(rest_pos)
        if duration_ms <= 0:
            self._drop_ghost()
            self._window.setWindowOpacity(1.0)
            self.finished.emit(True)
            return
        scale = ENTER_SCALE
        if not animations_enabled():
            offset, scale = QPoint(0, 0), 1.0
        self._entering = True
        ghost = self._ghost
        if ghost is None:
            ghost = self._new_ghost()
            ghost.jump((0.0, scale, float(offset.x()), float(offset.y())))
        else:
            # Reversing an exit: keep the frame it is at, copy the window again
            ghost.set_origin(self._origin())
            ghost.refresh(self._window)
        self._window.setWindowOpacity(0.0)
        ghost.show()
        ghost.animate((1.0, 1.0, 0.0, 0.0), duration_ms)

    def exit(self, offset: QPoint, duration_ms: int) -> None:
        """Fade out, drift by offset and shrink a hair; the owner hides the real window afterwards."""
        if duration_ms <= 0 or not self._window.isVisible():
            self._drop_ghost()
            self.finished.emit(False)
            return
        scale = EXIT_SCALE
        if not animations_enabled():
            offset, scale = QPoint(0, 0), 1.0
        self._entering = False
        ghost = self._ghost
        if ghost is None:
            ghost = self._new_ghost()
            ghost.jump((self._window.windowOpacity(), 1.0, 0.0, 0.0))
        else:
            ghost.refresh(self._window)
        # The real window stays shown so it keeps the mouse grab (the closing click must not reach
        # the widget that opened it), but only the copy is visible
        self._window.setWindowOpacity(0.0)
        ghost.show()
        ghost.animate((0.0, scale, float(offset.x()), float(offset.y())), duration_ms)

    def retarget(self, rest_pos: QPoint) -> None:
        """The window was resized or re-anchored mid-animation: copy it again where it is now."""
        if self._ghost is not None:
            self._ghost.set_origin(self._origin())
            self._ghost.refresh(self._window)

    def stop(self) -> None:
        """Remove the copy at once, without emitting finished."""
        self._drop_ghost()

    def _new_ghost(self) -> SnapshotGhost:
        ghost = SnapshotGhost(self._window, self._origin())
        ghost.finished.connect(self._on_ghost_finished)
        self._window.destroyed.connect(ghost.deleteLater)
        self._ghost = ghost
        return ghost

    def _drop_ghost(self) -> None:
        ghost, self._ghost = self._ghost, None
        if ghost is not None:
            ghost.stop()
            ghost.finished.disconnect(self._on_ghost_finished)
            ghost.hide()
            ghost.deleteLater()

    def _on_ghost_finished(self) -> None:
        entering = self._entering
        if entering:
            # Hand over to the real window at the exact same pixels, then remove the copy
            self._window.setWindowOpacity(1.0)
        self._drop_ghost()
        self.finished.emit(entering)
