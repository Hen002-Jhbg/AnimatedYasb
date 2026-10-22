import ctypes
import logging
import os
import subprocess
import winreg
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any

import win32gui
import win32process
from PyQt6.QtCore import (
    QAbstractNativeEventFilter,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QSizePolicy,
    QWidget,
    QWidgetAction,
)
from win32con import HWND_BOTTOM, HWND_NOTOPMOST, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE

from core.utils.controller import exit_application, reload_application
from core.utils.motion import ValueMotion
from core.utils.utilities import AnimatedContextMenu, refresh_widget_style
from core.utils.win32.app_bar import APPBAR_CALLBACK_MESSAGE, AppBarNotify
from core.utils.win32.bindings import SetWindowPos
from core.utils.win32.bindings.user32 import KillTimer, RegisterWindowMessage, SetTimer, user32
from core.utils.win32.structs import MSG
from core.utils.win32.utils import apply_qmenu_style

# Register TaskbarCreated message to detect Explorer restarts
WM_TASKBARCREATED = RegisterWindowMessage("TaskbarCreated")


class GlobalState:
    """Centralized global state for detached widgets and application-wide configurations."""

    _is_dark = False
    _stylesheet = None
    _tooltip_options = None

    @classmethod
    def is_dark(cls) -> bool:
        return cls._is_dark

    @classmethod
    def set_dark(cls, value: bool):
        cls._is_dark = value

    @classmethod
    def stylesheet(cls) -> str:
        return cls._stylesheet or ""

    @classmethod
    def set_stylesheet(cls, value: str):
        cls._stylesheet = value

    @classmethod
    def tooltip_options(cls) -> Any:
        return cls._tooltip_options

    @classmethod
    def set_tooltip_options(cls, value: Any):
        cls._tooltip_options = value


class BarAnimationManager(QObject):
    """Handles bar show/hide animations.

    One progress value drives the motion, 0 hidden and 1 shown, on the bar's spring (motion.SPRINGS).
    Showing while a hide is running (or the other way round) turns around from where the bar is, at the
    speed it has, instead of waiting for the end. Hiding is the system answering, so its spring is quicker.
    An auto-hiding bar uses the "autohide" spring, quicker to arrive.
    """

    # True once the bar has finished showing, False once it has finished hiding
    finished = pyqtSignal(bool)

    def __init__(self, bar_widget: QWidget, parent=None):
        super().__init__(parent)
        self.bar_widget = bar_widget
        self._target_geo = None
        self._mode = "fade"
        self._showing = True
        self._progress = 1.0
        self._rest_pos = QPoint()
        self._hidden_pos = QPoint()
        self._motion = ValueMotion(bar_widget, "bar", precision=0.002)
        self._motion.valueChanged.connect(self._apply)
        self._motion.finished.connect(self._on_finished)

    def show_bar(self):
        if not self.bar_widget._animation.get("enabled"):
            self.bar_widget.show()
            return
        if self.bar_widget._animation.get("type") == "fade":
            self._start_fade(True)
        else:
            self._start_slide(True)

    def hide_bar(self):
        if not self.bar_widget._animation.get("enabled"):
            self.bar_widget._skip_animation = True
            self.bar_widget.hide()
            self.bar_widget._skip_animation = False
            return
        if self.bar_widget._animation.get("type") == "fade":
            self._start_fade(False)
        else:
            self._start_slide(False)

    def _is_running(self) -> bool:
        return self._motion.is_running()

    def _stop_animation(self):
        self._motion.stop()

    def _start_fade(self, show: bool):
        if not self._is_running():
            self._mode = "fade"
            self._target_geo = None
        self._run(show)
        if show and not self.bar_widget.isVisible():
            self.bar_widget.show()

    def _slide_is_blocked(self, hidden: QRect) -> bool:
        """Is another screen sitting where the bar would slide out of view?

        The bar leaves its own screen while it slides, so on stacked monitors it would show up on
        the one next to it. Keeping it clipped means resizing the window on every frame, and DWM
        redraws the blur and the window shadow a step behind that, which is what smears. Nothing
        can clip a blurred window without resizing it, so those bars fade instead.
        """
        return any(screen.geometry().intersects(hidden) for screen in QApplication.screens())

    def _start_slide(self, show: bool):
        bar = self.bar_widget
        if not self._is_running():
            bar.position_bar()
            geo = bar.geometry()
            self._target_geo = (geo.x(), geo.y(), geo.width(), geo.height())

            screen_geo = bar.screen().geometry()
            if bar._alignment["position"] == "top":
                hidden_y = screen_geo.y() - geo.height()
            else:
                hidden_y = screen_geo.y() + screen_geo.height()
            hidden = QRect(geo.x(), hidden_y, geo.width(), geo.height())

            if self._slide_is_blocked(hidden):
                self._start_fade(show)
                return
            self._mode = "slide"
            self._rest_pos = geo.topLeft()
            self._hidden_pos = hidden.topLeft()
        self._run(show)
        if show and not bar.isVisible():
            bar.show()

    def _run(self, show: bool):
        """Head for shown (1) or hidden (0); a reversal keeps the bar's position and speed."""
        if self._is_running():
            if show == self._showing:
                return
            start = self._progress
        else:
            start = 0.0 if show else 1.0
        self._showing = show
        # duration: 0 in the config still means no animation; any other value uses the bar's spring
        duration = self.bar_widget._animation.get("duration", 300)
        self._motion.start(start, 1.0 if show else 0.0, duration, exiting=not show, surface=self._surface())

    def _surface(self) -> str:
        """An auto-hiding bar is revealed under a pointer that is already waiting, so it comes quicker."""
        manager = getattr(self.bar_widget, "_autohide_manager", None)
        return "autohide" if manager is not None and manager.is_enabled() else "bar"

    def _apply(self, value: float) -> None:
        self._progress = value
        if self._mode == "slide":
            x = round(self._hidden_pos.x() + (self._rest_pos.x() - self._hidden_pos.x()) * value)
            y = round(self._hidden_pos.y() + (self._rest_pos.y() - self._hidden_pos.y()) * value)
            if self.bar_widget.x() != x or self.bar_widget.y() != y:
                self.bar_widget.move(x, y)
        else:
            self.bar_widget.setWindowOpacity(value)

    def is_running(self) -> bool:
        return self._is_running()

    def is_hiding(self) -> bool:
        """Still on screen, sliding or fading out."""
        return self._is_running() and not self._showing

    def _on_finished(self):
        if self._showing:
            self._on_show_finished()
        else:
            self._on_hide_finished()
        self.finished.emit(self._showing)

    def _on_show_finished(self):
        if self._target_geo:
            self.bar_widget.setGeometry(*self._target_geo)
        self.bar_widget.setWindowOpacity(1.0)

        # Check if mouse left during the animation
        if (
            hasattr(self.bar_widget, "_autohide_manager")
            and self.bar_widget._autohide_manager is not None
            and self.bar_widget._autohide_manager._is_enabled
        ):
            cursor_pos = QCursor.pos()
            bar_geometry = self.bar_widget.geometry()
            autohide_mgr = self.bar_widget._autohide_manager

            # If not in the bar, and not in the safe zone (padding gap), start the timer
            if not bar_geometry.contains(cursor_pos) and not autohide_mgr._is_mouse_in_safe_zone(
                cursor_pos, bar_geometry
            ):
                if autohide_mgr._hide_timer:
                    autohide_mgr._hide_timer.start(autohide_mgr._autohide_delay)

    def _on_hide_finished(self):
        self.bar_widget._skip_animation = True
        self.bar_widget.hide()
        self.bar_widget._skip_animation = False
        self.bar_widget.setWindowOpacity(1.0)

    def cleanup(self):
        self._stop_animation()


