"""Text helpers for labels that show other apps' text (window titles, media titles)."""

from PyQt6.QtCore import QEvent, Qt, QTextBoundaryFinder
from PyQt6.QtWidgets import QLabel

ELLIPSIS = "…"


def truncate_text(text: str, limit: int, ellipsis: str = "...") -> str:
    """text cut to at most limit characters plus the ellipsis, between two characters a reader sees.

    A plain slice can split an emoji sequence (a family, a skin tone), a flag or an accent from its letter,
    which then shows as a different emoji, a lone letter box or a bare letter.
    """
    limit = max(0, limit)
    if len(text) <= limit:
        return text
    # Qt counts UTF-16 units and Python code points: an emoji outside the BMP is two units but one character
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
    finder.setPosition(offsets[limit])
    if not finder.isAtBoundary():
        finder.toPreviousBoundary()
    # A grapheme boundary is always a code point boundary
    return text[: offsets.index(max(0, finder.position()))] + ellipsis


class ElidingLabel(QLabel):
    """A one-line label for other apps' text that gives up width first when the bar runs short.

    The bar gives it a budget in pixels (set_budget); past it the label shows the start of its text and "…",
    with the whole text as a tooltip. Without a budget, or as rich text, it is a plain QLabel. text() is
    always the whole text; shown_text() is what is on screen.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self._budget: int | None = None

    def setText(self, text: str) -> None:
        self._full = text or ""
        self._refresh()

    def text(self) -> str:
        return self._full

    def shown_text(self) -> str:
        return super().text()

    def set_budget(self, budget: int | None) -> None:
        if budget != self._budget:
            self._budget = budget
            self._refresh()

    def elides(self) -> bool:
        """Whether it can shorten at all: rich text keeps its markup whole."""
        return self.textFormat() != Qt.TextFormat.RichText

    def full_width(self) -> int:
        """The width the whole text asks for, padding included."""
        return self._chrome() + self.fontMetrics().horizontalAdvance(self._full)

    def least_width(self) -> int:
        """The narrowest it goes: the ellipsis alone (nothing at all without text)."""
        return self._chrome() + self.fontMetrics().horizontalAdvance(ELLIPSIS) if self._full else 0

    def _chrome(self) -> int:
        # What the label adds around its text (the theme's padding), measured rather than assumed
        return max(0, super().sizeHint().width() - self.fontMetrics().horizontalAdvance(super().text()))

    def _refresh(self) -> None:
        shown = self._full
        if self._budget is not None and self._full and self.elides():
            room = self._budget - self._chrome()
            metrics = self.fontMetrics()
            if metrics.horizontalAdvance(self._full) > room:
                shown = metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, room)) or ELLIPSIS
        if shown != super().text():
            super().setText(shown)
            # The widgets above cache their size hints until their own layout requests come round; renew
            # them now, so a width reveal reading its widget next gets the new width, not the last one
            parent = self.parentWidget()
            while parent is not None and not parent.isWindow():
                parent.updateGeometry()
                parent = parent.parentWidget()
        tooltip = getattr(self, "_tooltip_filter", None)
        if shown != self._full:
            # Imported here: core.utils.tooltip imports core.bar_helper, which this module must not wait on
            from core.utils.tooltip import set_tooltip

            set_tooltip(self, self._full)
        elif tooltip is not None and tooltip.tooltip_text:
            tooltip.update_tooltip_text("")
            tooltip._hide_tooltip()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # The theme's font arrives with the stylesheet, after the first budget
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._refresh()
