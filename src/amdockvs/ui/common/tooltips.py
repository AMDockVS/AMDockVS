"""Long tooltips wrap, whoever set them.

Qt word-wraps a plain-text tooltip only once it is wider than the whole screen. Anything a bit
shorter shows as a single line running most of the way across it: "Include cofactors" spanned
~1590 px on a 1920 px display while "Charge metal ions", a little longer, wrapped. A long
plain tooltip is broken into lines here instead - for every widget and every item-view cell,
without touching the hundreds of places that set one. By characters, not left to Qt's rich-text
sizing: that settles on a column ~250 px wide, a word or four a line.
"""
from __future__ import annotations

import textwrap

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtWidgets import QAbstractItemView, QApplication, QToolTip, QWidget

# A line longer than this is broken; the lines run to about this many characters (~450 px).
WRAP_AFTER = 90
LINE = 72


def wrapped(text: str) -> str | None:
    """``text`` broken into lines of about LINE characters, or None when it needs nothing: no
    line runs past WRAP_AFTER, or it is rich text (which Qt wraps itself). The line breaks it
    already has stay; a single word longer than a line is never split."""
    lines = (text or "").split("\n")
    if max((len(line) for line in lines), default=0) <= WRAP_AFTER or GuiQt.mightBeRichText(text):
        return None
    return "\n".join(textwrap.fill(line, LINE, break_long_words=False, break_on_hyphens=False) or line
                     for line in lines)


class _WrapLongToolTips(QObject):
    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if event.type() != QEvent.Type.ToolTip or not isinstance(watched, QWidget):
            return False
        view = watched.parent()
        if isinstance(view, QAbstractItemView) and watched is view.viewport():
            index = view.indexAt(event.pos())
            text = wrapped(str(index.data(Qt.ItemDataRole.ToolTipRole) or "")) if index.isValid() else None
            if text is None:
                return False
            QToolTip.showText(event.globalPos(), text, watched, view.visualRect(index))
            return True
        text = wrapped(watched.toolTip())
        if text is None:
            return False  # nothing of its own (Qt asks the parent next), or nothing to wrap
        QToolTip.showText(event.globalPos(), text, watched)
        return True


def install(app: QApplication) -> None:
    """Wrap long tooltips application-wide. Once per application."""
    if getattr(app, "_wrap_long_tooltips", None) is None:
        app._wrap_long_tooltips = _WrapLongToolTips(app)
        app.installEventFilter(app._wrap_long_tooltips)