class AutoHideZone(QFrame):
    """A transparent zone at the edge of the screen to detect when to show the bar"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setWindowOpacity(0.01)

    def enterEvent(self, event):
        # Show the parent bar when mouse enters detection zone
        if self.parent() and hasattr(self.parent(), "_autohide_manager"):
            self.parent()._autohide_manager.show_bar()


class AutoHideManager(QObject):
    """Manages autohide functionality for bars"""

    def __init__(self, bar_widget, parent=None):
        super().__init__(parent)
        self.bar_widget = bar_widget
        self._autohide_delay = 600
        self._detection_zone_height = None
        self._detection_zone = None
        self._hide_timer = None
        self._is_enabled = False

    def setup_autohide(self):
        """Initialize autohide functionality"""
        self._is_enabled = True
        # Set fixed 1px detection zone height
        self._detection_zone_height = 1

        # Create detection zone
        self._detection_zone = AutoHideZone(self.bar_widget)

        # Create hide timer
        self._hide_timer = QTimer(self.bar_widget)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_bar)

        # Install event filter on the bar
        self.bar_widget.installEventFilter(self)

        # Remove reserved screen space when autohide is enabled
        if hasattr(self.bar_widget, "app_bar_manager") and self.bar_widget.app_bar_manager:
            try:
                SystrayAppBarHelper.execute_without_systray_interference(
                    lambda: self.bar_widget.app_bar_manager.remove_appbar()
                )
            except Exception as e:
                logging.error("Failed to remove AppBar reservation: %s", e)

        # Set up detection zone after a short delay
        QTimer.singleShot(self._autohide_delay, self.setup_detection_zone)

    def setup_detection_zone(self):
        """Position and configure the autohide detection zone"""
        if not self._is_enabled or not self._detection_zone:
            return

        screen_geometry = self.bar_widget.screen().geometry()
        alignment = self.bar_widget._alignment

        if alignment["position"] == "top":
            self._detection_zone.setGeometry(
                screen_geometry.x(), screen_geometry.y(), screen_geometry.width(), self._detection_zone_height
            )
        else:
            self._detection_zone.setGeometry(
                screen_geometry.x(),
                screen_geometry.y() + screen_geometry.height() - self._detection_zone_height,
                screen_geometry.width(),
                self._detection_zone_height,
            )

        self._hide_timer.start(self._autohide_delay)

    def show_bar(self):
        """Show the bar when the pointer reaches the screen edge; a bar still sliding away turns around."""
        if not self._is_enabled:
            return
        if self._hide_timer:
            self._hide_timer.stop()
        animation = getattr(self.bar_widget, "_animation_manager", None)
        if not self.bar_widget.isVisible():
            self.bar_widget.show()
        elif animation is not None and animation.is_hiding():
            # Still on screen on its way out: come back from where it is, at the speed it has
            self.bar_widget.show_bar()

    def _is_child_of_bar(self, widget):
        """Walk parent chain to check if widget belongs to this bar."""
        p = widget.parent() if widget else None
        while p:
            if p is self.bar_widget:
                return True
            p = p.parent()
        return False

    def _should_stay_visible(self):
        """Check if bar should stay visible because a child popup/menu is open."""
        # Qt::Popup windows (QMenu, PopupWidget)
        if QApplication.activePopupWidget():
            return True
        # Qt::Tool windows that called activateWindow() (SystrayPopup); the edge zone is not one of them
        active = QApplication.activeWindow()
        if active and active is not self.bar_widget and active is not self._detection_zone and self._is_child_of_bar(active):
            return True
        # Check all visible top-level widgets
        cursor_pos = QCursor.pos()
        for w in QApplication.topLevelWidgets():
            if w is self.bar_widget or w is self._detection_zone or not w.isVisible():
                continue
            # Child of bar (parent chain intact) - e.g. SystrayPopup after losing focus
            if self._is_child_of_bar(w):
                return True
            # Cursor is over it (parent chain severed) - e.g. ThumbnailHost
            if w.geometry().contains(cursor_pos):
                return True
        return False

    def hide_bar(self):
        """Hide the bar and show detection zone"""
        if self._is_enabled and self.bar_widget.isVisible():
            if self._should_stay_visible():
                self._hide_timer.start(self._autohide_delay)
                return
            self.bar_widget.hide()
            if self._detection_zone:
                self._detection_zone.show()
                self._detection_zone.raise_()

    def eventFilter(self, watched, event):
        """Filter bar Enter/Leave events for hide timer"""
        if watched is self.bar_widget and self._is_enabled:
            if event.type() == QEvent.Type.Enter:
                if self._hide_timer:
                    self._hide_timer.stop()
                animation = getattr(self.bar_widget, "_animation_manager", None)
                if animation is not None and animation.is_hiding():
                    # The pointer came back onto the bar while it slides away: bring it back
                    self.bar_widget.show_bar()
            elif event.type() == QEvent.Type.Leave:
                cursor_pos = QCursor.pos()
                bar_geometry = self.bar_widget.geometry()

                if self._is_mouse_in_safe_zone(cursor_pos, bar_geometry):
                    return False

                if self._hide_timer:
                    self._hide_timer.start(self._autohide_delay)
        return False

    def _is_mouse_in_safe_zone(self, cursor_pos, bar_geometry):
        """Check if mouse is in the gap between bar and detection zone"""
        screen_geometry = self.bar_widget.screen().geometry()
        alignment = self.bar_widget._alignment

        # Calculate mouse position relative to screen
        screen_x = cursor_pos.x() - screen_geometry.x()
        screen_y = cursor_pos.y() - screen_geometry.y()

        # Check if mouse is within screen bounds horizontally
        if screen_x < 0 or screen_x > screen_geometry.width():
            return False

        if alignment["position"] == "top":
            bar_top = bar_geometry.y() - screen_geometry.y()
            return 0 <= screen_y <= bar_top
        else:
            bar_bottom = (bar_geometry.y() + bar_geometry.height()) - screen_geometry.y()
            return bar_bottom <= screen_y <= screen_geometry.height()

    def is_enabled(self):
        """Check if autohide is enabled"""
        return self._is_enabled

    def cleanup(self):
        """Clean up resources"""
        if self._hide_timer:
            self._hide_timer.stop()
        if self._detection_zone:
            self._detection_zone.hide()
            self._detection_zone.deleteLater()
        self._is_enabled = False

        # Restore reserved screen space when autohide is disabled and only if windows_app_bar was enabled
        if hasattr(self.bar_widget, "update_app_bar") and self.bar_widget._window_flags["windows_app_bar"]:
            try:
                SystrayAppBarHelper.execute_without_systray_interference(lambda: self.bar_widget.update_app_bar())
            except Exception as e:
                logging.error("Failed to restore AppBar reservation: %s", e)


class SystrayAppBarHelper:
    """Helper class to manage systray window state during AppBar operations"""

    @staticmethod
    def execute_without_systray_interference(callback):
        """
        Execute a callback with systray timer temporarily killed.
        This prevents systray from continuously reasserting HWND_TOPMOST every 100ms,
        which interferes with AppBar registration by triggering work area recalculations.
        """
        systray_hwnd = SystrayAppBarHelper._get_systray_hwnd()
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE

        try:
            if systray_hwnd:
                # Kill the systray timer (ID 1) to prevent HWND_TOPMOST interference
                KillTimer(systray_hwnd, 1)
                # Demote systray to non-topmost
                SetWindowPos(systray_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)

            callback()

        finally:
            if systray_hwnd:
                # Restore systray to topmost
                SetWindowPos(systray_hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                # Restart the timer (ID 1, 100ms interval)
                SetTimer(systray_hwnd, 1, 100, None)

    @staticmethod
    def _get_systray_hwnd():
        """Get the systray monitor window hwnd if active"""
        try:
            from core.widgets.yasb.systray import SystrayWidget

            if SystrayWidget._systray_client_instance and hasattr(SystrayWidget._systray_client_instance, "hwnd"):
                hwnd = SystrayWidget._systray_client_instance.hwnd
                if hwnd and hwnd != 0:
                    return hwnd
        except Exception:
            pass
        return None


class AppBarManager(QAbstractNativeEventFilter):
    """Central handler for AppBar-related native Windows messages."""

    _instance = None
    _installed = False

    # Default window classes to exclude from fullscreen detection
    EXCLUDED_WINDOW_CLASSES = {
        "Progman",
        "WorkerW",
        "XamlWindow",
        "Shell_TrayWnd",
        "XamlExplorerHostIslandWindow",
        "CEF-OSC-WIDGET",
        "CEFCLIENT",
    }

    # Suffixes for version-dependent window classes (e.g. Qt653QWindowIcon)
    EXCLUDED_WINDOW_CLASS_SUFFIXES = ("QWindowIcon",)

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._bars = {}
            cls._instance._bar_intended_state = {}  # Track intended visibility (True=visible, False=hidden)
            cls._instance._swp_flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            cls._instance._ready = False  # Enabled after first bar registers
            cls._instance._reregister_pending = False  # Coalesces multiple WM_TASKBARCREATED into one
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            super().__init__()
            self._initialized = True

    def _ensure_installed(self):
        """Install the native event filter on first use"""
        if not AppBarManager._installed:
            app = QApplication.instance()
            if app:
                app.installNativeEventFilter(self)
                AppBarManager._installed = True

    def register_bar(self, hwnd: int, bar_widget):
        """Register a bar to receive fullscreen notifications"""
        self._ensure_installed()
        self._bars[hwnd] = bar_widget
        self._bar_intended_state[hwnd] = True  # Initially visible
        self._ready = True

    def unregister_bar(self, hwnd: int):
        """Unregister a bar from receiving fullscreen notifications"""
        self._bars.pop(hwnd, None)
        self._bar_intended_state.pop(hwnd, None)

    def suppress(self):
        """Temporarily suppress WM_TASKBARCREATED handling.
        Used when our own code broadcasts TaskbarCreated (e.g. systray init)."""
        self._ready = False

    def unsuppress(self):
        """Re-enable WM_TASKBARCREATED handling after suppress()."""
        self._ready = True

    def nativeEventFilter(self, eventType, message):
        """Filter native Windows messages for AppBar fullscreen notifications and Explorer restarts"""
        try:
            if eventType == b"windows_generic_MSG":
                msg = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents

                # Handle TaskbarCreated message (Explorer restart)
                if msg.message == WM_TASKBARCREATED:
                    # Only handle if fully initialized and not already scheduled.
                    # WM_TASKBARCREATED is broadcast to all top-level windows, so we
                    # coalesce multiple messages into a single deferred re-registration.
                    if self._ready and not self._reregister_pending and self._bars:
                        self._reregister_pending = True
                        QTimer.singleShot(0, self._deferred_reregister)
                    return False, 0

                if msg.message == APPBAR_CALLBACK_MESSAGE:
                    hwnd = msg.hwnd
                    notification_code = msg.wParam

                    if hwnd in self._bars and notification_code == AppBarNotify.FullScreenApp:
                        is_fullscreen_opening = bool(msg.lParam)
                        self._handle_fullscreen(hwnd, is_fullscreen_opening)
        except Exception:
            pass

        return False, 0

    def _deferred_reregister(self):
        """Deferred handler that runs once per event loop iteration,
        coalescing all WM_TASKBARCREATED messages from the same batch."""
        self._reregister_pending = False
        if not self._ready or not self._bars:
            return

        # Collect bars that actually need re-registration
        bars_to_reregister = []
        needs_systray_workaround = False
        for bw in self._bars.values():
            flags = getattr(bw, "_window_flags", {})
            app_bar = flags.get("windows_app_bar", False)
            fullscreen = getattr(bw, "_hide_on_fullscreen", False)
            if not app_bar and not fullscreen:
                continue
            if hasattr(bw, "_autohide_manager") and bw._autohide_manager and bw._autohide_manager.is_enabled():
                continue
            if not hasattr(bw, "update_app_bar"):
                continue
            bars_to_reregister.append((bw, app_bar))
            if app_bar:
                needs_systray_workaround = True

        if not bars_to_reregister:
            return

        count = len(bars_to_reregister)
        logging.info("AppBarManager need to re-register %d %s", count, "bar" if count == 1 else "bars")

        def reregister():
            for bw, app_bar in bars_to_reregister:
                try:
                    if hasattr(bw, "app_bar_manager") and bw.app_bar_manager:
                        bw.app_bar_manager.remove_appbar()
                    bw.update_app_bar()
                    reason = "space reservation + fullscreen" if app_bar else "fullscreen detection"
                    logging.info("Re-registered AppBar for %s (%s)", getattr(bw, "bar_id", "?"), reason)
                except Exception as e:
                    logging.error("Failed to re-register bar: %s", e)

        if needs_systray_workaround:
            SystrayAppBarHelper.execute_without_systray_interference(reregister)
        else:
            reregister()

    def _is_foreground_excluded(self) -> bool:
        """Check if the foreground window should be excluded from fullscreen detection."""
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False
            # Exclude our own process windows , overlays, modal dialogs and etc.
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == os.getpid():
                return True
            window_class = win32gui.GetClassName(hwnd)
            if window_class in self.EXCLUDED_WINDOW_CLASSES or window_class.endswith(
                self.EXCLUDED_WINDOW_CLASS_SUFFIXES
            ):
                return True
        except Exception:
            pass
        return False

    def _handle_fullscreen(self, hwnd: int, is_fullscreen_opening: bool):
        """Handle ABN_FULLSCREENAPP notification for a bar."""
        bar_widget = self._bars.get(hwnd)
        if not bar_widget:
            return

        # Check if the fullscreen app's window should be excluded
        if is_fullscreen_opening:
            if self._is_foreground_excluded():
                return

        intended_visible = self._bar_intended_state.get(hwnd, True)
        should_hide_bar = getattr(bar_widget, "_hide_on_fullscreen", False)

        # We only need to process if hide_on_fullscreen is enabled
        if not should_hide_bar:
            return

        if is_fullscreen_opening:
            if intended_visible:
                SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, self._swp_flags)
                self._bar_intended_state[hwnd] = False
        else:
            if not intended_visible:
                SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, self._swp_flags)
                self._bar_intended_state[hwnd] = True


class MaximizedWindowWatcher(QObject):
    """Watches for any maximized window on the bar's monitor and toggles autohide accordingly."""

    def __init__(self, bar_widget, parent=None):
        super().__init__(parent)
        self.bar_widget = bar_widget
        self._is_autohide_active = False
        self._had_autohide_before = False

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self._check_maximized_windows)
        self._poll_timer.start()

    def _check_maximized_windows(self):
        """Check if any top-level window is maximized on the bar's monitor."""
        try:
            from core.utils.win32.utils import get_monitor_hwnd, is_window_maximized

            bar_monitor = getattr(self.bar_widget, "monitor_hwnd", None)
            if not bar_monitor:
                return

            has_maximized = False

            def enum_callback(hwnd, _):
                nonlocal has_maximized
                if has_maximized:
                    return False
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    if not win32gui.GetWindowText(hwnd):
                        return True
                    cls_name = win32gui.GetClassName(hwnd)
                    if cls_name in AppBarManager.EXCLUDED_WINDOW_CLASSES:
                        return True
                    if cls_name.endswith(AppBarManager.EXCLUDED_WINDOW_CLASS_SUFFIXES):
                        return True
                    window_monitor = get_monitor_hwnd(hwnd)
                    if window_monitor != bar_monitor:
                        return True
                    if is_window_maximized(hwnd):
                        has_maximized = True
                        return False
                except Exception:
                    pass
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.POINTER(ctypes.c_long))
            callback = WNDENUMPROC(enum_callback)
            user32.EnumWindows(callback, 0)

            if has_maximized and not self._is_autohide_active:
                self._enable_autohide()
            elif not has_maximized and self._is_autohide_active:
                self._disable_autohide()

        except Exception:
            logging.exception("Failed to check maximized windows")

    def _enable_autohide(self):
        """Enable autohide because a maximized window was detected."""
        self._is_autohide_active = True
        # Remember if autohide was already active before we touched it
        self._had_autohide_before = (
            hasattr(self.bar_widget, "_autohide_manager")
            and self.bar_widget._autohide_manager is not None
            and self.bar_widget._autohide_manager.is_enabled()
        )
        if self._had_autohide_before:
            return
        if not self.bar_widget._autohide_manager:
            self.bar_widget._autohide_manager = AutoHideManager(self.bar_widget, self.bar_widget)
        if not self.bar_widget._autohide_manager.is_enabled():
            self.bar_widget._autohide_manager.setup_autohide()

    def _disable_autohide(self):
        """Disable autohide because no maximized windows remain."""
        self._is_autohide_active = False
        # If user already had autohide enabled before, don't disable it
        if self._had_autohide_before:
            self._had_autohide_before = False
            return
        if hasattr(self.bar_widget, "_autohide_manager") and self.bar_widget._autohide_manager:
            self.bar_widget._autohide_manager.cleanup()
            self.bar_widget._autohide_manager = None
        # Ensure bar is visible
        if not self.bar_widget.isVisible():
            self.bar_widget.show()

    def cleanup(self):
        """Clean up resources."""
        self._poll_timer.stop()
        if self._is_autohide_active:
            self._disable_autohide()


