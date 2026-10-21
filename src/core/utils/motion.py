"""Shared motion for popups, context menus and the rest of the bar.

Everything moves on springs, the way Apple's interfaces do: a spring has no fixed duration, it keeps
its velocity when its target changes (so turning around mid-way never hits a wall), and it settles
without overshoot at damping 1.0. Each kind of surface has its spring in SPRINGS, and
~/.config/yasb/motion-tuning.json can override them while YASB runs.
WindowMotion animates the top-level window itself (opacity + position), so whatever the window
contains moves with it and nothing is resized or re-laid out while it runs. ValueMotion animates
a single number (a scroll offset, a height) on the same springs and ticker. SpringMotion moves a
number on a critically damped spring for motion that is retargeted often, like a carousel.
SnapshotMotion plays a popup's entrance and exit on a copy of it drawn at sub-pixel position and
scale, because a real window can only sit on whole pixels. WidthReveal animates a bar widget's
width (opening, closing, and content that changes size), so the widgets next to it slide instead of jumping.
"""

import ctypes
import json
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
    QRectF,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QFrame, QWidget

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
# A launcher (Quick Launch) does not slide, so it grows a little more, from its own centre
LAUNCHER_ENTER_SCALE = 0.96
LAUNCHER_EXIT_SCALE = 0.98
# Room around the snapshot so the slide never clips it, in pixels
SNAPSHOT_MARGIN_PX = 12

# Qt's "no limit" for a widget's size (QWIDGETSIZE_MAX)
_NO_SIZE_LIMIT = 16777215

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


def ease_in_out() -> QEasingCurve:
    """Strong ease-in-out: cubic-bezier(0.77, 0, 0.175, 1), for things moving across the screen."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.77, 0.0), QPointF(0.175, 1.0), QPointF(1.0, 1.0))
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


# Springs in Apple's two terms: response is roughly how long the value takes to reach its target, in
# seconds, and damping 1.0 settles without overshoot (lower adds a little bounce). Per kind of surface:
# (entering response, leaving response, damping). Leaving is quicker: it is the interface answering.
SPRINGS = {
    "popup": (0.28, 0.18, 1.0),
    "menu": (0.24, 0.16, 1.0),
    "tooltip": (0.22, 0.14, 1.0),
    "gallery": (0.26, 0.16, 1.0),
    "bar": (0.42, 0.24, 1.0),
    "width": (0.34, 0.26, 1.0),
    "section": (0.32, 0.24, 1.0),
    # Bar widgets' hover pill: a quick fade in, a slightly longer fade out
    "hover": (0.10, 0.16, 1.0),
    # Quick Launch, summoned from the keyboard many times a day: quicker than a popup both ways
    "launcher": (0.24, 0.15, 1.0),
}

# Live tuning while YASB runs: this JSON overrides any surface, for example
# {"popup": {"response": 0.3, "exit_response": 0.2, "damping": 1.0}}. It is read again when it changes.
_TUNING_FILE = os.path.join(os.path.expanduser("~"), ".config", "yasb", "motion-tuning.json")
_tuning_state: dict = {"mtime": None, "data": {}}


def _tuning() -> dict:
    if os.environ.get("YASB_MOTION_NO_TUNING"):
        return {}
    try:
        mtime = os.stat(_TUNING_FILE).st_mtime
    except OSError:
        _tuning_state.update(mtime=None, data={})
        return {}
    if mtime != _tuning_state["mtime"]:
        _tuning_state["mtime"] = mtime
        try:
            with open(_TUNING_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _tuning_state["data"] = data if isinstance(data, dict) else {}
        except Exception as e:
            logging.warning("motion-tuning.json ignored: %s", e)
            _tuning_state["data"] = {}
    return _tuning_state["data"]


def spring_params(surface: str, exiting: bool = False) -> tuple[float, float]:
    """(response in seconds, damping ratio) of a surface's spring, with any live override applied."""
    entering, leaving, damping = SPRINGS[surface]
    response = leaving if exiting else entering
    override = _tuning().get(surface)
    if isinstance(override, dict):
        try:
            response = float(override.get("exit_response" if exiting else "response", response))
            damping = float(override.get("damping", damping))
        except (TypeError, ValueError):
            pass
    return max(0.02, response), min(1.0, max(0.2, damping))


