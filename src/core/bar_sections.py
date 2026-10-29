"""How the bar's three sections share its width: the centre stays centred, the long texts give way first,
and a separator with nothing to separate folds away."""

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtWidgets import QWidget

from core.utils.motion import WidthReveal
from core.utils.text import ElidingLabel

# Between two sections' widgets on a bar without islands to keep apart
_PLAIN_GAP = 16


def _section_widgets(container: QWidget) -> list[QWidget]:
    """A section's widgets, in their order on the bar."""
    layout = container.layout()
    if layout is None:
        return []
    items = (layout.itemAt(index) for index in range(layout.count()))
    return [item.widget() for item in items if item.widget() is not None]


def _is_open(widget: QWidget) -> bool:
    """Shown or opening; a widget closing by its width already counts as gone."""
    reveal = getattr(widget, "_reveal", None)
    if isinstance(reveal, WidthReveal):
        return reveal.is_open()
    return not widget.isHidden()


def _on_toggle(timer: QTimer):
    return lambda _is_open: timer.start()


def _settled_width(widget: QWidget) -> int:
    """The width the layout gives the widget once its motion is over, as the layout measures it: the size
    hint within the widget's own limits (a fixed width, a theme's min-width)."""
    reveal = getattr(widget, "_reveal", None)
    if isinstance(reveal, WidthReveal):
        return round(reveal.settled_width())
    return max(widget.minimumWidth(), min(widget.maximumWidth(), widget.sizeHint().width()))


class SectionSpace(QObject):
    """Keeps the centre section centred on the bar and gives what is left to the long texts.

    Both sides get (bar width - centre) / 2. Inside a side, its ElidingLabels (the window title) get the room
    its other widgets leave, so a long title shortens with "…" instead of pushing the clock off-centre or into
    the next island. The centre's own texts (the media title) shorten only once even the sides at their
    narrowest could not fit beside it. Budgets come from the widgets' natural widths, not their animated
    ones, so a title that has to shorten slides straight to its new width.

    A bar with no centre widgets has nothing to keep in the middle, so its two sides share the whole width.
    A centre whose widgets are only hidden for now (no media playing) keeps its half, so a title does not
    grow and shrink each time they come and go.
    """

    def __init__(self, bar_widget: QWidget, parent=None):
        super().__init__(parent)
        self._frame = bar_widget._bar_frame
        grid = self._frame.layout()
        self._containers = [grid.itemAt(index).widget() for index in range(grid.count())]
        # The sides split what the centre leaves evenly, so the centre lands in the middle
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.update)
        self._widgets: set[QWidget] = set()
        self._frame.installEventFilter(self)
        for container in self._containers:
            container.installEventFilter(self)
            for widget in _section_widgets(container):
                # Installed after the widget's own width reveal, so this filter sees each event first
                widget.installEventFilter(self)
                self._widgets.add(widget)
                reveal = getattr(widget, "_reveal", None)
                if isinstance(reveal, WidthReveal):
                    reveal.toggled.connect(_on_toggle(self._timer))
        self.update()

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.LayoutRequest and obj in self._widgets:
            # A widget's content changed: share the room now, before its width reveal reads its new width,
            # so a new title that must shorten heads straight for its final width (no detour)
            self.update()
        elif kind in (QEvent.Type.Resize, QEvent.Type.LayoutRequest, QEvent.Type.Show, QEvent.Type.Hide):
            self._timer.start()
        return False

    def _gap(self) -> int:
        island_gap = getattr(self._frame, "island_gap", None)
        return island_gap() if island_gap is not None else _PLAIN_GAP

    @staticmethod
    def _measure(container: QWidget) -> tuple[int, list[ElidingLabel]]:
        """The width a section's open widgets take besides their texts that can shorten, and those texts."""
        layout = container.layout()
        margins = layout.contentsMargins() if layout is not None else None
        fixed = container.width() - container.contentsRect().width()
        if margins is not None:
            fixed += margins.left() + margins.right()
        labels: list[ElidingLabel] = []
        for widget in _section_widgets(container):
            if not _is_open(widget):
                continue
            width = _settled_width(widget)
            for label in widget.findChildren(ElidingLabel):
                if label.isHidden() or not label.elides():
                    continue
                width -= label.sizeHint().width()
                labels.append(label)
            fixed += max(0, width)
        return fixed, labels

    @staticmethod
    def _give(labels: list[ElidingLabel], room: int) -> None:
        """Share room between the labels: a short one keeps its whole text, the others split the rest.

        Each keeps the room it was given as its budget, so a new, longer text shortens at once."""
        pending = sorted(labels, key=lambda label: label.full_width())
        for index, label in enumerate(pending):
            share = room // (len(pending) - index) if room > 0 else 0
            budget = max(label.least_width(), share)
            label.set_budget(budget)
            room -= min(label.full_width(), budget)

    def update(self) -> None:
        try:
            self._share()
        except RuntimeError:
            pass  # a widget already deleted while its bar closes (a reload): nothing left to share

    def _share(self) -> None:
        width = self._frame.width()
        if width <= 0 or len(self._containers) != 3:
            return
        (left, left_labels), (centre, centre_labels), (right, right_labels) = map(self._measure, self._containers)
        gap = self._gap()
        if not _section_widgets(self._containers[1]):
            # Only a left and a right section: their texts share what both sides leave, less one gap
            self._give(left_labels + right_labels, width - left - centre - right - gap)
            return

        def least(labels):
            return sum(label.least_width() for label in labels)

        # The centre's texts get what the sides at their narrowest leave beside it
        narrowest_side = max(left + least(left_labels), right + least(right_labels))
        self._give(centre_labels, width - 2 * (narrowest_side + gap) - centre)
        centre += sum(label.sizeHint().width() for label in centre_labels)
        # Each side gets half of the rest, less the gap that keeps its island apart from the centre's
        side = (width - centre) // 2 - gap
        self._give(left_labels, side - left)
        self._give(right_labels, side - right)


