from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QThread
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QSplashScreen

# ponytail: the protein artwork is a bundled asset; the frame around it is painted at runtime.
_WIDTH = 640
_HEIGHT = 380
_RADIUS = 18
_BG_TOP = QColor("#0A0F1E")
_BG_BOTTOM = QColor("#111A30")
_ACCENT = QColor("#5EEAD4")  # teal — reads "molecular / next-gen"
_TAGLINE = "For Virtual Screening"
_IMAGE_CREDIT = "ArtWork by Paco Enguita"

_ASSET_DIR = Path(__file__).resolve().parent / "resources" / "images"
# ponytail: single artwork shipped; keep the list so more can be dropped in and randomised.
_IMAGE_LIST = [
    "image1.png",
    "image1-1.png",
    "image1-4.png",
    "image1-6.png",
    "image1-15.png",
    "image1-67.png",
]
_PROTEIN_IMAGE = _ASSET_DIR / random.choice(_IMAGE_LIST)


def _load_image(image_path: Path) -> QPixmap:
    pixmap = QPixmap(str(image_path))
    if pixmap.isNull():
        print(f"[Splash] Could not load image: {image_path}")
    return pixmap


def _scale_pixmap(source: QPixmap, target_width: int, target_height: int, *, zoom: float = 1.0) -> QPixmap:
    """Scales keeping the aspect ratio (KeepAspectRatioByExpanding covers the whole target)."""
    if source.isNull():
        return QPixmap()
    width = max(1, round(target_width * zoom))
    height = max(1, round(target_height * zoom))
    return source.scaled(width, height, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)


def _apply_fast_elliptical_mask(
        source: QPixmap,
        *,
        maximum_alpha: int = 205,
        center_x: float = 0.59,
        center_y: float = 0.50,
        radius_x: float = 0.54,
        radius_y: float = 0.50,
        solid_until: float = 0.22,
        tint_color: QColor = QColor("#111A30"),
        edge_tint_alpha: int = 210,
) -> QPixmap:
    """Darkens the periphery and then fades it out with an elliptical alpha mask, in a single pass."""
    if source.isNull():
        return QPixmap()

    width = source.width()
    height = source.height()

    result = QPixmap(source.size())
    result.fill(Qt.transparent)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawPixmap(0, 0, source)

    center = QPointF(width * center_x, height * center_y)
    radius_x_px = max(1.0, width * radius_x)
    radius_y_px = max(1.0, height * radius_y)
    base_radius = max(radius_x_px, radius_y_px)

    # 1. Darken the periphery before making it transparent.
    tint_gradient = QRadialGradient(QPointF(base_radius, base_radius), base_radius)
    r, g, b = tint_color.red(), tint_color.green(), tint_color.blue()
    tint_gradient.setColorAt(0.0, QColor(r, g, b, 0))
    tint_gradient.setColorAt(solid_until, QColor(r, g, b, 0))
    tint_gradient.setColorAt(0.65, QColor(r, g, b, round(edge_tint_alpha * 0.35)))
    tint_gradient.setColorAt(0.88, QColor(r, g, b, round(edge_tint_alpha * 0.85)))
    tint_gradient.setColorAt(1.0, QColor(r, g, b, edge_tint_alpha))

    painter.save()
    painter.translate(center)
    painter.scale(radius_x_px / base_radius, radius_y_px / base_radius)
    painter.translate(-base_radius, -base_radius)
    painter.fillRect(QRectF(0, 0, base_radius * 2, base_radius * 2), tint_gradient)
    painter.restore()

    # 2. Apply the elliptical alpha mask.
    alpha_mask = QPixmap(source.size())
    alpha_mask.fill(Qt.transparent)

    mask_painter = QPainter(alpha_mask)
    mask_painter.setRenderHint(QPainter.Antialiasing, True)

    alpha_gradient = QRadialGradient(QPointF(base_radius, base_radius), base_radius)
    alpha_gradient.setColorAt(0.0, QColor(255, 255, 255, maximum_alpha))
    alpha_gradient.setColorAt(solid_until, QColor(255, 255, 255, maximum_alpha))
    alpha_gradient.setColorAt(0.55, QColor(255, 255, 255, round(maximum_alpha * 0.78)))
    alpha_gradient.setColorAt(0.78, QColor(255, 255, 255, round(maximum_alpha * 0.28)))
    alpha_gradient.setColorAt(0.94, QColor(255, 255, 255, 8))
    alpha_gradient.setColorAt(1.0, QColor(255, 255, 255, 0))

    mask_painter.save()
    mask_painter.translate(center)
    mask_painter.scale(radius_x_px / base_radius, radius_y_px / base_radius)
    mask_painter.translate(-base_radius, -base_radius)
    mask_painter.fillRect(QRectF(0, 0, base_radius * 2, base_radius * 2), alpha_gradient)
    mask_painter.restore()
    mask_painter.end()

    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawPixmap(0, 0, alpha_mask)
    painter.end()
    return result


def _rotate_pixmap(source: QPixmap, angle_degrees: float) -> QPixmap:
    """Rotates an image that already carries transparency at its edges."""
    if source.isNull() or angle_degrees == 0:
        return source
    transform = QTransform()
    transform.rotate(angle_degrees)
    return source.transformed(transform, Qt.SmoothTransformation)