def _spring_step(offset: float, velocity: float, omega: float, damping: float, dt: float) -> tuple[float, float]:
    """Exact step of a damped spring over dt seconds, from its offset to the target and its velocity."""
    if damping >= 0.999:
        b = velocity + omega * offset
        decay = math.exp(-omega * dt)
        return (offset + b * dt) * decay, (velocity - omega * b * dt) * decay
    a = damping * omega
    wd = omega * math.sqrt(1.0 - damping * damping)
    decay = math.exp(-a * dt)
    c, s = math.cos(wd * dt), math.sin(wd * dt)
    b = (velocity + a * offset) / wd
    return decay * (offset * c + b * s), decay * ((b * wd - a * offset) * c - (offset * wd + a * b) * s)


class SpringGroup(QObject):
    """A few numbers, each on its own spring toward a target, sharing one ticker at the screen's rate.

    animate() while running keeps every number's position and velocity and only changes the targets,
    so a reversal continues the motion instead of stopping dead. It settles when every number is
    within its precision of its target (and at rest), and only then snaps them exactly onto it.
    """

    changed = pyqtSignal()
    settled = pyqtSignal()

    def __init__(self, widget: QWidget, count: int):
        super().__init__(widget)
        self._widget = widget
        self.values = [0.0] * count
        self.velocities = [0.0] * count
        self.targets = [0.0] * count
        self._precisions = [0.001] * count
        self._omega = 2.0 * math.pi / 0.3
        self._damping = 1.0
        self._hold: Callable[[], bool] | None = None
        self._clock = QElapsedTimer()
        self._wait = QElapsedTimer()
        self._last_ms = 0
        self.ticks = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    def is_running(self) -> bool:
        return self._timer.isActive()

    def elapsed_ms(self) -> int:
        return self._clock.elapsed() if self._clock.isValid() else 0

    def interval_ms(self) -> int:
        return self._timer.interval()

    def jump(self, values) -> None:
        """Sit at values, at rest, without animating."""
        self._timer.stop()
        self.values = [float(v) for v in values]
        self.velocities = [0.0] * len(self.values)
        self.targets = list(self.values)
        self.changed.emit()

    def shift(self, deltas) -> None:
        """Move the values and their targets together (the resting place itself moved)."""
        self.values = [v + d for v, d in zip(self.values, deltas)]
        self.targets = [t + d for t, d in zip(self.targets, deltas)]
        self.changed.emit()

    def animate(self, targets, response: float, damping: float = 1.0, precisions=None, hold=None) -> None:
        """Head for targets from wherever the numbers are, keeping their velocity.

        While hold() returns True (a new window not painted yet, for at most 50 ms) no time passes,
        so a slow first paint cannot swallow the start of the motion.
        """
        self.targets = [float(t) for t in targets]
        self._omega = 2.0 * math.pi / max(0.02, response)
        self._damping = damping
        if precisions is not None:
            self._precisions = list(precisions)
        if not self._timer.isActive():
            self.ticks = 0
            self._hold = hold
            self._wait.start()
            self._clock.start()
            self._last_ms = 0
            self._timer.setInterval(tick_interval_ms(self._widget))
            self._timer.start()

    def stop(self) -> None:
        """Stop where it is, at rest, without emitting settled."""
        self._timer.stop()
        self.velocities = [0.0] * len(self.values)

    def _tick(self) -> None:
        now = self._clock.elapsed()
        if self._hold is not None:
            if self._hold() and self._wait.elapsed() < _FIRST_PAINT_WAIT_MS:
                self._last_ms = now
                return
            self._hold = None
        dt = (now - self._last_ms) / 1000.0
        self._last_ms = now
        if dt <= 0:
            return
        self.ticks += 1
        done = True
        omega, damping = self._omega, self._damping
        for i, target in enumerate(self.targets):
            x, v = _spring_step(self.values[i] - target, self.velocities[i], omega, damping, dt)
            self.values[i] = target + x
            self.velocities[i] = v
            precision = self._precisions[i]
            if abs(x) >= precision or abs(v) >= precision * omega:
                done = False
        if done:
            self._timer.stop()
            self.values = list(self.targets)
            self.velocities = [0.0] * len(self.values)
        self.changed.emit()
        if done:
            self.settled.emit()