class SeparatorFolder(QObject):
    """Folds away a separator with nothing to separate: one at a section's start or end, or right after
    another one, as when the widgets between two separators are hidden (komorebi not running leaves
    "| | |"). It closes and reopens by its width, at the same time as the widget that caused it.

    A separator is a widget whose class has "separator" in it ("separator-widget" in the docs' configs).
    """

    def __init__(self, bar_widget: QWidget, parent=None):
        super().__init__(parent)
        grid = bar_widget._bar_frame.layout()
        self._sections = [_section_widgets(grid.itemAt(index).widget()) for index in range(grid.count())]
        self._separators: set[QWidget] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.update)
        for section in self._sections:
            for widget in section:
                if self._is_separator(widget):
                    self._separators.add(widget)
                    if not isinstance(getattr(widget, "_reveal", None), WidthReveal):
                        widget._reveal = WidthReveal(widget, is_open=not widget.isHidden())
                else:
                    widget.installEventFilter(self)
                    reveal = getattr(widget, "_reveal", None)
                    if isinstance(reveal, WidthReveal):
                        reveal.toggled.connect(_on_toggle(self._timer))
        if self._separators:
            self.update()

    @staticmethod
    def _is_separator(widget: QWidget) -> bool:
        frame = getattr(widget, "_widget_frame", None) or widget
        classes = str(frame.property("class") or "").split()
        return any(name == "separator" or name.startswith("separator-") or name.endswith("-separator") for name in classes)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self._timer.start()
        return False

    def update(self) -> None:
        try:
            self._fold()
        except RuntimeError:
            pass  # a widget already deleted while its bar closes (a reload)

    def _fold(self) -> None:
        for section in self._sections:
            keep: set[QWidget] = set()
            seen, candidate = False, None
            for widget in section:
                if widget in self._separators:
                    # The first separator after something shown; it stays if something follows
                    if seen and candidate is None:
                        candidate = widget
                elif _is_open(widget):
                    if candidate is not None:
                        keep.add(candidate)
                        candidate = None
                    seen = True
            for widget in section:
                if widget in self._separators:
                    if widget in keep:
                        widget._reveal.open()
                    else:
                        widget._reveal.close()
