"""界面公用的小工具：图标、头像、二维码图片、状态颜色和深色 / 浅色、常用控件、后台任务。"""

from __future__ import annotations

import html
import math
import re
import time
from collections.abc import Callable

import qrcode
from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QWidget,
)
from shiboken6 import isValid

from .config import THEME_DARK, THEME_LIGHT

BILI_PINK = "#FB7299"
PAUSED_GRAY = "#8C959F"


def circle_avatar(source: QPixmap | None, size: int) -> QPixmap:
    """把头像裁成圆形；没有头像时画一个灰色圆圈里的人形。"""
    ratio = 2  # 按两倍画，高分屏上也清楚
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    full = QRectF(0, 0, size * ratio, size * ratio)
    path = QPainterPath()
    path.addEllipse(full)
    painter.setClipPath(path)
    if source is not None and not source.isNull():
        scaled = source.scaled(
            size * ratio, size * ratio, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
        )
        painter.drawPixmap(0, 0, scaled)
    else:
        painter.fillRect(full, QColor(PAUSED_GRAY))
        painter.setBrush(QColor("white"))
        painter.setPen(Qt.PenStyle.NoPen)
        s = size * ratio
        painter.drawEllipse(QRectF(s * 0.34, s * 0.18, s * 0.32, s * 0.32))  # 头
        painter.drawEllipse(QRectF(s * 0.16, s * 0.56, s * 0.68, s * 0.6))  # 肩膀
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


# ---------------------------------------------------------------- 常用控件


def format_duration(seconds: float) -> str:
    """时长写成“1 小时 5 分”“12 分 30 秒”“45 秒”。"""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def hint_label(text: str = "") -> QLabel:
    """灰色的小字说明。只显示纯文字（见 plain_tip）。"""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {StatusColors().muted};")
    return label


def set_colored(label: QLabel, text: str, color: str = "") -> None:
    """显示一句状态（常带着主播昵称、直播标题、B 站返回的提示），按纯文字显示。"""
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setStyleSheet(f"color: {color};" if color else "")


def plain_tip(text: str) -> str:
    """鼠标提示按纯文字显示。凡是可能带着 B 站来的文字（直播标题、主播昵称、表情名字等）的提示都要过一下这里。

    Qt 看到像 HTML 的提示文字会当网页显示，里面的 <img> 还会去读本机文件、甚至连别人电脑上的共享文件夹
    （Windows 会顺带发出登录验证信息）。主播改个直播标题就能做到，所以转义成普通文字，换行照旧。
    """
    if not text:
        return ""
    return "<qt>" + html.escape(text, quote=False).replace("\n", "<br>") + "</qt>"


def unit_row(spin: QSpinBox, unit: str = "秒") -> QHBoxLayout:
    """数字输入框后面跟单位，单位不放进输入框里。"""
    row = QHBoxLayout()
    row.addWidget(spin)
    row.addWidget(QLabel(unit))
    row.addStretch(1)
    return row


class NoFocusDelegate(QStyledItemDelegate):
    """表格里点中某一格时，不画那个白色的焦点框。"""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus


def utc_offset_label(timestamp: float | None = None) -> str:
    """本机时区，写成 UTC+8 这样（有半小时的写成 UTC+5:30）。"""
    offset = time.localtime(timestamp).tm_gmtoff
    sign = "+" if offset >= 0 else "-"
    hours, rest = divmod(abs(offset), 3600)
    minutes = rest // 60
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def log_timestamp(timestamp: float | None = None) -> str:
    """日志里的时间：日期 + 时间 + 时区。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) + " " + utc_offset_label(timestamp)


class MessageEdit(QPlainTextEdit):
    """两行高的弹幕输入框：40 个字也能完整显示；不能换行，最多 max_length 个字。"""

    def __init__(self, max_length: int, parent=None):
        super().__init__(parent)
        self._max_length = max_length
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        margins = self.contentsMargins()
        doc_margin = int(self.document().documentMargin())
        self.setFixedHeight(self.fontMetrics().lineSpacing() * 2 + doc_margin * 2 + margins.top() + margins.bottom() + 4)
        self.textChanged.connect(self._enforce)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return  # 弹幕里不能换行
        super().keyPressEvent(event)

    def _enforce(self) -> None:
        text = self.toPlainText()
        clean = text.replace("\r", "").replace("\n", " ")[: self._max_length]
        if clean != text:  # 粘贴进来的换行换成空格，超出的字截掉
            position = min(self.textCursor().position(), len(clean))
            self.setPlainText(clean)
            cursor = self.textCursor()
            cursor.setPosition(position)
            self.setTextCursor(cursor)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:
        self.setPlainText(text)


def exact_chars(count: int) -> str:
    """准确字数，大数字加千位分隔：1234567 → 1,234,567 字。"""
    return f"{count:,} 字"


def format_chars(count: int) -> str:
    """字数写成好读的样子：12345 → 1.2 万字。"""
    if count < 10000:
        return f"{count} 字"
    return f"{count / 10000:.1f}".rstrip("0").rstrip(".") + " 万字"


def save_cancel_buttons(dialog: QDialog, save_text: str = "保存") -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText(save_text)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


# ---------------------------------------------------------------- 程序图标：对话气泡骑独轮车
# “独轮车”是B站对反复刷同一句弹幕的叫法，也是本程序主要的发送方式。
# 分三档画：48 像素以上带细节；20–32 像素去掉细节、线条加粗；托盘不要底板，暂停时变灰。

APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
TRAY_ICON_SIZES = (16, 20, 24, 32, 40, 48)  # 托盘在 100%～300% 缩放下实际用到的大小
LARGE_ICON_MIN = 48  # 从这么大开始用带细节的大图标
SPEED_PINK = "#FFD3E0"  # 大图标左边的速度线


def make_app_icon() -> QIcon:
    """窗口、任务栏、通知和 exe 用的图标（托盘图标见 make_tray_icon）。"""
    icon = QIcon()
    for size in APP_ICON_SIZES:
        icon.addPixmap(app_icon_pixmap(size))
    return icon


def app_icon_pixmap(size: int) -> QPixmap:
    """正好 size×size 像素的程序图标。打包时生成 exe 图标也用它。"""
    return _icon_pixmap(size, _draw_large_icon if size >= LARGE_ICON_MIN else _draw_small_icon)


def make_tray_icon(paused: bool = False) -> QIcon:
    """托盘图标：粉色的气泡和轮子，暂停监控时变灰。"""
    color = PAUSED_GRAY if paused else BILI_PINK
    icon = QIcon()
    for size in TRAY_ICON_SIZES:
        icon.addPixmap(_icon_pixmap(size, lambda painter: _draw_tray_icon(painter, color)))
    return icon


def _icon_pixmap(size: int, draw: Callable[[QPainter], None]) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 100, size / 100)  # 按 100×100 的格子画
    draw(painter)
    painter.end()
    return pixmap


def _draw_large_icon(painter: QPainter) -> None:
    """车轮有辐条，左边有速度线，气泡里三个点。"""
    _fill_backplate(painter)
    for x, y in ((14, 60), (9, 70), (14, 80)):
        _draw_line(painter, SPEED_PINK, 3.5, QPointF(x, y), QPointF(28, y))
    _draw_ring(painter, "white", 6, QPointF(56, 70), 18)
    for start, end in (((40, 70), (72, 70)), ((48, 56.1), (64, 83.9)), ((64, 56.1), (48, 83.9))):
        _draw_line(painter, "white", 2.2, QPointF(*start), QPointF(*end), Qt.PenCapStyle.FlatCap)
    _fill_circle(painter, "white", QPointF(56, 70), 3.5)
    _draw_line(painter, "white", 5, QPointF(56, 46), QPointF(56, 68))
    _fill_bubble(painter, "white", QRectF(33, 12, 46, 28), 10, 12, 7)
    for x in (46, 56, 66):
        _fill_circle(painter, BILI_PINK, QPointF(x, 26), 3.2)


def _draw_small_icon(painter: QPainter) -> None:
    """去掉辐条和速度线，线条加粗，缩小了也不糊。气泡里的三个点放大留着，不然气泡看着像块牌子。"""
    _fill_backplate(painter)
    _draw_ring(painter, "white", 11, QPointF(50, 71), 15)
    _draw_line(painter, "white", 9, QPointF(50, 42), QPointF(50, 58), Qt.PenCapStyle.FlatCap)
    _fill_bubble(painter, "white", QRectF(18, 8, 64, 32), 12, 16, 9)
    for x in (34, 50, 66):
        _fill_circle(painter, BILI_PINK, QPointF(x, 24), 6)


def _draw_tray_icon(painter: QPainter, color: str) -> None:
    """不要底板，和托盘里别的图标一样只有图形，深色、浅色任务栏上都看得清。"""
    _fill_bubble(painter, color, QRectF(8, 4, 84, 38), 14, 20, 10)
    for x in (32, 50, 68):
        _fill_circle(painter, "white", QPointF(x, 23), 6)
    _draw_line(painter, color, 10, QPointF(50, 50), QPointF(50, 60), Qt.PenCapStyle.FlatCap)
    _draw_ring(painter, color, 12, QPointF(50, 76), 15)


def _fill_backplate(painter: QPainter) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BILI_PINK))
    painter.drawRoundedRect(QRectF(0, 0, 100, 100), 22, 22)


def _fill_bubble(painter: QPainter, color: str, body: QRectF, radius: float, tail_width: float, tail_height: float) -> None:
    """对话气泡：圆角方块，底边中间一个朝下的尖。"""
    path = QPainterPath()
    path.addRoundedRect(body, radius, radius)
    middle, bottom = body.center().x(), body.bottom()
    tail = QPainterPath()
    tail.addPolygon(QPolygonF([
        QPointF(middle - tail_width / 2, bottom - 1), QPointF(middle, bottom + tail_height), QPointF(middle + tail_width / 2, bottom - 1)
    ]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawPath(path.united(tail))


def _fill_circle(painter: QPainter, color: str, center: QPointF, radius: float) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(center, radius, radius)


def _draw_ring(painter: QPainter, color: str, width: float, center: QPointF, radius: float) -> None:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, radius, radius)


def _draw_line(painter: QPainter, color: str, width: float, start: QPointF, end: QPointF,
               cap: Qt.PenCapStyle = Qt.PenCapStyle.RoundCap) -> None:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(cap)
    painter.setPen(pen)
    painter.drawLine(start, end)


HELP_ICON_SIZE = 16


def help_icon(tip: str) -> QLabel:
    """圆圈里一个问号，鼠标停在上面显示说明。不常看的解释文字放这里，界面不占地方。"""
    label = QLabel()
    label.setPixmap(_help_pixmap())
    label.setFixedSize(HELP_ICON_SIZE, HELP_ICON_SIZE)
    label.setToolTip(tip)
    label.setCursor(Qt.CursorShape.WhatsThisCursor)
    label.setProperty("helpIcon", True)  # 换深色 / 浅色时 recolor_widgets 按这个找到它重画
    return label


def _help_pixmap() -> QPixmap:
    size, ratio = HELP_ICON_SIZE, 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    color = QColor(StatusColors().muted)
    pen = QPen(color)
    pen.setWidthF(1.3 * ratio)
    painter.setPen(pen)
    full = QRectF(0, 0, size * ratio, size * ratio)
    painter.drawEllipse(full.adjusted(ratio, ratio, -ratio, -ratio))
    font = QFont("Microsoft YaHei UI")
    font.setBold(True)
    font.setPixelSize(10 * ratio)
    painter.setFont(font)
    painter.drawText(full, Qt.AlignmentFlag.AlignCenter, "?")
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def with_help(widget: QWidget, tip: str) -> QHBoxLayout:
    """控件后面跟一个问号（问号上显示说明）。"""
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(widget)
    row.addWidget(help_icon(tip), 0, Qt.AlignmentFlag.AlignTop if isinstance(widget, QLabel) else Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    return row


# ---------------------------------------------------------------- 左侧功能栏的图标


def _draw_rooms(painter: QPainter, color: QColor) -> None:
    """直播间：一台电视，屏幕里一个播放三角。"""
    painter.drawRoundedRect(QRectF(3, 4.5, 18, 13), 2.5, 2.5)
    painter.drawLine(QPointF(8, 20.5), QPointF(16, 20.5))
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([QPointF(10.2, 8.2), QPointF(10.2, 13.8), QPointF(15, 11)]))


def _draw_favorites(painter: QPainter, color: QColor) -> None:
    """弹幕收藏夹：一颗五角星。"""
    points = []
    for k in range(10):
        angle = math.radians(-90 + k * 36)
        radius = 9.5 if k % 2 == 0 else 4.2
        points.append(QPointF(12 + radius * math.cos(angle), 12.8 + radius * math.sin(angle)))
    painter.drawPolygon(QPolygonF(points))


def _draw_history(painter: QPainter, color: QColor) -> None:
    """历史记录：一个钟。"""
    painter.drawEllipse(QPointF(12, 12), 8.8, 8.8)
    painter.drawLine(QPointF(12, 12), QPointF(12, 7))
    painter.drawLine(QPointF(12, 12), QPointF(15.5, 14.2))


def _draw_settings(painter: QPainter, color: QColor) -> None:
    """设置：一个齿轮（8 个齿）和中间的圆孔。"""
    points = []
    for k in range(32):
        angle = math.radians(k * 360 / 32 - 360 / 64 * 3)
        radius = 9.5 if k % 4 in (1, 2) else 7.2
        points.append(QPointF(12 + radius * math.cos(angle), 12 + radius * math.sin(angle)))
    painter.drawPolygon(QPolygonF(points))
    painter.drawEllipse(QPointF(12, 12), 3, 3)


_NAV_DRAWERS = {"rooms": _draw_rooms, "favorites": _draw_favorites, "history": _draw_history, "settings": _draw_settings}


def nav_icon(kind: str) -> QIcon:
    """左侧功能栏的线条图标（自己画，不用 Emoji 和图片文件），颜色跟着深色 / 浅色模式的文字颜色。"""
    color = QGuiApplication.palette().color(QPalette.ColorRole.WindowText)
    icon = QIcon()
    for size in (16, 18, 20, 24, 32, 36, 40, 48):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 24, size / 24)  # 按 24×24 的格子画
        pen = QPen(color)
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        _NAV_DRAWERS[kind](painter, color)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def qr_pixmap(text: str, size: int = 220) -> QPixmap:
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    count = len(matrix)
    scale = max(1, size // count)
    image = QImage(count * scale, count * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()
    return QPixmap.fromImage(image)


class StatusColors:
    """状态文字的颜色，深色模式下换成更亮的一套，保证看得清。"""

    def __init__(self) -> None:
        if is_dark():
            self.ok, self.warn, self.error, self.muted = "#3FB950", "#D29922", "#F85149", "#8B949E"
            self.selection, self.hover, self.sidebar = "#264F78", "#2D2D30", "#1B1B1C"
        else:
            self.ok, self.warn, self.error, self.muted = "#1A7F37", "#9A6700", "#CF222E", "#6E7781"
            self.selection, self.hover, self.sidebar = "#CCE8FF", "#E8E8EC", "#F3F3F5"

    def values(self) -> tuple[str, ...]:
        """全部颜色，顺序固定：换深色 / 浅色时两套颜色按位置一一对应。"""
        return self.ok, self.warn, self.error, self.muted, self.selection, self.hover, self.sidebar


# ---------------------------------------------------------------- 深色 / 浅色


_THEME_SCHEMES = {THEME_LIGHT: Qt.ColorScheme.Light, THEME_DARK: Qt.ColorScheme.Dark}
_native_style = ""  # Qt 一开始选的样式：Windows 11 是 windows11，Windows 10 是 windowsvista


def is_dark() -> bool:
    """界面现在是不是深色。看实际的窗口底色，不只看 Windows 的设置。"""
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def apply_theme(theme: str) -> None:
    """外观设成跟随系统 / 白天模式 / 黑夜模式。Qt 当场换掉调色板；样式表里写死的颜色要再调 recolor_widgets。"""
    QGuiApplication.styleHints().setColorScheme(_THEME_SCHEMES.get(theme, Qt.ColorScheme.Unknown))
    fit_style()


# Windows 10 深色时 Fusion 用的调色板。Windows 自己给的那套是配 windows11 样式的，Fusion 拿来画的话
# 没勾选的勾选框是实心蓝块、表格隔行是深蓝色，所以自己配一套
_FUSION_DARK = {
    "Window": "#1E1E1E", "WindowText": "#FFFFFF", "Base": "#2D2D2D", "AlternateBase": "#262626",
    "ToolTipBase": "#2D2D2D", "ToolTipText": "#FFFFFF", "PlaceholderText": "#9D9D9D", "Text": "#FFFFFF",
    "Button": "#3C3C3C", "ButtonText": "#FFFFFF", "BrightText": "#FF5555", "Light": "#5A5A5A", "Midlight": "#4A4A4A",
    "Mid": "#3A3A3A", "Dark": "#141414", "Shadow": "#000000", "Highlight": "#0078D4", "HighlightedText": "#FFFFFF",
    "Link": "#4CC2FF", "LinkVisited": "#B58AFF", "Accent": "#0078D4",
}
_PALETTE_CLASSES = ("QCheckBox", "QRadioButton")  # Windows 给这两种控件单独配了调色板，要一起换掉


def _fusion_dark_palette() -> QPalette:
    palette = QPalette()
    for role, color in _FUSION_DARK.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#7A7A7A"))
    return palette


def fit_style() -> None:
    """Windows 10 默认的 windowsvista 样式画不出深色界面：要深色时改用 Fusion 和上面那套调色板，浅色时换回来。"""
    global _native_style
    app = QApplication.instance()
    _native_style = _native_style or app.style().name()
    if _native_style != "windowsvista":
        return
    dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    if dark == (app.style().name() == "fusion"):
        return
    app.setStyle("fusion" if dark else _native_style)
    palette = _fusion_dark_palette() if dark else QPalette()  # 空的调色板＝换回 Windows 给的
    app.setPalette(palette)
    for name in _PALETTE_CLASSES:
        app.setPalette(palette, name)


def recolor_widgets(old: StatusColors, new: StatusColors) -> None:
    """换了深色 / 浅色：各窗口样式表里写死的旧颜色（提示文字、状态文字、选中行、标签卡片等）换成新的，问号图标重画。

    表格、日志这些按颜色画出来的内容不归这里管，由各页面自己重画。
    """
    mapping = {a.upper(): b for a, b in zip(old.values(), new.values()) if a.upper() != b.upper()}
    if not mapping:
        return
    pattern = re.compile("|".join(re.escape(color) for color in mapping), re.IGNORECASE)
    for top in QApplication.topLevelWidgets():
        for widget in (top, *top.findChildren(QWidget)):
            sheet = widget.styleSheet()
            if sheet:
                recolored = pattern.sub(lambda m: mapping[m.group(0).upper()], sheet)
                if recolored != sheet:
                    widget.setStyleSheet(recolored)
            if widget.property("helpIcon"):
                widget.setPixmap(_help_pixmap())


# ---------------------------------------------------------------- 后台任务


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable, args: tuple):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.args = args
        self.signals = _TaskSignals()  # 在界面线程创建，回调因此会回到界面线程执行

    def run(self) -> None:
        try:
            result = self.fn(*self.args)
        except Exception as e:  # 界面只需要一句能看懂的错误信息
            self._emit(self.signals.failed, str(e) or e.__class__.__name__)
        else:
            self._emit(self.signals.done, result)

    def _emit(self, signal, value) -> None:
        try:
            signal.emit(value)
        except RuntimeError:
            pass  # 程序正在退出，接收结果的对象已经没了，结果直接丢掉



_running: set[_Task] = set()


def run_async(
    owner: QObject,
    fn: Callable,
    *args,
    on_done: Callable | None = None,
    on_error: Callable | None = None,
    pool: QThreadPool | None = None,
) -> None:
    """在后台线程执行 fn(*args)，完成后在界面线程回调。owner 已被销毁（比如窗口关了）就不回调。

    pool 不给就用全局线程池；一下子要排很多任务的（比如下载一大堆表情图片）用自己的线程池，免得别的任务排在后面等。
    """
    task = _Task(fn, args)
    _running.add(task)

    def finish(callback: Callable | None, value) -> None:
        _running.discard(task)
        if callback is not None and isValid(owner):
            callback(value)

    task.signals.done.connect(lambda result: finish(on_done, result))
    task.signals.failed.connect(lambda message: finish(on_error, message))
    (pool or QThreadPool.globalInstance()).start(task)