def _log_spring(kind: str, response: float, springs: SpringGroup) -> None:
    if os.path.exists(_DEBUG_FLAG):
        logging.info(
            "%s spring %.2f s: settled in %d ms, %d ticks at %d ms",
            kind,
            response,
            springs.elapsed_ms(),
            springs.ticks,
            springs.interval_ms(),
        )


class WindowMotion(QObject):
    """Fades and slides a top-level window between a resting position and an offset from it, on springs.

    Opacity and position each follow the surface's spring and keep their velocity when the motion
    turns around (a closing popup reopened mid-way), so nothing jumps or stops dead. A newly shown
    window holds the motion until its first frame is painted, so a slow first paint cannot swallow
    the start.
    """

    # True when an enter animation completed, False when an exit animation completed
    finished = pyqtSignal(bool)

    def __init__(self, window: QWidget, surface: str = "popup"):
        super().__init__(window)
        self._window = window
        self._surface = surface
        self._springs = SpringGroup(window, 3)
        self._springs.changed.connect(self._apply)
        self._springs.settled.connect(self._on_settled)
        self._entering = True
        self._response = 0.0
        self._awaiting_paint = False
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
        return self._springs.is_running()

    def is_exiting(self) -> bool:
        return self.is_running() and not self._entering

    def enter(self, rest_pos: QPoint, offset: QPoint, duration_ms: int) -> None:
        """Fade in while sliding from rest_pos + offset to rest_pos. Any duration > 0 uses the spring."""
        if not animations_enabled():
            offset = QPoint(0, 0)
        self._rest_pos = QPoint(rest_pos)
        self._entering = True
        if duration_ms <= 0:
            self._springs.jump([1.0, rest_pos.x(), rest_pos.y()])
            self.finished.emit(True)
            return
        if not self.is_running():
            start = rest_pos + offset
            self._springs.jump([0.0, start.x(), start.y()])
        self._response, damping = spring_params(self._surface)
        self._springs.animate(
            [1.0, rest_pos.x(), rest_pos.y()], self._response, damping, [0.004, 0.5, 0.5], hold=self._unpainted
        )

    def exit(self, offset: QPoint, duration_ms: int) -> None:
        """Fade out while sliding from where it is to the resting position + offset."""
        if not animations_enabled():
            offset = QPoint(0, 0)
        if not self.is_running():
            pos = self._window.pos()
            self._rest_pos = QPoint(pos)
            self._springs.jump([self._window.windowOpacity(), pos.x(), pos.y()])
        self._entering = False
        target = self._rest_pos + offset
        if duration_ms <= 0:
            self._springs.jump([0.0, target.x(), target.y()])
            self.finished.emit(False)
            return
        # Done once it is invisible; where the position is by then no longer matters
        self._response, damping = spring_params(self._surface, exiting=True)
        self._springs.animate([0.0, target.x(), target.y()], self._response, damping, [0.004, math.inf, math.inf])

    def retarget(self, rest_pos: QPoint) -> None:
        """Move the resting position while running (the window was resized and re-anchored)."""
        if not self.is_running():
            return
        delta = rest_pos - self._rest_pos
        self._rest_pos = QPoint(rest_pos)
        self._springs.shift([0.0, delta.x(), delta.y()])

    def stop(self) -> None:
        """Stop where it is, without emitting finished."""
        self._springs.stop()

    def _unpainted(self) -> bool:
        return self._awaiting_paint

    def _apply(self) -> None:
        opacity, x, y = self._springs.values
        self._window.setWindowOpacity(min(1.0, max(0.0, opacity)))
        x, y = round(x), round(y)
        if self._window.x() != x or self._window.y() != y:
            self._window.move(x, y)

    def _on_settled(self) -> None:
        _log_spring(f"motion {'enter' if self._entering else 'exit'} {self._surface}", self._response, self._springs)
        self.finished.emit(self._entering)