class OsThemeManager(QObject):
    """Manages OS theme detection and applies theme classes to widgets"""

    def __init__(self, target_widget: QWidget, parent=None):
        super().__init__(parent)
        self.target_widget = target_widget
        self._is_dark_theme = None

    def detect_os_theme(self) -> bool:
        """Detect if OS is using dark theme"""
        try:
            with winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER) as registry:
                with winreg.OpenKey(registry, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                    value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                    return value == 0
        except Exception as e:
            logging.error("Failed to determine Windows theme: %s", e)
            return False

    def update_theme_class(self):
        """Update the theme class on the target widget"""
        if not self.target_widget:
            return

        is_dark_theme = self.detect_os_theme()
        if is_dark_theme != self._is_dark_theme:
            class_property = self.target_widget.property("class")
            if is_dark_theme:
                class_property += " dark"
            else:
                class_property = class_property.replace(" dark", "")
            self.target_widget.setProperty("class", class_property)
            self._update_styles(self.target_widget)
            self._is_dark_theme = is_dark_theme
            GlobalState.set_dark(is_dark_theme)

    def _update_styles(self, widget):
        """Update styles for widget and its children by unpolishing and re-polishing"""
        refresh_widget_style(widget)
        for child in widget.findChildren(QWidget):
            refresh_widget_style(child)


class BarContextMenu:
    """A class to handle the context menu for a bar."""

    def __init__(self, parent, bar_name, widgets, widget_config_map, autohide_bar):
        self.parent = parent
        self._bar_name = bar_name
        self._widgets = widgets
        self._widget_config_map = widget_config_map
        self._autohide_bar = autohide_bar

    def show(self, position):
        self._menu = AnimatedContextMenu(self.parent)
        apply_qmenu_style(self._menu)
        self._menu.setProperty("class", "context-menu dark" if GlobalState.is_dark() else "context-menu")
        self._menu.aboutToHide.connect(self._on_menu_about_to_hide)

        # Bar info
        bar_info = self._menu.addAction(f"Bar: {self._bar_name}")
        bar_info.setEnabled(False)

        # Widgets menu
        widgets_menu = self._menu.addMenu("Active Widgets")
        apply_qmenu_style(widgets_menu)
        widgets_menu.setProperty(
            "class", "context-menu submenu dark" if GlobalState.is_dark() else "context-menu submenu"
        )
        self._populate_widgets_menu(widgets_menu)

        self._menu.addSeparator()

        # System actions
        task_manager = self._menu.addAction("Task Manager")
        task_manager.triggered.connect(self._open_task_manager)

        # Screenshot action
        screenshot_action = self._menu.addAction("Take Screenshot")
        screenshot_action.triggered.connect(self._take_screenshot)

        self._menu.addSeparator()

        # Bar actions - Check current autohide state dynamically
        current_autohide_enabled = (
            hasattr(self.parent, "_autohide_manager")
            and self.parent._autohide_manager
            and self.parent._autohide_manager.is_enabled()
        )

        if not current_autohide_enabled:
            enable_autohide = self._menu.addAction("Enable Auto Hide")
            enable_autohide.triggered.connect(self._enable_autohide)
        else:
            disable_autohide = self._menu.addAction("Disable Auto Hide")
            disable_autohide.triggered.connect(self._disable_autohide)

        reload_action = self._menu.addAction("Reload Bar")
        reload_action.triggered.connect(partial(reload_application, "Reloading Bar from context menu..."))

        exit_action = self._menu.addAction("Exit")
        exit_action.triggered.connect(partial(exit_application, "Exiting Application from context menu..."))

        global_position = self.parent.mapToGlobal(position)
        self._menu.show_animated(global_position)

    def _on_menu_about_to_hide(self):
        """Called when the context menu is about to hide - restart autohide timer if enabled"""
        try:
            # Check if autohide is enabled and start the hide timer
            if (
                hasattr(self.parent, "_autohide_manager")
                and self.parent._autohide_manager
                and self.parent._autohide_manager.is_enabled()
            ):
                # Start the autohide timer with the configured delay
                if self.parent._autohide_manager._hide_timer:
                    self.parent._autohide_manager._hide_timer.start(self.parent._autohide_manager._autohide_delay)

        except Exception as e:
            logging.error("Failed to restart autohide timer: %s", e)

    def _populate_widgets_menu(self, widgets_menu):
        if not any(self._widgets.get(layout) for layout in ["left", "center", "right"]):
            no_widgets = widgets_menu.addAction("No active widgets")
            no_widgets.setEnabled(False)
            return

        for i, layout_type in enumerate(["left", "center", "right"]):
            # Layout header
            layout_header = widgets_menu.addAction(f"{layout_type.title()} Layout")
            layout_header.setEnabled(False)

            # Add widgets or empty message
            if self._widgets.get(layout_type):
                for widget in self._widgets[layout_type]:
                    self._add_widget_checkbox(widgets_menu, widget)
            else:
                no_widgets = widgets_menu.addAction("  No active widgets")
                no_widgets.setEnabled(False)
            # Add separator after each layout except the last one
            if i < 2:
                widgets_menu.addSeparator()

    def _add_widget_checkbox(self, menu, widget):
        checkbox = QCheckBox(self._get_widget_display_name(widget))
        checkbox.setChecked(widget.isVisible())
        checkbox.setProperty("class", "checkbox")
        checkbox.stateChanged.connect(partial(self._toggle_widget, widget))

        # Container with hover effects
        container = QWidget()
        container.setProperty("class", "menu-checkbox")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(checkbox)
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        # Event filter for hover and click
        def event_filter(obj, event):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                checkbox.toggle()
                return True
            return False

        container.installEventFilter(container)
        container.eventFilter = event_filter

        action = QWidgetAction(menu)
        action.setDefaultWidget(container)
        menu.addAction(action)

    def _toggle_widget(self, widget, enabled):
        try:
            # Add a flag to track manual visibility override
            widget._manual_visibility_override = not enabled
            widget.setVisible(bool(enabled))

            # Store the original show/hide methods if not already stored
            if not hasattr(widget, "_original_show"):
                widget._original_show = widget.show
                widget._original_hide = widget.hide

            # Override show method to respect manual override
            def controlled_show():
                if not getattr(widget, "_manual_visibility_override", False):
                    widget._original_show()

            def controlled_hide():
                widget._original_hide()

            widget.show = controlled_show
            widget.hide = controlled_hide

        except Exception as e:
            logging.error("Failed to toggle widget %s: %s", self._get_widget_display_name(widget), e)

    def _get_widget_display_name(self, widget):
        for layout_type, widget_list in self._widgets.items():
            try:
                index = widget_list.index(widget)
                if (
                    self._widget_config_map
                    and layout_type in self._widget_config_map
                    and index < len(self._widget_config_map[layout_type])
                ):
                    return self._widget_config_map[layout_type][index].replace("_", " ").title()
            except ValueError:
                continue
        return str(widget)

    def _open_task_manager(self):
        try:
            subprocess.Popen("taskmgr", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            logging.error("Failed to open Task Manager: %s", e)

    def _take_screenshot(self):
        """Take a screenshot of the bar with proper padding"""
        try:
            # Get bar and screen geometries
            bar_geometry = self.parent.geometry()
            screen = self.parent.screen()
            screen_geometry = screen.geometry()

            # Get bar padding information
            bar_padding = getattr(self.parent, "_padding", {"top": 0, "bottom": 0, "left": 0, "right": 0})
            bar_alignment = getattr(self.parent, "_alignment", {"position": "top"})

            # Calculate screenshot area with padding
            padding_top = bar_padding.get("top", 0)
            padding_area = 10

            if bar_alignment["position"] == "top":
                # For top bar: start from screen top (y=0) and extend to bar bottom + padding
                screenshot_x = screen_geometry.x()
                screenshot_y = screen_geometry.y()
                screenshot_width = screen_geometry.width()
                screenshot_height = (bar_geometry.y() - screen_geometry.y()) + bar_geometry.height() + padding_area
            else:
                # For bottom bar: start from bar top - padding and extend to screen bottom
                screenshot_x = screen_geometry.x()
                screenshot_y = bar_geometry.y() - padding_top
                screenshot_width = screen_geometry.width()
                screenshot_height = (screen_geometry.y() + screen_geometry.height()) - screenshot_y

            # Take screenshot of the calculated area
            screenshot = screen.grabWindow(
                0,  # Desktop window
                screenshot_x,
                screenshot_y,
                screenshot_width,
                screenshot_height,
            )

            # Create screenshots directory if it doesn't exist
            screenshots_dir = os.path.join(os.path.expanduser("~"), "Pictures", "YASB_Screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)

            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"yasb_bar_{self._bar_name}_{timestamp}.png"
            filepath = os.path.join(screenshots_dir, filename)

            # Save the screenshot
            if not screenshot.save(filepath, "PNG"):
                logging.error("Failed to save screenshot")

            self._screenshot_flash()

        except Exception as e:
            logging.error("Failed to take screenshot: %s", e)

    def _screenshot_flash(self):
        """Create a flashing effect on the bar when taking screenshot"""
        try:
            opacity_effect = QGraphicsOpacityEffect()
            self.parent.setGraphicsEffect(opacity_effect)

            self.flash_animation = QPropertyAnimation(opacity_effect, b"opacity")
            self.flash_animation.setDuration(200)
            self.flash_animation.setStartValue(0.2)
            self.flash_animation.setKeyValueAt(0.25, 1.0)
            self.flash_animation.setKeyValueAt(0.5, 0.2)
            self.flash_animation.setEndValue(1.0)

            self.flash_animation.finished.connect(lambda: self.parent.setGraphicsEffect(None))
            self.flash_animation.start()

        except Exception as e:
            logging.error("Failed to create flash effect: %s", e)

    def _enable_autohide(self):
        """Enable autohide functionality for the bar"""
        try:
            if not hasattr(self.parent, "_autohide_manager") or not self.parent._autohide_manager:
                # Create autohide manager if it doesn't exist
                self.parent._autohide_manager = AutoHideManager(self.parent, self.parent)

            # Setup autohide if not already enabled
            if not self.parent._autohide_manager.is_enabled():
                self.parent._autohide_manager.setup_autohide()

        except Exception as e:
            logging.error("Failed to enable autohide: %s", e)

    def _disable_autohide(self):
        """Disable autohide functionality"""
        try:
            if hasattr(self.parent, "_autohide_manager") and self.parent._autohide_manager:
                self.parent._autohide_manager.cleanup()
                self.parent._autohide_manager = None

            # Ensure bar is visible after disabling autohide
            if not self.parent.isVisible():
                self.parent.show()

        except Exception as e:
            logging.error("Failed to disable autohide: %s", e)


class AutoWidthManager(QObject):
    """Manages auto-width calculation and resize/reposition for bars with width='auto'."""

    def __init__(self, bar_widget: QWidget, parent=None):
        super().__init__(parent)
        self.bar_widget = bar_widget
        self._current_auto_width = 0

    def update(self) -> int:
        """Calculate current auto width from the layout size hint. Returns the new width."""
        layout = self.bar_widget._bar_frame.layout()
        if layout:
            layout.activate()

        requested = max(self.bar_widget._bar_frame.sizeHint().width(), 0)
        available = (
            self.bar_widget._target_screen.geometry().width()
            - self.bar_widget._padding["left"]
            - self.bar_widget._padding["right"]
        )
        new_width = min(requested, available)
        self._current_auto_width = new_width
        return new_width

    def apply(self, new_width: int) -> None:
        """Resize and reposition the bar using the supplied auto width."""
        if new_width < 0:
            return

        bar_height = self.bar_widget._dimensions["height"]
        screen_geometry = self.bar_widget._target_screen.geometry()
        bar_x, bar_y = self.bar_widget.bar_pos(
            new_width,
            bar_height,
            screen_geometry.width(),
            screen_geometry.height(),
        )

        self.bar_widget.setGeometry(bar_x, bar_y, new_width, bar_height)
        self.bar_widget._bar_frame.setGeometry(0, 0, new_width, bar_height)

    def sync(self) -> None:
        """Ensure auto width matches the layout after a DPI/geometry change."""
        previous_width = self._current_auto_width
        new_width = self.update()

        if new_width != previous_width or self.bar_widget.width() != new_width:
            self.apply(new_width)


class BarCliManager(QObject):
    """Handles CLI show/hide/toggle commands for a bar, including app bar reservation management.

    Registering or removing the app bar reservation blocks the UI thread while Windows moves the other
    windows (about half a second), which froze the bar mid-slide. So the reservation waits for the
    animation: the space is reserved once the bar is in place and given back once it is gone. A show
    that arrives while the bar is still sliding out turns it around, and a toggle goes by where the bar
    is heading, not only by whether it is still visible.
    """

    def __init__(self, bar_widget: QWidget, parent=None):
        super().__init__(parent)
        self.bar_widget = bar_widget
        # The state the bar must end in, and the reservation change to make then
        self._pending: tuple[bool, Callable[[], None]] | None = None
        self._listening = False

    def handle(self, action: str, screen_name: str) -> None:
        current_screen_matches = not screen_name or self.bar_widget._target_screen.name() == screen_name
        if not current_screen_matches:
            return

        autohide_active = self.bar_widget._autohide_manager and self.bar_widget._autohide_manager.is_enabled()
        manages_app_bar = self.bar_widget._window_flags["windows_app_bar"] and not autohide_active

        if action == "toggle":
            action = "hide" if self._is_shown() else "show"
        if action == "show":
            self._show(manages_app_bar)
        elif action == "hide":
            self.bar_widget.hide()
            if manages_app_bar:
                self._after_motion(False, self.bar_widget.try_remove_app_bar)

    def _manager(self):
        return getattr(self.bar_widget, "_animation_manager", None)

    def _is_shown(self) -> bool:
        """Visible and not on its way out."""
        manager = self._manager()
        return self.bar_widget.isVisible() and not (manager is not None and manager.is_hiding())

    def _show(self, manages_app_bar: bool) -> None:
        manager = self._manager()
        if self.bar_widget.isVisible() and manager is not None and manager.is_hiding():
            # Still sliding out: turn it around from where it is. Its reservation was never given back.
            self._pending = None
            self.bar_widget.show_bar()
            return
        self.bar_widget.show()
        if manages_app_bar:
            self._after_motion(True, self.bar_widget.update_app_bar)

    def _after_motion(self, shown: bool, change: Callable[[], None]) -> None:
        """Make a reservation change once the bar has finished showing (True) or hiding (False)."""
        manager = self._manager()
        if manager is None or not manager.is_running():
            self._pending = None
            SystrayAppBarHelper.execute_without_systray_interference(change)
            return
        if not self._listening:
            manager.finished.connect(self._on_motion_finished)
            self._listening = True
        self._pending = (shown, change)

    def _on_motion_finished(self, shown: bool) -> None:
        pending, self._pending = self._pending, None
        # A motion that turned around ends in the other state; its change no longer applies
        if pending is not None and pending[0] == shown:
            SystrayAppBarHelper.execute_without_systray_interference(pending[1])
