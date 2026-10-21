from PyQt6.QtCore import (
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from core.bar_helper import GlobalState
from core.utils.motion import TOOLTIP_ENTER_MS, TOOLTIP_EXIT_MS, TOOLTIP_SLIDE_PX, TOOLTIP_WARM_MS, WindowMotion
from core.utils.win32.backdrop import enable_blur


class CustomToolTip(QFrame):
    """Custom tooltip widget with enhanced styling and fade effects."""

    _active_tooltip = None  # Class-level reference to the currently visible tooltip
    _tooltip_pool = []  # Pool of reusable tooltip instances
    _pool_size_limit = 5  # Maximum number of tooltips to keep in pool
    _last_hidden = QElapsedTimer()  # When the last tooltip finished hiding (see is_warm)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")
        self._base_pos = None
        self._is_destroyed = False
        self._position = None  # 'top', 'bottom', or None for auto

        # Create label for text content
        self.label = QLabel()
        self.label.setProperty("class", "tooltip dark" if GlobalState.is_dark() else "tooltip")

        # cast to int for margins
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.addWidget(self.label)
        self.apply_stylesheet()

        # Same window fade + slide as the bar popups, at tooltip size and speed
        self._motion = WindowMotion(self, "tooltip")
        self._motion.finished.connect(self._on_motion_finished)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.start_fade_out)

    @classmethod
    def is_warm(cls) -> bool:
        """True while a tooltip is showing, or one hid moments ago: the pointer is moving along a row,
        so the next tooltip should appear at once instead of waiting and animating again."""
        active = cls._active_tooltip
        if active is not None and active.isVisible():
            return True
        return cls._last_hidden.isValid() and cls._last_hidden.elapsed() < TOOLTIP_WARM_MS

    def cleanup_animations(self):
        """Stop any running fade before the tooltip is pooled or deleted."""
        self._motion.stop()

    @classmethod
    def get_or_create_tooltip(cls):
        """Get a tooltip from the pool or create a new one."""
        if cls._tooltip_pool:
            tooltip = cls._tooltip_pool.pop()
            tooltip._is_destroyed = False
            tooltip.label.setProperty("class", "tooltip dark" if GlobalState.is_dark() else "tooltip")
            tooltip.setStyleSheet(GlobalState.stylesheet())
            return tooltip
        return cls()

    @classmethod
    def return_to_pool(cls, tooltip):
        """Return a tooltip to the pool for reuse."""
        if len(cls._tooltip_pool) < cls._pool_size_limit and not tooltip._is_destroyed:
            tooltip.hide()
            tooltip.cleanup_animations()
            tooltip._is_destroyed = False
            cls._tooltip_pool.append(tooltip)
        else:
            tooltip._is_destroyed = True
            tooltip.cleanup_animations()
            tooltip.deleteLater()

    def apply_stylesheet(self):
        """Apply the tooltip stylesheet from GlobalState."""
        self.setStyleSheet(GlobalState.stylesheet())

    def update_content(self, text):
        """Update tooltip content without hiding it."""
        if self.label.text() != text:
            self.label.setText(text)
            self.adjustSize()
            # While fading in or out, the motion owns the position and lands on _base_pos itself
            if self.isVisible() and self._base_pos and not self._motion.is_running():
                self.move(self._base_pos)

    def _calculate_position(self, widget_geometry, top_level_geometry=None):
        """Calculate tooltip position based on widget geometry and screen bounds."""
        if top_level_geometry is None:
            top_level_geometry = widget_geometry

        screen = QGuiApplication.screenAt(widget_geometry.center()) or QGuiApplication.primaryScreen()
        screen_geometry = screen.geometry()

        # Center horizontally on widget
        x = widget_geometry.center().x() - (self.width() // 2)

        # Position vertically based on top level window (Bar or Popup)
        space_below = screen_geometry.bottom() - top_level_geometry.bottom()
        space_above = top_level_geometry.top() - screen_geometry.top()

        # Get dynamic offset
        opts = GlobalState.tooltip_options()
        offset = opts.offset if opts else 5

        if self._position == "top":
            # Force top position if there's space, otherwise fallback to bottom
            if space_above >= self.height() + offset:
                y = top_level_geometry.top() - self.height() - offset
            else:
                y = top_level_geometry.bottom() + offset
        elif self._position == "bottom":
            # Force bottom position if there's space, otherwise fallback to top
            if space_below >= self.height() + offset:
                y = top_level_geometry.bottom() + offset
            else:
                y = top_level_geometry.top() - self.height() - offset
        else:
            # Auto: prefer below, but above if no space
            if space_below >= self.height() + offset:
                y = top_level_geometry.bottom() + offset
            elif space_above >= self.height() + offset:
                y = top_level_geometry.top() - self.height() - offset
            else:
                y = top_level_geometry.bottom() + offset  # Default to below

        # Clamp to screen bounds
        x = max(screen_geometry.left(), min(x, screen_geometry.right() - self.width()))
        y = max(screen_geometry.top(), min(y, screen_geometry.bottom() - self.height()))

        return QPoint(x, y)

    def show_tooltip(self, text, widget_geometry=None, top_level_geometry=None, duration=None):
        """Show tooltip centered below or above the widget."""
        # Decide before replacing the previous tooltip, which still counts as showing
        warm = CustomToolTip.is_warm()
        if CustomToolTip._active_tooltip and CustomToolTip._active_tooltip is not self:
            CustomToolTip._active_tooltip.hide()
        CustomToolTip._active_tooltip = self

        self.label.setText(text)
        self.adjustSize()

        if widget_geometry:
            self._base_pos = self._calculate_position(widget_geometry, top_level_geometry)
        else:
            return

        self.hide_timer.stop()
        if not self.isVisible():
            # Start invisible so the first frame is never drawn at full opacity
            self.setWindowOpacity(0.0)
            self.move(self._base_pos)
        self.show()
        # Emerge from the widget it describes: a tooltip below it starts a little higher, one above a little lower
        below = self._base_pos.y() >= widget_geometry.center().y()
        offset = QPoint(0, -TOOLTIP_SLIDE_PX if below else TOOLTIP_SLIDE_PX)
        # Moving along a row of icons: the next tooltip appears at once
        self._motion.enter(self._base_pos, offset, 0 if warm else TOOLTIP_ENTER_MS)

        opts = GlobalState.tooltip_options()
        if opts and opts.blur_effect and opts.blur_effect.enabled:
            blur_cfg = opts.blur_effect
            enable_blur(
                self.winId(),
                DarkMode=blur_cfg.dark_mode,
                RoundCorners=blur_cfg.round_corners,
                RoundCornersType=blur_cfg.round_corners_type,
                BorderColor=blur_cfg.border_color,
            )

        if duration:
            self.hide_timer.start(duration)

    def start_fade_out(self):
        """Fade out in place (no slide), then hide and return to the pool."""
        if self._is_destroyed or not self.isVisible():
            return
        self.hide_timer.stop()
        self._motion.exit(QPoint(0, 0), TOOLTIP_EXIT_MS)

    def _on_motion_finished(self, entered: bool):
        if entered:
            return
        self.hide()
        CustomToolTip._last_hidden.start()
        if CustomToolTip._active_tooltip is self:
            CustomToolTip._active_tooltip = None
        CustomToolTip.return_to_pool(self)


class TooltipEventFilter(QObject):
    """Event filter that shows/hides a custom tooltip with delay."""

    def __init__(self, widget, tooltip_text, delay: int, position=None, parent=None):
        super().__init__(parent)
        self.widget = widget
        self.tooltip_text = tooltip_text
        self.tooltip = None
        self.hover_delay = delay
        self.position = position  # 'top', 'bottom', or None for auto
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._hide_tooltip)
        self._app_event_filter_installed = False
        self._mouse_inside = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(50)
        self.poll_timer.timeout.connect(self._poll_mouse)
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.timeout.connect(self._on_hover_timer)

    def cleanup(self):
        """Clean up resources when the event filter is no longer needed."""
        self.hide_timer.stop()
        self.poll_timer.stop()
        self.hover_timer.stop()

        if self._app_event_filter_installed:
            QGuiApplication.instance().removeEventFilter(self)
            self._app_event_filter_installed = False

        if self.tooltip and self.tooltip.isVisible():
            self.tooltip.start_fade_out()
        self.tooltip = None

    def _on_hover_timer(self):
        if self._mouse_inside:
            self.show_tooltip()

    def show_tooltip(self):
        if not self.tooltip:
            self.tooltip = CustomToolTip.get_or_create_tooltip()
            self.tooltip._position = self.position  # Set position preference

        # Update content if tooltip is visible, otherwise show it
        if self.tooltip.isVisible():
            self.tooltip.update_content(self.tooltip_text)
        else:
            widget_rect = self.widget.rect()
            widget_global_pos = self.widget.mapToGlobal(QPoint(0, 0))
            global_geometry = widget_rect.translated(widget_global_pos)

            geometry = (
                global_geometry
                if (self.widget.isVisible() and widget_rect.width() > 0 and widget_rect.height() > 0)
                else None
            )
            top_level = self.widget.window()
            if top_level and top_level.__class__.__name__ == "Bar":
                frame = top_level._bar_frame
                top_level_geometry = frame.click_rect().translated(frame.mapToGlobal(QPoint(0, 0)))
            else:
                top_level_geometry = global_geometry

            self.tooltip.show_tooltip(self.tooltip_text, geometry, top_level_geometry)

        self.hide_timer.stop()
        if not self._app_event_filter_installed:
            QGuiApplication.instance().installEventFilter(self)
            self._app_event_filter_installed = True
        self._mouse_inside = True
        self.poll_timer.start()

    def update_tooltip_text(self, new_text):
        """Update tooltip text without hiding the tooltip if it's currently shown."""
        self.tooltip_text = new_text
        # If tooltip is currently visible, update its content immediately
        if self.tooltip and self.tooltip.isVisible():
            self.tooltip.update_content(new_text)

    def _hide_tooltip(self):
        if self.tooltip and self.tooltip.isVisible():
            self.tooltip.start_fade_out()
        if self._app_event_filter_installed:
            QGuiApplication.instance().removeEventFilter(self)
            self._app_event_filter_installed = False
        self._mouse_inside = False
        self.poll_timer.stop()
        # Clear reference to tooltip so it can be returned to pool
        self.tooltip = None

    def _poll_mouse(self):
        pos = QCursor.pos()
        widget_rect = self.widget.rect()
        widget_global_pos = self.widget.mapToGlobal(QPoint(0, 0))
        global_geometry = widget_rect.translated(widget_global_pos)

        if global_geometry.contains(pos):
            self.hide_timer.stop()
            self._mouse_inside = True
        else:
            if not self.hide_timer.isActive():
                self.hide_timer.start(10)
            self._mouse_inside = False

    def eventFilter(self, obj, event):
        if not isinstance(obj, QObject):
            return False
        if obj is self.widget:
            if event.type() == QEvent.Type.Enter:
                self._mouse_inside = True
                # Moving along a row of icons: no second wait once a tooltip is already up
                self.hover_timer.start(0 if CustomToolTip.is_warm() else self.hover_delay)
                self.hide_timer.stop()
            elif event.type() == QEvent.Type.Leave:
                self.hover_timer.stop()
                if not self.hide_timer.isActive():
                    self.hide_timer.start(10)  # Always use 10ms for quick hide
                self._mouse_inside = False
            elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.FocusIn):
                self.hover_timer.stop()
                self.hide_timer.stop()
                self._hide_tooltip()
        # Application-wide mouse move
        if event.type() == QEvent.Type.MouseMove and self.tooltip and self.tooltip.isVisible():
            self._poll_mouse()
        return super().eventFilter(obj, event)


def set_tooltip(widget: QWidget, text: str, delay: int = 400, position: str | None = None):
    """Set a tooltip to a widget in a declarative way.

    Args:
        widget: The widget to attach the tooltip to.
        text: The tooltip text.
        delay: Tooltip delay in ms, tooltip will show after this delay, default is 400ms.
        position: Optional position preference - 'top' or 'bottom'. If None, auto-positions based on available space.
    """
    if not text:
        return

    if hasattr(widget, "_tooltip_filter"):
        widget._tooltip_filter.update_tooltip_text(text)
        # Update position if provided
        if position is not None:
            widget._tooltip_filter.position = position
    else:
        event_filter = TooltipEventFilter(widget, text, delay, position, widget)
        widget.setMouseTracking(True)
        widget.installEventFilter(event_filter)
        widget._tooltip_filter = event_filter