class ValueMotion(QObject):
    """One number on a surface's spring (a height, a width, a progress), on the same ticker as the windows.

    start() while running keeps the number's position and velocity and only changes where it heads,
    so reversing mid-way continues smoothly. With Windows "Animation effects" off it jumps.
    """

    valueChanged = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, widget: QWidget, surface: str = "section", precision: float = 0.5):
        super().__init__(widget)
        self._surface = surface
        self._precision = precision
        self._response = 0.0
        self._springs = SpringGroup(widget, 1)
        self._springs.changed.connect(self._emit_value)
        self._springs.settled.connect(self._on_settled)

    def value(self) -> float:
        return self._springs.values[0]

    def is_running(self) -> bool:
        return self._springs.is_running()

    def stop(self) -> None:
        """Stop where it is, without emitting finished."""
        self._springs.stop()

    def start(self, start: float, end: float, duration_ms: int, exiting: bool = False) -> None:
        """Head for end; from start when at rest, from where it is when running. Any duration > 0 springs."""
        if duration_ms <= 0 or not animations_enabled():
            self._springs.jump([end])
            self.finished.emit()
            return
        if not self.is_running():
            self._springs.jump([start])
        self._response, damping = spring_params(self._surface, exiting)
        self._springs.animate([end], self._response, damping, [self._precision])

    def _emit_value(self) -> None:
        self.valueChanged.emit(self._springs.values[0])

    def _on_settled(self) -> None:
        _log_spring(f"value motion {self._surface}", self._response, self._springs)
        self.finished.emit()


