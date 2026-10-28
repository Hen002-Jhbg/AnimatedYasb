"""Text helpers for labels that show other apps' text (window titles, media titles)."""

from PyQt6.QtCore import QTextBoundaryFinder


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
