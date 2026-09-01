from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QProxyStyle,
    QPushButton,
    QStackedWidget,
    QStyle,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget, QSplitter,
)


class _PreviewTabStyle(QProxyStyle):
    """Italic title for the preview tab.

    Qt has no per-tab font, so the label is drawn with an italic painter font; the tab is
    identified by its text because that is all the style option carries.
    """

    def __init__(self):
        super().__init__()
        self.preview_title = ""

    def drawControl(self, element, option, painter, widget=None):
        if (element == QStyle.CE_TabBarTabLabel and self.preview_title
                and getattr(option, "text", "") == self.preview_title):
            font = painter.font()
            font.setItalic(True)
            painter.setFont(font)
        super().drawControl(element, option, painter, widget)


class MainContentWidget(QStackedWidget):
    current_view_changed = Signal(object)
    view_open_state_changed = Signal(str, bool)
    open_project_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._view_factories: dict[str, tuple[str, Callable[[], QWidget]]] = {}
        self._open_tabs: dict[str, QWidget] = {}
        self._close_callbacks: dict[str, Callable[[], None]] = {}
        self._stale: set[str] = set()  # refreshed while hidden -> reload on the way back
        # Ephemeral views (QSAR models, binding sites, complexes) share ONE tab slot: opening
        # any of them closes whichever held it. Without this they pile up, which is how the
        # tab bar filled with tables nobody meant to keep.
        self._preview_views: set[str] = set()
        self._preview_open: str | None = None
        self._preview_style = _PreviewTabStyle()

        self.welcome = self._build_welcome()
        self.addWidget(self.welcome)

        self.central_widget = QSplitter(Qt.Orientation.Vertical, self)
        self.addWidget(self.central_widget)

        self.main_content_tabs = QTabWidget(self)
        self.main_content_tabs.setTabsClosable(True)
        self.main_content_tabs.setMovable(True)
        self.main_content_tabs.currentChanged.connect(self._on_current_tab_changed)
        self.main_content_tabs.tabCloseRequested.connect(self.on_tab_close)
        self._preview_style.setParent(self.main_content_tabs)  # style is not owned by the widget
        self.main_content_tabs.tabBar().setStyle(self._preview_style)
        self.central_widget.addWidget(self.main_content_tabs)

        self.aux_widget = QStackedWidget(self)
        self.central_widget.addWidget(self.aux_widget)
        # ponytail: QSplitter hides the handle of the hidden widget; no need to remove it from the splitter
        self.aux_widget.setVisible(False)

        self.setCurrentWidget(self.welcome)

    def _build_welcome(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(8)

        title = QLabel("AMDockVS")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        hint = QLabel("Open or create a project to get started.")
        hint.setAlignment(Qt.AlignCenter)

        button = QPushButton("Open or Create Project…")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.open_project_requested.emit)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(8)
        layout.addWidget(button, alignment=Qt.AlignCenter)
        return page

    def register_view(
        self,
        view_id: str,
        title: str,
        factory: Callable[[], QWidget],
        *,
        on_close: Callable[[], None] | None = None,
        preview: bool = False,
    ) -> None:
        self._view_factories[view_id] = (title, factory)
        if on_close is not None:
            self._close_callbacks[view_id] = on_close
        if preview:
            self._preview_views.add(view_id)

    def open_or_focus_view(self, view_id: str) -> QWidget:
        current = self._open_tabs.get(view_id)
        if current is not None:
            self.main_content_tabs.setCurrentWidget(current)
            self.setCurrentWidget(self.central_widget)
            return current

        registered = self._view_factories.get(view_id)
        if registered is None:
            raise KeyError(f"View '{view_id}' is not registered.")

        if view_id in self._preview_views and self._preview_open not in (None, view_id):
            self.close_view(self._preview_open)  # one slot: the previous preview gives it up

        title, factory = registered
        widget = factory()
        widget.setProperty("view_id", view_id)
        self._open_tabs[view_id] = widget
        index = self.main_content_tabs.addTab(widget, title)
        self._set_tab_close_cursor(index)
        if view_id in self._preview_views:
            self._preview_open = view_id
            self._preview_style.preview_title = title
        self.main_content_tabs.setCurrentWidget(widget)
        self.setCurrentWidget(self.central_widget)
        self.view_open_state_changed.emit(view_id, True)
        return widget

    def _set_tab_close_cursor(self, index: int) -> None:
        """Use a hand cursor only on Qt's native close button."""
        tab_bar = self.main_content_tabs.tabBar()
        for position in (
            QTabBar.ButtonPosition.LeftSide,
            QTabBar.ButtonPosition.RightSide,
        ):
            button = tab_bar.tabButton(index, position)
            if button is not None:
                button.setCursor(Qt.CursorShape.PointingHandCursor)

    def open_view(self, view_id: str) -> QWidget | None:
        return self._open_tabs.get(view_id)

    def build_view_widget(self, view_id: str) -> tuple[str, QWidget]:
        """Build a fresh widget from a registered factory WITHOUT adding a tab.
        Used for tool views that mount in the left tool panel instead of a tab."""
        registered = self._view_factories.get(view_id)
        if registered is None:
            raise KeyError(f"View '{view_id}' is not registered.")
        title, factory = registered
        widget = factory()
        widget.setProperty("view_id", view_id)
        return title, widget

    def close_view(self, view_id: str) -> None:
        widget = self._open_tabs.get(view_id)
        if widget is None:
            return
        index = self.main_content_tabs.indexOf(widget)
        if index >= 0:
            self.on_tab_close(index)

    def refresh_open_view(self, view_id: str) -> None:
        """Reload a view, but only if it is on screen. A finished job refreshes every open
        tab; without this, N background tabs pay N table reloads nobody is looking at.
        What we skip is remembered and reloaded when the tab comes back."""
        widget = self.open_view(view_id)
        if widget is None:
            return
        if not widget.isVisible():
            self._stale.add(view_id)
            return
        self._stale.discard(view_id)
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()

    def on_tab_close(self, index: int) -> None:
        widget = self.main_content_tabs.widget(index)
        if widget is None:
            return
        view_id = next((key for key, value in self._open_tabs.items() if value is widget), None)
        self.main_content_tabs.removeTab(index)
        if view_id is not None:
            self._open_tabs.pop(view_id, None)
            self._stale.discard(view_id)
            if view_id == self._preview_open:
                self._preview_open = None
                self._preview_style.preview_title = ""
            self.view_open_state_changed.emit(view_id, False)
            callback = self._close_callbacks.get(view_id)
            if callback is not None:
                callback()
        widget.deleteLater()
        if self.main_content_tabs.count() == 0:
            self.setCurrentWidget(self.welcome)

    def current_view_id(self) -> str | None:
        widget = self.main_content_tabs.currentWidget()
        if widget is None:
            return None
        view_id = widget.property("view_id")
        if not view_id:
            return None
        return str(view_id)

    def _on_current_tab_changed(self, _index: int) -> None:
        view_id = self.current_view_id()
        if view_id is not None and view_id in self._stale:
            self.refresh_open_view(view_id)
        self.current_view_changed.emit(view_id)