class WidthReveal(QObject):
    """Animates a bar widget's width, so the widgets next to it slide instead of jumping.

    Opening shows the widget at zero width and grows it to its natural width; closing shrinks it to
    zero and then hides it (with hide_when_closed=False it stays shown at zero width instead: the audio
    visualizer must keep getting show and hide events). While it is open, a change of its natural
    width animates too (a new media title, a longer count): the width is held where it is and moves to
    the new size. All of these turn around mid-way from the current width. A widget that is not on
    screen jumps, and so does everything with Windows "Animation effects" off.
    """

    def __init__(self, widget: QWidget, is_open: bool = True, hide_when_closed: bool = True):
        super().__init__(widget)
        self._widget = widget
        self._open = is_open
        self._hide_when_closed = hide_when_closed
        self._width = 0.0
        self._target = 0.0
        self._min_width = widget.minimumWidth()
        self._on_closed: Callable[[], None] | None = None
        self._motion = ValueMotion(widget, "width", precision=0.5)
        self._motion.valueChanged.connect(self._apply)
        self._motion.finished.connect(self._on_finished)
        widget.installEventFilter(self)

    def is_open(self) -> bool:
        return self._open

    def eventFilter(self, obj, event) -> bool:
        # The content changed size: this arrives before the widget is re-laid out, so the width can be
        # held where it is and moved to the new size instead of jumping there
        if obj is self._widget and event.type() == QEvent.Type.LayoutRequest:
            self._follow_content()
        return False

    def open(self) -> None:
        """Grow to the natural width, showing the widget first."""
        self._on_closed = None
        if self._open:
            return
        self._open = True
        start = self._width if self._motion.is_running() else 0.0
        if self._hide_when_closed and self._widget.isHidden():
            self._widget.setMaximumWidth(0)
            self._widget.show()
        self._run(start, self._natural())

    def close(self, on_closed: Callable[[], None] | None = None) -> None:
        """Shrink to zero width, then hide (unless the widget has to stay shown) and call on_closed.

        The content stays as it is while it shrinks, so a caller can clear it in on_closed.
        """
        if not self._open:
            if self._motion.is_running():
                self._on_closed = on_closed
            elif on_closed is not None:
                on_closed()
            return
        self._open = False
        self._on_closed = on_closed
        if not self._widget.isVisible():
            self._motion.stop()
            self._finish()
            return
        start = self._width if self._motion.is_running() else float(self._widget.width())
        self._run(start, 0.0)

    def set_open(self, is_open: bool) -> None:
        """Jump to open or closed without animating (startup, or a widget that is not on screen)."""
        self._motion.stop()
        self._open = is_open
        self._finish()

    def _natural(self) -> float:
        return float(self._widget.sizeHint().width())

    def _follow_content(self) -> None:
        if not self._open or not self._widget.isVisible():
            return
        target = self._natural()
        if self._motion.is_running():
            if abs(target - self._target) >= 1:
                self._run(self._width, target)
            return
        current = float(self._widget.width())
        if abs(target - current) >= 1:
            self._run(current, target)

    def _run(self, start: float, end: float) -> None:
        if not self._motion.is_running():
            self._min_width = self._widget.minimumWidth()
        self._target = end
        self._widget.setMinimumWidth(0)
        shrinking = end < start
        self._motion.start(start, end, COLLAPSE_MS if shrinking else EXPAND_MS, exiting=shrinking)

    def _apply(self, width: float) -> None:
        self._width = width
        # Both limits: a maximum alone cannot hold a widget whose content just got narrower
        limit = max(0, round(width))
        if self._widget.minimumWidth() != limit or self._widget.maximumWidth() != limit:
            self._widget.setFixedWidth(limit)

    def _on_finished(self) -> None:
        if self._open and abs(self._natural() - self._width) >= 1:
            # The content changed again on the last frame: keep following it
            self._run(self._width, self._natural())
            return
        self._finish()

    def _finish(self) -> None:
        if self._open:
            self._width = self._natural()
            self._widget.setMaximumWidth(_NO_SIZE_LIMIT)
            self._widget.setMinimumWidth(self._min_width)
            if self._hide_when_closed and self._widget.isHidden():
                self._widget.show()
        else:
            self._width = 0.0
            if self._hide_when_closed:
                self._widget.hide()
                self._widget.setMaximumWidth(_NO_SIZE_LIMIT)
                self._widget.setMinimumWidth(self._min_width)
            else:
                self._widget.setMinimumWidth(0)
                self._widget.setMaximumWidth(0)
        self._widget.updateGeometry()
        if not self._open:
            callback, self._on_closed = self._on_closed, None
            if callback is not None:
                callback()


