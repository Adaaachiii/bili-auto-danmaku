"""界面公用的小工具：图标、头像、二维码图片、状态颜色、常用控件、后台任务。"""

from __future__ import annotations

import html
import math
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


def make_app_icon(paused: bool = False) -> QIcon:
    """程序图标：粉色圆角方块里一个“弹”字；暂停时变灰。"""
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PAUSED_GRAY if paused else BILI_PINK))
        radius = size * 0.22
        painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
        font = QFont("Microsoft YaHei UI")
        font.setBold(True)
        font.setPixelSize(max(8, round(size * 0.66)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "弹")
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def help_icon(tip: str) -> QLabel:
    """圆圈里一个问号，鼠标停在上面显示说明。不常看的解释文字放这里，界面不占地方。"""
    size, ratio = 16, 2
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
    label = QLabel()
    label.setPixmap(pixmap)
    label.setFixedSize(size, size)
    label.setToolTip(tip)
    label.setCursor(Qt.CursorShape.WhatsThisCursor)
    return label


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
        dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        if dark:
            self.ok, self.warn, self.error, self.muted = "#3FB950", "#D29922", "#F85149", "#8B949E"
            self.selection, self.hover, self.sidebar = "#264F78", "#2D2D30", "#1B1B1C"
        else:
            self.ok, self.warn, self.error, self.muted = "#1A7F37", "#9A6700", "#CF222E", "#6E7781"
            self.selection, self.hover, self.sidebar = "#CCE8FF", "#E8E8EC", "#F3F3F5"


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