def _draw_protein_image(
        painter: QPainter,
        image_path: Path,
        target_rect: QRectF,
        *,
        rotation_degrees: float = -15.0,
        zoom: float = 1.15,
        maximum_alpha: int = 205,
        mask_center_x: float = 0.57,
        mask_center_y: float = 0.50,
        mask_radius_x: float = 0.52,
        mask_radius_y: float = 0.50,
        solid_until: float = 0.30,
) -> None:
    source = _load_image(image_path)
    if source.isNull():
        return

    scaled = _scale_pixmap(source, round(target_rect.width()), round(target_rect.height()), zoom=zoom)
    faded = _apply_fast_elliptical_mask(
        scaled,
        maximum_alpha=maximum_alpha,
        center_x=mask_center_x,
        center_y=mask_center_y,
        radius_x=mask_radius_x,
        radius_y=mask_radius_y,
        solid_until=solid_until,
        tint_color=_BG_BOTTOM,
        edge_tint_alpha=220,
    )
    rotated = _rotate_pixmap(faded, rotation_degrees)
    if rotated.isNull():
        return

    draw_x = target_rect.center().x() - rotated.width() / 2.0
    draw_y = target_rect.center().y() - rotated.height() / 2.0

    painter.save()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawPixmap(QPointF(draw_x, draw_y), rotated)
    painter.restore()


def _build_pixmap() -> QPixmap:
    screen = QApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen else 1.0

    pixmap = QPixmap(round(_WIDTH * ratio), round(_HEIGHT * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    rect = QRectF(0, 0, _WIDTH, _HEIGHT)

    clip = QPainterPath()
    clip.addRoundedRect(rect, _RADIUS, _RADIUS)
    painter.setClipPath(clip)

    background = QLinearGradient(0, 0, 0, _HEIGHT)
    background.setColorAt(0.0, _BG_TOP)
    background.setColorAt(1.0, _BG_BOTTOM)
    painter.fillRect(rect, background)

    glow = QRadialGradient(_WIDTH * 0.16, _HEIGHT * 0.18, _WIDTH * 0.55)
    glow.setColorAt(0.0, QColor(51, 225, 208, 55))
    glow.setColorAt(1.0, QColor(51, 225, 208, 0))
    painter.fillRect(rect, glow)

    # The rectangle overflows the canvas; the rounded clip trims the outer parts.
    _draw_protein_image(
        painter,
        _PROTEIN_IMAGE,
        QRectF(_WIDTH * 0.42, -20, _WIDTH * 0.68, _HEIGHT + 40),
        rotation_degrees=-15.0,
        zoom=1.15,
        maximum_alpha=200,
        mask_center_x=0.59,
        mask_center_y=0.50,
        mask_radius_x=0.54,
        mask_radius_y=0.50,
        solid_until=0.22,
    )

    painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
    painter.drawLine(QPointF(0, 1), QPointF(_WIDTH, 1))

    # Wordmark: "AMDock-" white + "VS" accented.
    title_font = QFont(painter.font().family(), 0, QFont.Bold)
    title_font.setPixelSize(58)
    painter.setFont(title_font)
    metrics = QFontMetricsF(title_font)
    base_x, base_y = 46.0, _HEIGHT * 0.52
    painter.setPen(QColor("#F4F7FB"))
    painter.drawText(QPointF(base_x, base_y), "AMDock-")
    painter.setPen(_ACCENT)
    painter.drawText(QPointF(base_x + metrics.horizontalAdvance("AMDock-"), base_y), "VS")

    # Tagline: light, letter-spaced.
    tag_font = QFont(painter.font().family())
    tag_font.setPixelSize(17)
    tag_font.setLetterSpacing(QFont.AbsoluteSpacing, 2.2)
    painter.setFont(tag_font)
    painter.setPen(QColor(170, 185, 205))
    painter.drawText(QPointF(base_x + 3, base_y + 34), _TAGLINE)

    # Accent underline under the wordmark.
    painter.setPen(QPen(_ACCENT, 3))
    painter.drawLine(QPointF(base_x + 3, base_y + 12), QPointF(base_x + 66, base_y + 12))

    # Permanent image credit, bottom-right.
    credit_font = QFont(painter.font().family())
    credit_font.setBold(True)
    credit_font.setPixelSize(10)
    painter.setFont(credit_font)
    painter.setPen(QColor(235, 239, 246, 145))
    painter.drawText(
        QRectF(_WIDTH * 0.50, _HEIGHT - 28, _WIDTH * 0.50 - 14, 20),
        Qt.AlignRight | Qt.AlignVCenter,
        _IMAGE_CREDIT,
    )

    painter.end()
    return pixmap


class Splash(QSplashScreen):
    MIN_VISIBLE_MS = 3500  # keep the splash up long enough to read (measured from creation)

    def __init__(self) -> None:
        super().__init__(_build_pixmap())
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._shown = QElapsedTimer()

    def status(self, text: str) -> None:
        self.showMessage(
            f"  {text}",
            Qt.AlignBottom | Qt.AlignLeft,
            QColor(150, 165, 185),
        )
        QApplication.processEvents()

    def finish(self, window) -> None:  # type: ignore[override]
        # Hold the splash until MIN_VISIBLE_MS has passed, keeping the event loop alive.
        remaining = self.MIN_VISIBLE_MS - (self._shown.elapsed() if self._shown.isValid() else 0)
        while remaining > 0:
            QApplication.processEvents()
            QThread.msleep(min(30, remaining))
            remaining = self.MIN_VISIBLE_MS - self._shown.elapsed()
        super().finish(window)


def create_splash() -> Splash:
    splash = Splash()
    splash.show()
    splash._shown.start()
    # Force the first frame now so the splash is visible before the heavy imports block the thread.
    splash.repaint()
    QApplication.processEvents()
    return splash