class HoverPillFrame(QFrame):
    """A bar widget's frame that paints its own hover and press pill, on a spring at the screen's rate.

    The pill fades in on hover, jumps to the press colour the moment the button goes down, eases back
    on release and fades out on leave. Its colours and shape come from the stylesheet as qproperties
    (hovercolor, presscolor, pillradius, pillinsetx, pillinsety: lowercase, because YASB's CSS
    processor lowercases property names), so the theme still owns them; a frame without a hovercolor
    paints nothing. A frame of it costs about a fifth of restyling the widget, which is what a CSS
    :hover transition does on every frame (0.28 ms against 1.27 ms on the bar), so it can afford to
    run at the screen's rate instead of the transition engine's 60 Hz.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._hover_color = QColor(0, 0, 0, 0)
        self._press_color = QColor(0, 0, 0, 0)
        self._pill_radius = 10
        self._inset_x = 2
        self._inset_y = 3
        self._hovered = False
        self._pressed = False
        self._response = 0.0
        # The pill's colour, channel by channel (r, g, b, a)
        self._color = SpringGroup(self, 4)
        self._color.changed.connect(self.update)
        self._color.jump([0.0, 0.0, 0.0, 0.0])

    def _get_hover_color(self) -> QColor:
        return self._hover_color

    def _set_hover_color(self, color: QColor) -> None:
        if QColor(color) != self._hover_color:
            self._hover_color = QColor(color)
            self._retarget(instant=True)

    def _get_press_color(self) -> QColor:
        return self._press_color

    def _set_press_color(self, color: QColor) -> None:
        if QColor(color) != self._press_color:
            self._press_color = QColor(color)
            self._retarget(instant=True)

    def _get_pill_radius(self) -> int:
        return self._pill_radius

    def _set_pill_radius(self, radius: int) -> None:
        self._pill_radius = int(radius)
        self.update()

    def _get_inset_x(self) -> int:
        return self._inset_x

    def _set_inset_x(self, inset: int) -> None:
        self._inset_x = int(inset)
        self.update()

    def _get_inset_y(self) -> int:
        return self._inset_y

    def _set_inset_y(self, inset: int) -> None:
        self._inset_y = int(inset)
        self.update()

    hovercolor = pyqtProperty(QColor, _get_hover_color, _set_hover_color)
    presscolor = pyqtProperty(QColor, _get_press_color, _set_press_color)
    pillradius = pyqtProperty(int, _get_pill_radius, _set_pill_radius)
    pillinsetx = pyqtProperty(int, _get_inset_x, _set_inset_x)
    pillinsety = pyqtProperty(int, _get_inset_y, _set_inset_y)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._retarget()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._pressed = False
        self._retarget()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_color.alpha():
            # Feedback the moment the button goes down, before the click completes
            self._pressed = True
            self._retarget(instant=True)
        super().mousePressEvent(event)  # ignored, so the bar widget still gets it

    def mouseReleaseEvent(self, event) -> None:
        if self._pressed:
            self._pressed = False
            self._retarget()
        super().mouseReleaseEvent(event)

    def release_hover(self) -> None:
        """Fade the pill out now: a popup opened and took the mouse, so no Leave may come."""
        if self._hovered or self._pressed:
            self._hovered = False
            self._pressed = False
            self._retarget()

    def _target(self) -> list[float]:
        if self._pressed and self._press_color.alpha():
            color = self._press_color
        elif self._hovered or self._pressed:
            color = self._hover_color
        else:
            # Fade out in the colour it has, so only the alpha moves
            color = QColor(self._hover_color)
            color.setAlpha(0)
        return [float(color.red()), float(color.green()), float(color.blue()), float(color.alpha())]

    def _retarget(self, instant: bool = False) -> None:
        target = self._target()
        if instant or not animations_enabled():
            self._color.jump(target)
            return
        if self._color.values[3] < 0.5:
            # Not visible yet: start in the target colour, so only the alpha fades in
            self._color.jump(target[:3] + [0.0])
        leaving = target[3] < self._color.values[3]
        self._response, damping = spring_params("hover", exiting=leaving)
        self._color.animate(target, self._response, damping, [0.5, 0.5, 0.5, 0.5])

    def paintEvent(self, event) -> None:
        r, g, b, a = (min(255, max(0, round(v))) for v in self._color.values)
        if a > 0:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(r, g, b, a))
            rect = QRectF(self.rect()).adjusted(self._inset_x, self._inset_y, -self._inset_x, -self._inset_y)
            painter.drawRoundedRect(rect, self._pill_radius, self._pill_radius)
            painter.end()
        super().paintEvent(event)


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

    Its state is (opacity, scale, dx, dy), each on the popup's spring. The copy is taken when the ghost
    is created or refreshed; it only has to stand in for the real window while it enters or leaves.
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
        self._painted = False
        self._response = 0.0
        self._springs = SpringGroup(self, 4)
        self._springs.changed.connect(self.update)
        self._springs.settled.connect(self._on_settled)
        self._springs.jump([0.0, 1.0, 0.0, 0.0])
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
        return tuple(self._springs.values)

    def is_running(self) -> bool:
        return self._springs.is_running()

    def stop(self) -> None:
        self._springs.stop()

    def jump(self, state: tuple[float, float, float, float]) -> None:
        self._springs.jump(state)

    def animate(self, to_state, response: float, damping: float, precisions) -> None:
        """Head for to_state on the spring, from the current state and velocity; emits finished when settled."""
        self._response = response
        self._springs.animate(to_state, response, damping, precisions, hold=self._unpainted)

    def _unpainted(self) -> bool:
        return not self._painted

    def _on_settled(self) -> None:
        _log_spring("snapshot motion", self._response, self._springs)
        self.finished.emit()

    def paintEvent(self, event) -> None:
        self._painted = True
        opacity, scale, dx, dy = self._springs.values
        opacity = min(1.0, opacity)
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

    def __init__(
        self,
        window: QWidget,
        origin: Callable[[], QPointF],
        surface: str = "popup",
        scales: tuple[float, float] = (ENTER_SCALE, EXIT_SCALE),
        live: bool = False,
        release_on_exit: bool = False,
    ):
        """live: the copy follows the window's repaints while it enters (typing shows at once).
        release_on_exit: the window hides as the exit starts, so the keyboard and mouse go back to
        whatever is underneath right away, and only the copy fades.
        """
        super().__init__(window)
        self._window = window
        self._origin = origin
        self._surface = surface
        self._enter_scale, self._exit_scale = scales
        self._live = live
        self._release_on_exit = release_on_exit
        self._refresh_pending = False
        self._ghost: SnapshotGhost | None = None
        self._entering = True
        if live:
            window.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        # The window repaints after an UpdateRequest; copy it again once that has happened
        if obj is self._window and event.type() == QEvent.Type.UpdateRequest and self._ghost is not None:
            if not self._refresh_pending:
                self._refresh_pending = True
                QTimer.singleShot(0, self._refresh_live)
        return False

    def _refresh_live(self) -> None:
        self._refresh_pending = False
        if self._ghost is not None and self._entering and self._window.isVisible():
            self._ghost.set_origin(self._origin())
            self._ghost.refresh(self._window)

    def releases_on_exit(self) -> bool:
        return self._release_on_exit

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
        scale = self._enter_scale
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
        response, damping = spring_params(self._surface)
        # The hand-over needs the copy exactly on the window's pixels, so it settles to a twentieth of one
        ghost.animate((1.0, 1.0, 0.0, 0.0), response, damping, [0.004, 0.0005, 0.05, 0.05])

    def exit(self, offset: QPoint, duration_ms: int) -> None:
        """Fade out, drift by offset and shrink a hair; the owner hides the real window afterwards."""
        if duration_ms <= 0 or not self._window.isVisible():
            self._drop_ghost()
            self.finished.emit(False)
            return
        scale = self._exit_scale
        if not animations_enabled():
            offset, scale = QPoint(0, 0), 1.0
        self._entering = False
        ghost = self._ghost
        if ghost is None:
            ghost = self._new_ghost()
            ghost.jump((self._window.windowOpacity(), 1.0, 0.0, 0.0))
        else:
            ghost.refresh(self._window)
        ghost.show()
        if self._release_on_exit:
            # Give the keyboard and mouse back at once; only the copy is left to fade
            self._window.setVisible(False)
        else:
            # The real window stays shown so it keeps the mouse grab (the closing click must not reach
            # the widget that opened it), but only the copy is visible
            self._window.setWindowOpacity(0.0)
        response, damping = spring_params(self._surface, exiting=True)
        # Done once the copy is invisible; its scale and offset by then no longer matter
        ghost.animate((0.0, scale, float(offset.x()), float(offset.y())), response, damping, [0.004, math.inf, math.inf, math.inf])

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
