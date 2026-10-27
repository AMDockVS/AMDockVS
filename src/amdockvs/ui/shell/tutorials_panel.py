"""The application-specific tutorial library shown beside the workspace."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout


class TutorialsPanel(QFrame):
    start_requested = Signal()
    close_requested = Signal()
    startup_preference_changed = Signal(bool)

    def __init__(self, *, hide_on_startup: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("tutorialsPanel")
        self.setFixedWidth(320)
        self.setStyleSheet("""
            QFrame#tutorialsPanel { background: palette(base); border-right: 1px solid palette(mid); }
            QFrame#tutorialCard { background: palette(window); border: 1px solid palette(mid); border-radius: 10px; }
            QLabel#tutorialKicker { color: palette(placeholder-text); font-size: 11px; font-weight: 700; }
            QLabel#tutorialTitle { font-size: 21px; font-weight: 700; }
            QLabel#tutorialCardTitle { font-size: 17px; font-weight: 700; }
            QToolButton#tutorialClose { border: none; border-radius: 4px; font-size: 23px; padding: 0; }
            QToolButton#tutorialClose:hover { background: palette(midlight); }
            QPushButton#tutorialStart { background: palette(highlight); color: palette(highlighted-text);
                border: none; border-radius: 6px; padding: 10px 14px; font-weight: 600; }
            QPushButton#tutorialStart:hover { border: 1px solid palette(text); }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        header = QHBoxLayout()
        kicker = QLabel("TUTORIALS")
        kicker.setObjectName("tutorialKicker")
        header.addWidget(kicker)
        header.addStretch()
        self.close_button = QToolButton(self)
        self.close_button.setObjectName("tutorialClose")
        self.close_button.setText("×")
        self.close_button.setToolTip("Close tutorials panel")
        self.close_button.setAccessibleName("Close tutorials panel")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        title = QLabel("Learn by doing")
        title.setObjectName("tutorialTitle")
        layout.addWidget(title)
        intro = QLabel("Build confidence with guided, hands-on examples.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addSpacing(6)

        card = QFrame(self)
        card.setObjectName("tutorialCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 18, 18, 18)
        card_layout.setSpacing(12)
        detail = QLabel("BEGINNER  ·  8 STEPS")
        detail.setObjectName("tutorialKicker")
        card_layout.addWidget(detail)
        heading = QLabel("Your first docking")
        heading.setObjectName("tutorialCardTitle")
        card_layout.addWidget(heading)
        description = QLabel("Create a project and dock SAR405 into Vps34, from importing molecules to running your first calculation.")
        description.setWordWrap(True)
        card_layout.addWidget(description)
        example = QLabel("Vps34  ·  SAR405  ·  4OYS")
        example.setObjectName("tutorialKicker")
        card_layout.addWidget(example)
        self.start_button = QPushButton("Start tutorial  →", card)
        self.start_button.setObjectName("tutorialStart")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start_requested.emit)
        card_layout.addWidget(self.start_button)
        layout.addWidget(card)
        layout.addStretch()
        self.hide_on_startup_checkbox = QCheckBox("Do not show again", self)
        self.hide_on_startup_checkbox.setToolTip("Hide this panel at startup. You can reopen it from View.")
        self.hide_on_startup_checkbox.setChecked(hide_on_startup)
        self.hide_on_startup_checkbox.toggled.connect(self.startup_preference_changed.emit)
        layout.addWidget(self.hide_on_startup_checkbox)
