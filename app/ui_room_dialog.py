"""添加 / 编辑直播间：上面是直播间和模式，下面分标签页放发送内容、发送间隔与限制、其他选项。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTime, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from . import bili_api, story
from .bili_api import Credentials
from .config import (
    DANMAKU_MAX_LENGTH,
    DEFAULT_SEND_INTERVAL,
    DEFAULT_WINDOW,
    MAX_LIMIT_COUNT,
    MAX_LIMIT_MINUTES,
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
    MODE_NOTIFY,
    MODE_REPEAT,
    MODE_STORY,
    SUGGESTED_MIN_INTERVAL,
    RoomConfig,
    parse_hhmm,
)
from .favorites import FavoritesStore, count_chars
from .story import StoryPart, StoryText, count_chunks, join_parts, kept_position, queue_title, split_parts
from .ui_common import (
    MessageEdit,
    StatusColors,
    circle_avatar,
    exact_chars,
    format_duration,
    help_icon,
    hint_label,
    nav_icon,
    plain_tip,
    run_async,
    save_cancel_buttons,
    set_colored,
    unit_row,
    with_help,
)

DEFAULT_LIMIT_COUNT = 200  # 勾上“每场最多发多少条”时的默认值
DEFAULT_LIMIT_MINUTES = 60
# 系统屏蔽在任何直播间被拦过都算，主播屏蔽只算这个直播间（见 history.HistoryStore.is_filtered）
INHERIT_TIP = "本场直播结束后停止说书，下次直播开始时从中断处继续。不勾的话每场直播都从头说。"
SKIP_FILTERED_TIP = "若某一段落曾被系统屏蔽，或曾在直播间被主播屏蔽，在发送弹幕时自动跳过。"
QUEUE_HINT = "可以选择收藏夹里的多组文本加入发送序列。"
QUEUE_TIP = "按顺序说：第一组说完自动接着说第二组，以此类推。一条弹幕不会跨两组。"
QUEUE_ROWS = (2, 6)  # 发送序列列表最少、最多显示几行，再多就出滚动条
QUEUE_CURRENT_MARK = "　← 目前在这"  # 发送序列列表里标出正在说的那一组
QUEUE_CHARS_TIP = "每行首尾空白和空行不计"
REPEAT_ORDER_TIP = (
    "文字和表情按“文字→表情1→表情2→……”的顺序轮流发，拖动表情可以调整顺序；不填文字就只发表情。\n"
    "勾上“随机顺序”就每次随机挑一条，但不会连着发同一条。"
)
# 各模式的详细说明：放在模式卡片的鼠标提示里（也同步写进 mode_hint）
MODE_TIPS = {
    MODE_REPEAT: "按间隔一直发送同一句弹幕；也可以选几个表情，和文字轮流发。",
    MODE_STORY: "把一段长文字按顺序切成多条弹幕，一条接一条地发，直到全部说完。适合发小说或大段文字。",
    MODE_NOTIFY: "开播、下播时弹系统通知（不受设置里“系统通知”开关的影响），开播时可以自动打开直播页，不发弹幕。",
}

# ---- 尺寸和样式。颜色只用 palette(...)（跟着深色 / 浅色走）或 StatusColors，不写死
DIALOG_WIDTH = 660  # 窗口宽度固定，长文字换行或用省略号，不会把窗口撑宽
DIALOG_MARGIN = 16
SECTION_SPACING = 12
PAGE_MARGINS = (0, 14, 2, 4)  # 标签页里内容的边距（左、上、右、下）
PAGE_SPACING = 14
LABEL_WIDTH = 84  # 表单左边标签列的宽度：各页的控件列左边缘对齐成一条线
ROOM_AVATAR_SIZE = 40  # 编辑时名片上主播头像的大小
MODE_ICON_SIZE = 28  # 模式卡片右边图标的大小
PART_ROW_HEIGHT = 32  # 发送序列列表的行高
PART_ROLE = Qt.ItemDataRole.UserRole + 1  # 发送序列列表每一行画出来的内容：(序号, 标题, 右边的小字, 是不是正在说的那组)


def _mode_card_style() -> str:
    line = StatusColors().line
    return (
        f"QFrame#modeCard {{ border: 1px solid {line}; border-radius: 6px; }}\n"
        "QFrame#modeCard[selected=\"true\"] { border: 2px solid palette(highlight); }"
    )


def _card_style() -> str:
    return f"QFrame#card {{ background: palette(base); border: 1px solid {StatusColors().line}; border-radius: 6px; }}"


def _tabs_style() -> str:
    return f"""
QTabWidget::pane {{ border: none; border-top: 1px solid {StatusColors().line}; background: transparent; top: -1px; }}
QTabWidget::tab-bar {{ left: 0px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 6px 2px; margin-right: 22px; color: palette(window-text);
}}
QTabBar::tab:selected {{ color: palette(highlight); border-bottom: 2px solid palette(highlight); }}
QTabBar::tab:hover:!selected {{ color: palette(highlight); }}
"""


def _part_list_style() -> str:
    return (
        f"QListWidget#partList {{ border: 1px solid {StatusColors().line}; border-radius: 6px; "
        "background: palette(base); padding: 1px; }"
    )


def _time_edit() -> QTimeEdit:
    edit = QTimeEdit()
    edit.setDisplayFormat("HH:mm")
    edit.setFixedWidth(100)
    return edit


def _qtime(text: str, fallback: str) -> QTime:
    minutes = parse_hhmm(text)
    if minutes is None:
        minutes = parse_hhmm(fallback)
    return QTime(minutes // 60, minutes % 60)
from .ui_emoticons import EmoticonPickerDialog, EmoticonStrip
from .ui_favorites import FavoritePickerDialog, import_txt
from .ui_tags import TagEditor


def _lookup_room(room_input_id: int) -> tuple[bili_api.RoomInfo, bili_api.LiveStatus | None]:
    session = bili_api.new_session()
    info = bili_api.get_room_info(session, room_input_id)
    try:
        status = bili_api.get_live_status_by_uids(session, [info.uid]).get(info.uid)
    except bili_api.BiliApiError:
        status = None  # 只是拿不到昵称，不影响添加
    return info, status


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    form.setVerticalSpacing(8)
    return form


def _page_form() -> QFormLayout:
    """标签页里的表单：左边一列灰色标签（固定宽度、靠左），右边控件的左边缘对齐成一条线。"""
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(8)
    form.setVerticalSpacing(8)
    return form


def _field_label(text: str, tip: str = "") -> QWidget:
    """表单左边的标签：次要文字色，固定宽度；高度和一行输入框一样，文字和控件的第一行对齐。tip 不为空时后面带问号。"""
    label = QLabel(text)
    set_colored(label, text, StatusColors().muted)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    height = QSpinBox().sizeHint().height()
    if not tip:
        label.setFixedSize(LABEL_WIDTH, height)
        return label
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    row.addWidget(label)
    row.addWidget(help_icon(tip), 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    box.setFixedSize(LABEL_WIDTH, height)
    return box


def _section_title(text: str) -> QLabel:
    """加粗的小标题。"""
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _smaller(widget: QWidget, step: float = 1) -> QWidget:
    """字号小一点（灰色小字用）。"""
    font = widget.font()
    font.setPointSizeF(max(font.pointSizeF() - step, 7))
    widget.setFont(font)
    return widget


def _card(layout) -> QFrame:
    """淡色圆角卡片：底色 palette(base)、1px 细线色边框、圆角 6、内边距 10×12。"""
    card = QFrame()
    card.setObjectName("card")
    card.setStyleSheet(_card_style())
    layout.setContentsMargins(12, 10, 12, 10)
    card.setLayout(layout)
    return card


def _section(title: str, layout) -> QWidget:
    """加粗小标题 + 下面一块圆角卡片。"""
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(6)
    column.addWidget(_section_title(title))
    column.addWidget(_card(layout))
    return box


def _separator() -> QFrame:
    """一条 1px 的细横线（StatusColors().line，换深色 / 浅色时 recolor_widgets 会换掉）。"""
    line = QFrame()
    line.setObjectName("separator")
    line.setFixedHeight(1)
    line.setStyleSheet(f"QFrame#separator {{ background: {StatusColors().line}; border: none; }}")
    return line


def _wanted_height(widget: QWidget, width: int) -> int:
    """控件按 width 宽排好后想要多高（里面有自动换行的文字时，宽度不同高度也不同）。"""
    height = widget.heightForWidth(width) if widget.hasHeightForWidth() else -1
    return max(height if height >= 0 else widget.sizeHint().height(), widget.minimumSizeHint().height())


class _ModeCard(QFrame):
    """模式选择里的一张圆角卡片：单选按钮，下面一句灰色短说明；鼠标停在上面显示详细说明。
    点卡片里任何地方都算选中这个模式。选中的卡片边框是 2px 的强调色。"""

    def __init__(self, radio: QRadioButton, desc: str, tip: str, icon: str):
        super().__init__()
        self.radio = radio
        self.setObjectName("modeCard")
        self.setStyleSheet(_mode_card_style())
        self.setToolTip(plain_tip(tip))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)
        box = QVBoxLayout()
        box.setSpacing(2)
        outer.addLayout(box, 1)
        self.icon = QLabel()  # 右边一个线条图标：轮子 / 书 / 闹钟
        self.icon.setPixmap(nav_icon(icon).pixmap(MODE_ICON_SIZE, MODE_ICON_SIZE))
        self.icon.setFixedSize(MODE_ICON_SIZE, MODE_ICON_SIZE)
        outer.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        box.addWidget(radio)
        self.desc = _smaller(hint_label(desc))
        style = radio.style()
        indent = style.pixelMetric(QStyle.PixelMetric.PM_ExclusiveIndicatorWidth, None, radio) + style.pixelMetric(
            QStyle.PixelMetric.PM_RadioButtonLabelSpacing, None, radio
        )
        self.desc.setContentsMargins(indent, 0, 0, 0)  # 和单选按钮的文字对齐
        box.addWidget(self.desc)
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)  # 属性变了，样式表要重新套一遍才会换边框
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.radio.isEnabled():
            self.radio.setChecked(True)
            self.radio.setFocus()
        super().mousePressEvent(event)


class _PartDelegate(QStyledItemDelegate):
    """发送序列列表的一行：左边灰色序号，中间标题（太长用省略号），右边靠右的小字（字数、说到哪了）。
    正在说的那一组淡淡地高亮、文字用强调色；选中的行是实心的选中色，两者分得清。"""

    actions_width = 0  # 选中的那一行右边要留给“编辑 / 上移 / 下移 / 移除”的宽度

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), PART_ROW_HEIGHT)

    def paint(self, painter, option, index) -> None:
        info = index.data(PART_ROLE)
        if not info:
            super().paint(painter, option, index)
            return
        number, title, right, current = info
        palette = option.palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        highlight = palette.color(QPalette.ColorRole.Highlight)
        text_color = palette.color(QPalette.ColorRole.Text)
        muted = QColor(StatusColors().muted)
        right_color = muted
        painter.save()
        if selected:
            painter.fillRect(rect, highlight)
            text_color = right_color = muted = palette.color(QPalette.ColorRole.HighlightedText)
        elif current:
            tint = QColor(highlight)
            tint.setAlphaF(0.14)
            painter.fillRect(rect, tint)
            text_color = right_color = highlight
        if index.row() < index.model().rowCount() - 1 and not selected:  # 行与行之间的细线
            painter.setPen(QColor(StatusColors().line))
            painter.drawLine(rect.left() + 10, rect.bottom(), rect.right() - 10, rect.bottom())

        inner = rect.adjusted(12, 0, -12, 0)
        if selected:
            inner.setRight(inner.right() - self.actions_width)
        font = QFont(option.font)
        small = QFont(font)
        small.setPointSizeF(max(font.pointSizeF() - 1, 7))
        metrics, small_metrics = QFontMetrics(font), QFontMetrics(small)
        count = index.model().rowCount()
        number_w = metrics.horizontalAdvance("0" * len(str(count))) + 14
        right_w = min(small_metrics.horizontalAdvance(right), inner.width() // 2)
        middle = Qt.AlignmentFlag.AlignVCenter

        painter.setFont(font)
        painter.setPen(muted)
        painter.drawText(QRect(inner.left(), rect.top(), number_w, rect.height()), middle | Qt.AlignmentFlag.AlignLeft, str(number))
        painter.setFont(small)
        painter.setPen(right_color)
        painter.drawText(
            QRect(inner.right() - right_w + 1, rect.top(), right_w, rect.height()),
            middle | Qt.AlignmentFlag.AlignRight,
            small_metrics.elidedText(right, Qt.TextElideMode.ElideRight, right_w),
        )
        title_rect = QRect(inner.left() + number_w, rect.top(), inner.width() - number_w - right_w - 12, rect.height())
        if current:
            font.setBold(True)
            metrics = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(text_color)
        painter.drawText(title_rect, middle | Qt.AlignmentFlag.AlignLeft,
                         metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()))
        painter.restore()


class RoomDialog(QDialog):
    """保存后用 result_config() 取设置；new_story_text 不为 None 表示说书内容（发送序列）改了，要写进文件；
    reset_progress 表示要从头说。"""

    def __init__(
        self,
        parent: QWidget | None,
        room: RoomConfig | None,
        taken_ids: set[int],
        store: FavoritesStore,
        progress: tuple[int, int] | None = None,  # 说书进度（已说到第几个字, 总字数）
        creds: Callable[[], Credentials | None] | None = None,  # 取现在登录的账号，选表情时用
    ):
        super().__init__(parent)
        self.setWindowTitle("编辑直播间" if room else "添加直播间")
        self._colors = StatusColors()
        self._room = room
        self._taken = taken_ids
        self._store = store
        # 查询成功后记下（真实房间号, 主播 UID, 主播昵称）
        self._resolved: tuple[int, int, str] | None = (room.room_id, room.uid, room.uname) if room else None
        self._query_seq = 0
        self._avatar_url = ""  # 名片上现在显示（或正在下载）的头像地址
        self._has_avatar = False
        self.new_story_text: str | None = None
        self.reset_progress = False
        self._story_title = room.story_title if room else ""
        self._story_hash = room.story_hash if room else ""
        self._story_chars = room.story_chars if room else 0
        self._progress = progress
        self._creds = creds
        # 发送序列：打开对话框时的样子（判断改了没有、进度能不能接着用）和现在的样子
        raw = story.load_room_story(room.room_id) if room and room.story_hash else None
        self._original_text = StoryText(raw) if raw else None
        self._parts: list[StoryPart] = split_parts(raw, self._story_title) if raw else []
        self._part_chars = [StoryText(part.text).length for part in self._parts]
        self._story_cache: StoryText | None = self._original_text  # 现在的发送序列整理好的样子

        # 上面是直播间和模式，固定不动；下面的设置分成几个标签页，每页放不下时在页里面上下滚动；
        # 保存 / 取消按钮固定在最下面。窗口宽度固定，长文字换行或用省略号，不会把窗口撑宽。
        # 不用带标题的分组框，靠留白、小标题和淡色圆角卡片分组。
        self.setFixedWidth(DIALOG_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN)
        layout.setSpacing(SECTION_SPACING)
        self.room_group = self._build_room_group()
        layout.addWidget(self.room_group)
        self.mode_box = self._build_mode_group()
        layout.addWidget(self.mode_box)

        self.interval_group = self._build_interval_group()
        self.limit_group = self._build_limit_group()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("roomTabs")
        self.tabs.setStyleSheet(_tabs_style())  # 扁平的下划线标签
        self.tabs.tabBar().setDrawBase(False)
        self._page_scrolls: list[QScrollArea] = []
        self._add_tab([self.repeat_page, self.story_page], "弹幕内容")
        self._add_tab([self.interval_group, self.limit_group], "间隔与限制")
        self._add_tab([self._build_other_box()], "其他")
        layout.addWidget(self.tabs, 1)

        layout.addWidget(_separator())
        self.buttons = save_cancel_buttons(self)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setAutoDefault(False)  # 输入框里按回车不要直接保存
        save.setObjectName("primaryButton")
        layout.addWidget(self.buttons)

        self._load(room)
        self._update_story_labels()
        self._update_save_button()
        if room:
            self._query()  # 顺便显示一下当前状态

    def _add_tab(self, widgets: list[QWidget], title: str) -> None:
        """一个标签页：里面的东西从上往下排，放不下时页里面出现竖向滚动条（不会出现横向滚动条）。"""
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(*PAGE_MARGINS)
        box.setSpacing(PAGE_SPACING)
        for widget in widgets:
            box.addWidget(widget)
        box.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 不自己填底色，露出窗口的底色（深色 / 浅色都跟着调色板走）
        scroll.setObjectName("pageScroll")
        scroll.setStyleSheet("QScrollArea#pageScroll { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        inner.setAutoFillBackground(False)
        self._page_scrolls.append(scroll)
        self.tabs.addTab(scroll, title)

    # ------------------------------------------------------------ 窗口大小

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_to_content()

    def _schedule_fit(self) -> None:
        """内容变了（查询结果多了一行、发送序列多了几组等）：等布局算好后重新定窗口大小。"""
        if self.isVisible():
            QTimer.singleShot(0, self._fit_to_content)

    def _page_heights(self, width: int) -> list[int]:
        """各标签页里的内容按 width 宽排好后要多高（不含页边距）。第一页按独轮车、说书人两种里高的那种算，
        这样切换模式、切换标签页时窗口高度都不变。"""
        return [
            max(_wanted_height(self.repeat_page, width), _wanted_height(self.story_page, width)),
            _wanted_height(self.interval_group, width) + PAGE_SPACING + _wanted_height(self.limit_group, width),
            _wanted_height(self.other_box, width),
        ]

    def _fit_to_content(self, screen: QRect | None = None) -> None:
        """宽度固定；高度＝最高的那一页放得下的高度，但不超过屏幕可用高度，放不下时在标签页里面上下滚动。

        screen 是屏幕上能用的区域（不含任务栏），默认是窗口所在的屏幕；测试时可以传一个小的进来。
        """
        screen = screen or (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        width = min(DIALOG_WIDTH, screen.width() - 16)
        self.setFixedWidth(width)
        layout = self.layout()
        layout.activate()  # 先按这个宽度排一遍，下面才量得出各部分的实际宽度
        margins = layout.contentsMargins()
        top_w = width - margins.left() - margins.right()
        scroll = self.tabs.currentWidget()
        tab_chrome = self.tabs.height() - scroll.height()  # 标签栏和下面的细线
        if scroll.width() < 100 or tab_chrome <= 0:  # 还没排过（一般不会），按估计的算
            page_w, tab_chrome = top_w, self.tabs.tabBar().sizeHint().height() + 2
        else:
            page_w = scroll.width()
        # 按出现竖向滚动条时的宽度算：文字换行可能多一行，宁可高一点
        inner_w = page_w - scroll.verticalScrollBar().sizeHint().width() - PAGE_MARGINS[0] - PAGE_MARGINS[2]
        pages = max(self._page_heights(inner_w)) + PAGE_MARGINS[1] + PAGE_MARGINS[3]
        need_h, shown = margins.top() + margins.bottom(), 0
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if widget is None or widget.isHidden():
                continue
            shown += 1
            need_h += tab_chrome + pages if widget is self.tabs else _wanted_height(widget, top_w)
        need_h += layout.spacing() * max(0, shown - 1) + 4  # 留几像素余量：差一两像素也会出滚动条
        title_bar = max(0, self.frameGeometry().height() - self.height()) or 32
        max_h = screen.height() - title_bar - 16
        self.resize(width, min(need_h, max_h))
        # 变高以后底部跑到屏幕外面的话，往上挪
        frame = self.frameGeometry()
        if frame.bottom() > screen.bottom() or frame.top() < screen.top():
            self.move(frame.left(), max(screen.top(), screen.bottom() - frame.height()))

    # ------------------------------------------------------------ 搭界面

    def _build_room_group(self) -> QWidget:
        """添加时：房间号输入框 + “查询”；编辑时：主播名片（昵称、状态、房间号）+ “刷新”。下面一行是查询结果 / 出错提示。"""
        self.room_edit = QLineEdit()
        self.room_edit.setPlaceholderText("房间号或直播间链接，例如 6 或 https://live.bilibili.com/6")
        self.query_btn = QPushButton("查询")
        self.query_btn.setAutoDefault(False)
        self.room_info = hint_label("输入后点“查询”，确认是哪位主播")

        self.query_btn.clicked.connect(self._query)
        self.room_edit.returnPressed.connect(self._query)
        self.room_edit.textEdited.connect(self._on_room_edited)

        group = QWidget()
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        if self._room is None:
            row.addWidget(self.room_edit, 1)
            row.addWidget(self.query_btn)
        else:
            # 编辑已有直播间：房间号不能改，输入框藏起来（但还在，保存着房间号），换成名片
            self.room_edit.setParent(group)
            self.room_edit.hide()
            self.room_avatar = QLabel()  # 圆形头像：主播昵称的第一个字
            self.room_avatar.setObjectName("roomAvatar")
            self.room_avatar.setTextFormat(Qt.TextFormat.PlainText)
            self.room_avatar.setFixedSize(ROOM_AVATAR_SIZE, ROOM_AVATAR_SIZE)
            self.room_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.room_avatar.setStyleSheet(
                "QLabel#roomAvatar { background: palette(highlight); color: palette(highlighted-text); "
                "border-radius: 20px; font-size: 13pt; font-weight: bold; }"
            )
            row.addWidget(self.room_avatar, 0, Qt.AlignmentFlag.AlignVCenter)
            self.room_name = QLabel()
            self.room_name.setTextFormat(Qt.TextFormat.PlainText)  # 昵称来自 B 站
            font = self.room_name.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() + 3)
            self.room_name.setFont(font)
            self.room_state = QLabel()
            self.room_state.setTextFormat(Qt.TextFormat.PlainText)
            name_row = QHBoxLayout()
            name_row.setSpacing(10)
            name_row.addWidget(self.room_name)
            name_row.addWidget(self.room_state, 0, Qt.AlignmentFlag.AlignVCenter)
            name_row.addStretch(1)
            self.room_sub = _smaller(hint_label())
            left = QVBoxLayout()
            left.setSpacing(2)
            left.addLayout(name_row)
            left.addWidget(self.room_sub)
            row.addLayout(left, 1)
            row.addWidget(self.query_btn, 0, Qt.AlignmentFlag.AlignVCenter)
            self._update_room_card()
        box.addLayout(row)
        box.addWidget(self.room_info)
        return group

    def _update_room_card(self, state: str = "", color: str = "", title: str = "") -> None:
        """编辑时的主播名片：昵称（加粗）、状态（直播中是绿色）、下面一行小字房间号和直播标题。"""
        if self._room is None:
            return
        room_id, _uid, uname = self._resolved or (self._room.room_id, self._room.uid, self._room.uname)
        self.room_name.setText(uname or "（没取到昵称）")
        if not self._has_avatar:  # 头像下好以后就一直显示头像
            self.room_avatar.setText((uname or "").strip()[:1] or "？")
        set_colored(self.room_state, state, color or self._colors.muted)
        sub = f"房间号 {room_id}"
        if title:
            sub += f"　·　{title}"
        self.room_sub.setText(sub)

    def _load_room_avatar(self, url: str) -> None:
        """编辑时名片上的头像：后台下载主播的头像，下好后换上；没有地址或下载失败就继续显示昵称的第一个字。"""
        if self._room is None or not url or url == self._avatar_url:
            return
        self._avatar_url = url

        def on_done(data: bytes) -> None:
            pixmap = QPixmap()
            if url == self._avatar_url and pixmap.loadFromData(data):
                self.room_avatar.setStyleSheet("")
                self.room_avatar.setText("")
                self.room_avatar.setPixmap(circle_avatar(pixmap, ROOM_AVATAR_SIZE))
                self._has_avatar = True

        def on_error(_message: str) -> None:
            self._avatar_url = ""  # 下次点“刷新”再试

        run_async(self, bili_api.fetch_avatar, url, on_done=on_done, on_error=on_error)

    def _build_mode_group(self) -> QWidget:
        self.repeat_radio = QRadioButton("独轮车模式")
        self.story_radio = QRadioButton("说书人模式")
        self.notify_radio = QRadioButton("只提醒")
        self.mode_group = QButtonGroup(self)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(10)
        self._mode_cards: list[_ModeCard] = []
        for radio, mode, desc in (
            (self.repeat_radio, MODE_REPEAT, "循环发同一句话"),
            (self.story_radio, MODE_STORY, "把长文字按顺序发完"),
            (self.notify_radio, MODE_NOTIFY, "开播提醒，不发弹幕"),
        ):
            icon = {MODE_REPEAT: "wheel", MODE_STORY: "book", MODE_NOTIFY: "alarm"}[mode]
            self.mode_group.addButton(radio)
            card = _ModeCard(radio, desc, MODE_TIPS[mode], icon)
            self._mode_cards.append(card)
            mode_row.addWidget(card, 1)
        # 选中的那种模式的详细说明：界面上不单独占一行（已经放在各卡片的鼠标提示里），文字照样跟着模式更新
        self.mode_hint = hint_label()
        self.mode_hint.hide()

        self.repeat_page = self._build_repeat_page()
        self.story_page = self._build_story_page()
        self.mode_group.buttonToggled.connect(lambda _button, checked: checked and self._on_mode_changed())

        group = QWidget()
        group.setLayout(mode_row)
        self.mode_hint.setParent(group)
        return group

    def _build_repeat_page(self) -> QWidget:
        self.msg_edit = MessageEdit(DANMAKU_MAX_LENGTH)
        self.msg_edit.setPlaceholderText("要循环发送的那句话（只发表情的话可以不填）")
        self.msg_count = _smaller(hint_label())
        pick_btn = QPushButton("从收藏夹选择")
        pick_btn.setAutoDefault(False)
        pick_btn.clicked.connect(self._pick_message)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.msg_count, 1)
        pick_row.addWidget(pick_btn)
        self.msg_edit.textChanged.connect(self._update_count)
        message_box = QVBoxLayout()
        message_box.setSpacing(6)
        message_box.addWidget(self.msg_edit)
        message_box.addLayout(pick_row)

        page = QWidget()
        form = _page_form()
        form.addRow(_field_label("弹幕内容"), message_box)
        form.addRow(_field_label("表情"), self._build_emoticon_box())
        page.setLayout(form)
        return page

    def _build_emoticon_box(self) -> QVBoxLayout:
        self.emoticon_empty = hint_label("没有选表情，只发文字")
        self.emoticon_empty.setMinimumHeight(QSpinBox().sizeHint().height())  # 和左边的“表情”对齐
        self.emoticon_strip = EmoticonStrip()
        self.emoticon_strip.setFixedHeight(84)
        self.emoticon_strip.changed.connect(self._on_emoticons_changed)
        self.emoticon_btn = QPushButton("选择表情…")
        self.emoticon_clear_btn = QPushButton("清空")
        for button in (self.emoticon_btn, self.emoticon_clear_btn):
            button.setAutoDefault(False)
        self.emoticon_btn.clicked.connect(self._pick_emoticons)
        self.emoticon_clear_btn.clicked.connect(lambda: self.emoticon_strip.set_emoticons([]))
        self.repeat_random_check = QCheckBox("随机顺序")
        row = QHBoxLayout()
        row.addWidget(self.emoticon_btn)
        row.addWidget(self.emoticon_clear_btn)
        row.addSpacing(16)
        row.addLayout(with_help(self.repeat_random_check, REPEAT_ORDER_TIP))
        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(self.emoticon_empty)
        box.addWidget(self.emoticon_strip)
        box.addLayout(row)
        return box

    def _build_story_page(self) -> QWidget:
        # 第一行：左边“发送序列”加粗，后面灰色小字汇总；右边是往序列里加内容的三个按钮
        self.story_label = _smaller(QLabel())
        self.story_label.setTextFormat(Qt.TextFormat.PlainText)
        self.story_label.setWordWrap(True)
        pick_btn = QPushButton("从收藏夹选择")
        import_btn = QPushButton("导入 txt")
        paste_btn = QPushButton("粘贴文字")
        self.story_add_buttons = (pick_btn, import_btn, paste_btn)
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_row.addWidget(_section_title("发送序列"))
        source_row.addWidget(self.story_label, 1)
        for button, handler in ((pick_btn, self._pick_story), (import_btn, self._import_story), (paste_btn, self._paste_story)):
            button.setAutoDefault(False)
            button.clicked.connect(handler)
            source_row.addWidget(button)

        # 发送序列：一组一行，按顺序说；列表正下方一排按钮管选中的那一组，最右边是“重置”
        self.part_list = QListWidget()
        self.part_list.setObjectName("partList")
        self.part_list.setStyleSheet(_part_list_style())
        self.part_list.setItemDelegate(_PartDelegate(self.part_list))
        self.part_list.setUniformItemSizes(True)
        self.part_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.part_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.part_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.part_list.itemSelectionChanged.connect(self._update_part_buttons)
        self.part_list.itemDoubleClicked.connect(lambda _item: self._edit_part())
        # 选中哪一行，“编辑 / 上移 / 下移 / 移除”就浮在那一行的右边（见 _place_part_actions）
        self.part_edit_btn = QPushButton("编辑")
        self.part_up_btn = QPushButton("上移")
        self.part_down_btn = QPushButton("下移")
        self.part_remove_btn = QPushButton("移除")
        self.part_actions = QWidget(self.part_list.viewport())
        actions = QHBoxLayout(self.part_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        for button, handler in (
            (self.part_edit_btn, self._edit_part),
            (self.part_up_btn, lambda: self._move_part(-1)),
            (self.part_down_btn, lambda: self._move_part(1)),
            (self.part_remove_btn, self._remove_part),
        ):
            _smaller(button)
            button.setObjectName("smallButton")
            button.setAutoDefault(False)
            button.setFixedSize(button.fontMetrics().horizontalAdvance(button.text()) + 20, PART_ROW_HEIGHT - 8)
            button.clicked.connect(handler)
            actions.addWidget(button)
        self.part_actions.adjustSize()
        self.part_actions.hide()
        self.part_list.itemDelegate().actions_width = self.part_actions.width() + 10
        self.part_list.verticalScrollBar().valueChanged.connect(self._place_part_actions)
        self.part_list.viewport().installEventFilter(self)
        queue_layout = QVBoxLayout()
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.addWidget(self.part_list)
        self.queue_box = QWidget()
        self.queue_box.setLayout(queue_layout)

        # 列表下面一行：左边提示，右边说到哪了和“重置”
        queue_hint = _smaller(hint_label(QUEUE_HINT))
        queue_hint.setWordWrap(False)  # 一句短话，不用自动换行（自动换行会多留出一行空白）
        self.progress_short = _smaller(hint_label())
        self.progress_short.setWordWrap(False)
        # 说书进度的详细文字（说到第几个字、剩多少、在第几组）：鼠标停在右边的进度上看
        self.progress_label = QLabel(self)
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_label.hide()
        self.reset_btn = QPushButton("重置（从头开始说书）")
        self.reset_btn.setAutoDefault(False)
        self.reset_btn.clicked.connect(self._reset_progress)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(10)
        hint_row.addLayout(with_help(queue_hint, QUEUE_TIP), 1)
        hint_row.addWidget(self.progress_short)
        hint_row.addWidget(self.reset_btn)

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1, DANMAKU_MAX_LENGTH)
        self.chunk_spin.setFixedWidth(100)

        self.ignore_lines_radio = QRadioButton("忽略换行")
        self.by_line_radio = QRadioButton("按行说书")
        line_group = QButtonGroup(self)
        line_group.addButton(self.ignore_lines_radio)
        line_group.addButton(self.by_line_radio)
        self.line_hint = _smaller(hint_label())
        self.line_hint.setWordWrap(False)  # 一句短话，不用自动换行（自动换行会多留出一行空白）
        self.by_line_radio.toggled.connect(self._update_line_hint)
        line_box = QHBoxLayout()
        line_box.setSpacing(16)
        line_box.addWidget(self.ignore_lines_radio)
        line_box.addWidget(self.by_line_radio)
        line_box.addWidget(self.line_hint, 1)

        # 预计：跟在“每条字数”后面，灰色小字，一般两行；放不下时换行，不撑宽窗口
        self.estimate_label = _smaller(hint_label())
        self.chunk_spin.valueChanged.connect(self._update_estimate)
        self.by_line_radio.toggled.connect(self._update_estimate)
        chunk_row = QHBoxLayout()
        chunk_row.setSpacing(6)
        chunk_row.addWidget(self.chunk_spin)
        chunk_row.addWidget(QLabel("字"))
        chunk_row.addSpacing(12)
        chunk_row.addWidget(self.estimate_label, 1)
        chunk_row.addWidget(help_icon("预计：不算被限流、被屏蔽和不在发送时段的时间"), 0, Qt.AlignmentFlag.AlignVCenter)

        self.finish_combo = QComboBox()
        self.finish_combo.addItem("停止发送", False)
        self.finish_combo.addItem("从头循环", True)
        self.finish_combo.setFixedWidth(120)
        self.inherit_check = QCheckBox("说书状态继承")
        self.skip_filtered_check = QCheckBox("跳过以前被屏蔽过的段落")
        finish_row = QHBoxLayout()
        finish_row.setSpacing(6)
        finish_row.addWidget(self.finish_combo)
        finish_row.addSpacing(12)
        finish_row.addWidget(self.inherit_check)
        finish_row.addWidget(help_icon(INHERIT_TIP), 0, Qt.AlignmentFlag.AlignVCenter)
        finish_row.addSpacing(12)
        finish_row.addWidget(self.skip_filtered_check)
        finish_row.addWidget(help_icon(SKIP_FILTERED_TIP), 0, Qt.AlignmentFlag.AlignVCenter)
        finish_row.addStretch(1)

        form = _page_form()
        form.addRow(_field_label("每条字数"), chunk_row)
        form.addRow(_field_label("换行处理"), line_box)
        form.addRow(_field_label("说书结束后"), finish_row)

        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        box.addLayout(source_row)
        box.addWidget(self.queue_box)
        box.addLayout(hint_row)
        box.addSpacing(2)
        box.addWidget(_separator())
        box.addSpacing(2)
        box.addLayout(form)
        return page

    def _build_interval_group(self) -> QWidget:
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)
        self.interval_spin.setFixedWidth(100)
        self.random_check = QCheckBox("随机间隔")
        self.max_label = QLabel("至")
        self.max_spin = QSpinBox()
        self.max_spin.setRange(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)
        self.max_spin.setFixedWidth(100)
        self.max_unit = QLabel("秒")
        row = QHBoxLayout()
        row.addWidget(self.interval_spin)
        row.addWidget(QLabel("秒"))
        row.addSpacing(16)
        row.addWidget(self.random_check)
        row.addSpacing(8)
        row.addWidget(self.max_label)
        row.addWidget(self.max_spin)
        row.addWidget(self.max_unit)
        row.addStretch(1)
        self.interval_hint = _smaller(hint_label())

        self.random_check.toggled.connect(self._update_interval_widgets)
        self.interval_spin.valueChanged.connect(self._update_interval_widgets)
        self.max_spin.valueChanged.connect(self._update_estimate)

        box = QVBoxLayout()
        box.setSpacing(6)
        box.addLayout(row)
        box.addWidget(self.interval_hint)
        return _section("发送间隔", box)

    def _build_limit_group(self) -> QWidget:
        self.count_check = QCheckBox("每场最多发")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, MAX_LIMIT_COUNT)
        self.count_spin.setFixedWidth(100)
        self.minutes_check = QCheckBox("每场最多发")
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(1, MAX_LIMIT_MINUTES)
        self.minutes_spin.setFixedWidth(100)
        self.window_check = QCheckBox("只在每天")
        self.window_start_edit = _time_edit()
        self.window_end_edit = _time_edit()

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        rows = [
            (self.count_check, [self.count_spin, QLabel("条（按发送成功的条数算）")]),
            (self.minutes_check, [self.minutes_spin, QLabel("分钟（从这一场发出第一条算起）")]),
            (self.window_check, [self.window_start_edit, QLabel("到"), self.window_end_edit, QLabel("之间发送")]),
        ]
        for row, (check, widgets) in enumerate(rows):
            grid.addWidget(check, row, 0)
            line = QHBoxLayout()
            for widget in widgets:
                line.addWidget(widget)
            line.addStretch(1)
            if row == 0:
                line.addWidget(help_icon("可同时设置多条规则，触发某一规则上限后即停止发送。"))
            grid.addLayout(line, row, 1)
            check.toggled.connect(self._update_limit_widgets)
        grid.setColumnStretch(1, 1)
        self.window_check.setToolTip("结束时间早于开始时间表示跨过半夜，比如 22:00 到 02:00")
        return _section("发送限制", grid)

    def _build_other_box(self) -> QWidget:
        self.open_check = QCheckBox("开播时自动打开直播页")
        self.offline_check = QCheckBox("未开播时也发送弹幕")
        self.enabled_check = QCheckBox("启用直播间状态监控")
        box = QVBoxLayout()
        box.setSpacing(10)
        for check in (self.open_check, self.offline_check, self.enabled_check):
            box.addWidget(check)
        self.other_box = _card(box)
        return self.other_box

    def _load(self, room: RoomConfig | None) -> None:
        if room:
            self.room_edit.setText(str(room.room_id))
            self.room_edit.setReadOnly(True)
            self.query_btn.setText("刷新")
        cfg = room or RoomConfig(room_id=0, interval_sec=DEFAULT_SEND_INTERVAL)
        {MODE_STORY: self.story_radio, MODE_NOTIFY: self.notify_radio}.get(cfg.mode, self.repeat_radio).setChecked(True)
        self._on_mode_changed()
        self.msg_edit.setText(cfg.message)
        self._update_count()
        self.emoticon_strip.set_emoticons(cfg.emoticons)
        self.repeat_random_check.setChecked(cfg.repeat_random)
        self.chunk_spin.setValue(cfg.story_chunk_size)
        (self.by_line_radio if cfg.story_by_line else self.ignore_lines_radio).setChecked(True)
        self._update_line_hint()
        self.finish_combo.setCurrentIndex(1 if cfg.story_loop else 0)
        self.inherit_check.setChecked(cfg.story_inherit)
        self.skip_filtered_check.setChecked(cfg.story_skip_filtered)
        self.interval_spin.setValue(cfg.interval_sec)
        self.max_spin.setValue(max(cfg.interval_max_sec, cfg.interval_sec))
        self.random_check.setChecked(cfg.interval_random)
        self._update_interval_widgets()
        self.count_check.setChecked(cfg.limit_count > 0)
        self.count_spin.setValue(cfg.limit_count or DEFAULT_LIMIT_COUNT)
        self.minutes_check.setChecked(cfg.limit_minutes > 0)
        self.minutes_spin.setValue(cfg.limit_minutes or DEFAULT_LIMIT_MINUTES)
        self.window_check.setChecked(cfg.window_enabled)
        self.window_start_edit.setTime(_qtime(cfg.window_start, DEFAULT_WINDOW[0]))
        self.window_end_edit.setTime(_qtime(cfg.window_end, DEFAULT_WINDOW[1]))
        self._update_limit_widgets()
        self.open_check.setChecked(cfg.open_browser)
        self.offline_check.setChecked(cfg.send_when_offline)
        self.enabled_check.setChecked(cfg.enabled)

    # ------------------------------------------------------------ 结果

    def result_config(self) -> RoomConfig:
        assert self._resolved is not None
        room_id, uid, uname = self._resolved
        return RoomConfig(
            room_id=room_id,
            uid=uid,
            uname=uname,
            mode=self._mode(),
            message=self.msg_edit.text().strip(),
            emoticons=self.emoticon_strip.emoticons(),
            repeat_random=self.repeat_random_check.isChecked(),
            interval_sec=self.interval_spin.value(),
            interval_random=self.random_check.isChecked(),
            interval_max_sec=max(self.max_spin.value(), self.interval_spin.value()),
            open_browser=self.open_check.isChecked(),
            send_when_offline=self.offline_check.isChecked(),
            enabled=self.enabled_check.isChecked(),
            story_title=self._story_title,
            story_hash=self._story_hash,
            story_chars=self._story_chars,
            story_chunk_size=self.chunk_spin.value(),
            story_by_line=self.by_line_radio.isChecked(),
            story_inherit=self.inherit_check.isChecked(),
            story_loop=bool(self.finish_combo.currentData()),
            story_skip_filtered=self.skip_filtered_check.isChecked(),
            limit_count=self.count_spin.value() if self.count_check.isChecked() else 0,
            limit_minutes=self.minutes_spin.value() if self.minutes_check.isChecked() else 0,
            window_enabled=self.window_check.isChecked(),
            window_start=self.window_start_edit.time().toString("HH:mm"),
            window_end=self.window_end_edit.time().toString("HH:mm"),
        )

    def _mode(self) -> str:
        if self.story_radio.isChecked():
            return MODE_STORY
        if self.notify_radio.isChecked():
            return MODE_NOTIFY
        return MODE_REPEAT

    # ------------------------------------------------------------ 直播间查询

    def _on_room_edited(self) -> None:
        self._resolved = None
        self._set_room_info("输入后点“查询”，确认是哪位主播", self._colors.muted)
        self._update_save_button()

    def _query(self) -> None:
        room_id = bili_api.parse_room_input(self.room_edit.text())
        if room_id is None:
            self._set_room_info("请输入房间号，或者粘贴直播间链接", self._colors.error)
            return
        self._query_seq += 1
        seq = self._query_seq
        self.query_btn.setEnabled(False)
        self._set_room_info("正在查询…", self._colors.muted)
        if self._room is not None:
            set_colored(self.room_state, "正在查询…", self._colors.muted)
        run_async(
            self,
            _lookup_room,
            room_id,
            on_done=lambda result: self._on_lookup(seq, result),
            on_error=lambda message: self._on_lookup_failed(seq, message),
        )

    def _on_lookup(self, seq: int, result) -> None:
        if seq != self._query_seq:
            return
        self.query_btn.setEnabled(True)
        info, status = result
        if self._room is None and info.room_id in self._taken:
            self._resolved = None
            self._set_room_info("这个直播间已经在列表里了", self._colors.error)
            self._update_save_button()
            return
        old_uname = self._resolved[2] if self._resolved else ""
        uname = status.uname if status and status.uname else old_uname
        self._resolved = (info.room_id, info.uid, uname)
        state = bili_api.LIVE_STATUS_TEXT.get(info.live_status, "未知")
        text = f"主播：{uname or '（没取到昵称）'}　　真实房间号：{info.room_id}　　当前：{state}"
        if status and status.title:
            text += f"\n直播标题：{status.title}"
        self._set_room_info(text, self._colors.ok)
        live = info.live_status == bili_api.LIVE_ON
        self._update_room_card(state, self._colors.ok if live else self._colors.muted, status.title if status else "")
        self._load_room_avatar(status.face if status else "")
        self._update_save_button()
        self._schedule_fit()

    def _on_lookup_failed(self, seq: int, message: str) -> None:
        if seq != self._query_seq:
            return
        self.query_btn.setEnabled(True)
        self._set_room_info(message, self._colors.error)
        if self._room is not None:
            set_colored(self.room_state, "", self._colors.muted)

    def _set_room_info(self, text: str, color: str) -> None:
        """查询结果那一行。编辑已有直播间时名片上已经有昵称和状态，这一行只在出错时显示（红字）。"""
        set_colored(self.room_info, text, color)
        if self._room is not None:
            self.room_info.setVisible(color == self._colors.error)
            self._schedule_fit()

    # ------------------------------------------------------------ 模式

    def _on_mode_changed(self) -> None:
        mode = self._mode()
        sends = mode != MODE_NOTIFY
        self.repeat_page.setVisible(mode == MODE_REPEAT)
        self.story_page.setVisible(mode == MODE_STORY)
        self.interval_group.setVisible(sends)
        self.limit_group.setVisible(sends)
        self.offline_check.setVisible(sends)
        # 只提醒不发弹幕：只剩“其他”一页；发弹幕的模式回到第一页（发送内容）
        self.tabs.setTabText(0, "说书内容" if mode == MODE_STORY else "弹幕内容")
        self.tabs.setTabVisible(0, sends)
        self.tabs.setTabVisible(1, sends)
        self.tabs.setCurrentIndex(0 if sends else 2)
        self.mode_hint.setText(MODE_TIPS[mode])
        for card in self._mode_cards:
            card.set_selected(card.radio.isChecked())
        self._update_save_button()
        self._schedule_fit()

    def _update_count(self) -> None:
        self.msg_count.setText(f"{len(self.msg_edit.text().strip())} / {DANMAKU_MAX_LENGTH} 字")
        self._update_save_button()

    def _pick_message(self) -> None:
        dialog = FavoritePickerDialog(self, self._store, max_chars=DANMAKU_MAX_LENGTH)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_favorite:
            # 弹幕里不能换行，换行换成空格
            self.msg_edit.setText(" ".join(dialog.result_favorite.content.split()))

    def _pick_emoticons(self) -> None:
        if self._resolved is None:
            QMessageBox.information(self, "选择表情", "请先输入直播间并点“查询”。有的表情只能在某个直播间用，所以要先确定是哪个直播间。")
            return
        creds = self._creds() if self._creds else None
        if creds is None:
            QMessageBox.information(self, "选择表情", "请先登录 B 站账号。B 站只告诉登录了的账号它能用哪些表情（包括买过的装扮表情）。")
            return
        dialog = EmoticonPickerDialog(self, self._resolved[0], creds, self.emoticon_strip.emoticons())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.emoticon_strip.set_emoticons(dialog.result_emoticons)

    def _on_emoticons_changed(self) -> None:
        count = self.emoticon_strip.count()
        self.emoticon_empty.setVisible(count == 0)
        self.emoticon_strip.setVisible(count > 0)
        self.emoticon_clear_btn.setEnabled(count > 0)
        self.repeat_random_check.setEnabled(count > 0)  # 只有一句文字时谈不上顺序
        self._update_save_button()
        self._schedule_fit()

    # ------------------------------------------------------------ 说书内容

    def _set_parts(self, parts: list[StoryPart], select: int | None = None) -> None:
        """发送序列改了：重新整理文字，更新标题、字数、进度和预计。select 是改完以后选中第几组。"""
        self._parts = parts
        self._part_chars = [StoryText(part.text).length for part in parts]
        raw = join_parts(parts) if parts else ""
        self._story_cache = StoryText(raw) if parts else None
        self._story_hash = self._story_cache.hash if self._story_cache else ""
        self._story_chars = self._story_cache.length if self._story_cache else 0
        self._story_title = queue_title(parts)
        original = self._room.story_hash if self._room else ""
        self.new_story_text = raw if parts and self._story_hash != original else None
        self._update_story_labels(select)
        self._update_save_button()

    def _part_from(self, text: str, title: str) -> StoryPart | None:
        """一组新的文字；里面没有能发的内容时提示一下，返回 None。"""
        if StoryText(text).length == 0:
            QMessageBox.warning(self, "没有内容", f"《{title}》里没有可以发送的内容" if title else "这段文字里没有可以发送的内容")
            return None
        return StoryPart(title or " ".join(text.split())[:20], text)

    def _add_parts(self, new_parts: list[StoryPart]) -> None:
        if new_parts:
            self._set_parts(self._parts + new_parts, select=len(self._parts) + len(new_parts) - 1)

    def _pick_story(self) -> None:
        dialog = FavoritePickerDialog(self, self._store, multi=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            parts = [self._part_from(favorite.content, favorite.title) for favorite in dialog.result_favorites]
            self._add_parts([part for part in parts if part is not None])

    def _import_story(self) -> None:
        result = import_txt(self)
        if result is not None:
            name, text = result
            part = self._part_from(text, name)
            self._add_parts([part] if part else [])

    def _paste_story(self) -> None:
        dialog = StoryTextDialog(self, self._store, "", "")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            part = self._part_from(dialog.result_text, dialog.result_title)
            self._add_parts([part] if part else [])

    def _selected_part(self) -> int | None:
        row = self.part_list.currentRow()
        return row if self.part_list.selectedItems() and 0 <= row < len(self._parts) else None

    def _edit_part(self) -> None:
        index = self._selected_part()
        if index is None:
            return
        old = self._parts[index]
        dialog = StoryTextDialog(self, self._store, old.title, old.text)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            part = self._part_from(dialog.result_text, dialog.result_title)
            if part is not None:
                self._set_parts(self._parts[:index] + [part] + self._parts[index + 1 :], select=index)

    def _move_part(self, step: int) -> None:
        index = self._selected_part()
        if index is None or not 0 <= index + step < len(self._parts):
            return
        parts = list(self._parts)
        parts[index], parts[index + step] = parts[index + step], parts[index]
        self._set_parts(parts, select=index + step)

    def _remove_part(self) -> None:
        index = self._selected_part()
        if index is None:
            return
        parts = self._parts[:index] + self._parts[index + 1 :]
        self._set_parts(parts, select=min(index, len(parts) - 1) if parts else None)

    def _update_part_buttons(self) -> None:
        index = self._selected_part()
        self.part_edit_btn.setEnabled(index is not None)
        self.part_remove_btn.setEnabled(index is not None)
        self.part_up_btn.setEnabled(index is not None and index > 0)
        self.part_down_btn.setEnabled(index is not None and index < len(self._parts) - 1)
        self._place_part_actions()

    def _place_part_actions(self) -> None:
        """“编辑 / 上移 / 下移 / 移除”放在选中那一行的右边；没有选中、或那一行滚出去了就藏起来。"""
        index = self._selected_part()
        item = self.part_list.item(index) if index is not None else None
        rect = self.part_list.visualItemRect(item) if item else QRect()
        viewport = self.part_list.viewport()
        if item is None or rect.top() < 0 or rect.bottom() > viewport.height():
            self.part_actions.hide()
            return
        bar = self.part_actions
        bar.move(viewport.width() - bar.width() - 8, rect.top() + (rect.height() - bar.height()) // 2)
        bar.show()
        bar.raise_()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.part_list.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._place_part_actions)
        return super().eventFilter(watched, event)

    def _fill_part_list(self, select: int | None) -> None:
        """发送序列列表：序号、标题、字数；正在说的那一组标出来。"""
        resume = self._resume_pos()
        current = self._story_cache.part_at(resume) if self._story_cache and resume else None
        marks = self._part_marks()
        self.part_list.clear()
        for index, (part, chars) in enumerate(zip(self._parts, self._part_chars)):
            text = f"{index + 1}. 《{part.title}》　{exact_chars(chars)}"
            if index == current:
                text += QUEUE_CURRENT_MARK
            item = QListWidgetItem(text)
            item.setToolTip(plain_tip(f"《{part.title}》"))
            item.setData(PART_ROLE, (index + 1, part.title, marks[index], index == current))  # 列表实际画出来的样子
            self.part_list.addItem(item)
        if select is not None and 0 <= select < len(self._parts):
            self.part_list.setCurrentRow(select)
        rows = min(max(len(self._parts), QUEUE_ROWS[0]), QUEUE_ROWS[1])
        row_height = self.part_list.sizeHintForRow(0) if self._parts else self.part_list.fontMetrics().height() + 8
        self.part_list.setFixedHeight(rows * row_height + 2 * self.part_list.frameWidth() + 2)
        self.queue_box.setVisible(bool(self._parts))
        self._update_part_buttons()

    def _part_marks(self) -> list[str]:
        """发送序列每一组右边的小字：整组在进度之前＝“已说完”，进度所在的那组＝“← 目前在这”加这一组里说到的百分比，
        之后的＝“还没说”。进度为 0（还没开始、内容全新、点了重置）时只写字数。"""
        chars = [exact_chars(count) for count in self._part_chars]
        resume = self._resume_pos()
        text = self._story_cache
        if not resume or text is None or len(text.parts) != len(self._parts):
            return chars
        current = text.part_at(resume)
        marks = []
        for index, (count, (_title, start, end)) in enumerate(zip(chars, text.parts)):
            if index < current:
                marks.append(f"{count}　已说完")
            elif index == current:
                percent = int((resume - start) * 100 / (end - start)) if end > start else 0
                marks.append(f"← 目前在这　{min(percent, 100)}%")
            else:
                marks.append(f"{count}　还没说")
        return marks

    def _update_line_hint(self) -> None:
        if self.by_line_radio.isChecked():
            self.line_hint.setText("不同行的内容不会在同一条弹幕中")
        else:
            self.line_hint.setText("一行说完接着说下一行，原来换行的地方用空格隔开")

    def _reset_progress(self) -> None:
        self.reset_progress = True
        self._update_story_labels(self._selected_part())

    def _update_story_labels(self, select: int | None = None) -> None:
        if not self._story_hash:
            set_colored(self.story_label, "还没有设置说书内容", self._colors.warn)
        else:
            groups = f"{len(self._parts)} 组，" if len(self._parts) > 1 else ""
            set_colored(self.story_label, f"{groups}共 {exact_chars(self._story_chars)}", self._colors.muted)
        self.story_label.setToolTip(plain_tip(QUEUE_CHARS_TIP) if self._story_hash else "")
        self._fill_part_list(select)

        unchanged = self._room is not None and self.new_story_text is None and self._story_hash == self._room.story_hash
        resume = self._resume_pos()
        total = self._story_chars
        if not self._story_hash:
            progress = "—"
        elif self.reset_progress:
            progress = "保存后从头开始说"
        elif resume:
            where = f"{resume / total:.1%}（已说到第 {resume:,} 字，剩余 {total - resume:,} 字{self._part_text(resume)}）"
            progress = where if unchanged else f"已经说过的部分没变，保存后接着说：{where}"
        elif not unchanged:
            progress = "内容是新的，保存后从头开始说"
        else:
            progress = "还没开始说"
        self.progress_label.setText(f"说书进度：{progress}")
        if self.reset_progress or not (resume or unchanged):
            short = "保存后从头开始说"
        elif resume:
            short = f"总进度 {resume / total:.1%}" if unchanged else f"保存后接着说　{resume / total:.1%}"
        else:
            short = "还没开始说"
        self.progress_short.setText(short)
        self.progress_short.setToolTip(plain_tip(self.progress_label.text()))
        for widget in (self.progress_short, self.reset_btn):  # 没有内容时不用说进度
            widget.setVisible(bool(self._story_hash))
        self.reset_btn.setEnabled(resume > 0)
        self._update_estimate()

    def _part_text(self, pos: int) -> str:
        """发送序列有好几组时，说一下 pos 在第几组。"""
        text = self._story_cache
        if text is None or len(text.parts) < 2:
            return ""
        index = text.part_at(pos)
        return f"，正在说第 {index + 1}/{len(text.parts)} 组《{text.parts[index][0]}》"

    def _story_for_estimate(self) -> StoryText | None:
        return self._story_cache if self._story_hash else None

    def _resume_pos(self) -> int:
        """保存后从哪里开始说：没点重置、已经说了一部分，而且说过的那部分内容没变（比如只是在后面加了几组），
        就从进度那里接着说；否则从头说。和引擎里的规则一样（见 story.kept_position）。"""
        if self.reset_progress or not self._progress or self._story_cache is None:
            return 0
        pos = self._progress[0]
        if self._room is not None and self._story_hash == self._room.story_hash:
            return pos
        return kept_position(self._original_text, self._story_cache, pos)

    def _update_estimate(self) -> None:
        """说书预计：一共多少条、按现在的间隔多久说完，以及从现在的进度接着说还要多久。随机间隔按平均值算。"""
        if not hasattr(self, "interval_spin"):
            return  # 界面还没搭完
        text = self._story_for_estimate()
        if text is None or text.length == 0:
            self.estimate_label.setText("—")
            self._schedule_fit()
            return
        size, by_line = self.chunk_spin.value(), self.by_line_radio.isChecked()
        low, high = self.interval_spin.value(), max(self.interval_spin.value(), self.max_spin.value())
        if self.random_check.isChecked():
            delay, every = (low + high) / 2, f"{low}～{high}"
        else:
            delay, every = low, f"{low}"
        total = count_chunks(text, size, by_line)
        lines = [f"共 {total:,} 条，每 {every} 秒一条，约 {format_duration(total * delay)}后结束"]
        pos = self._resume_pos()
        if 0 < pos < text.length:
            left = count_chunks(text, size, by_line, pos)
            lines.append(f"目前剩余 {left:,} 条，约 {format_duration(left * delay)}后结束")
        self.estimate_label.setText("\n".join(lines))
        self._schedule_fit()

    # ------------------------------------------------------------ 间隔与保存

    def _update_interval_widgets(self) -> None:
        random_on = self.random_check.isChecked()
        for widget in (self.max_label, self.max_spin, self.max_unit):
            widget.setVisible(random_on)
        self.max_spin.setMinimum(self.interval_spin.value())
        tip = f"建议发送间隔不短于{SUGGESTED_MIN_INTERVAL}秒。短于{SUGGESTED_MIN_INTERVAL}秒时，可能会被B站拦截导致发送失败"
        if random_on:
            tip += "；每条弹幕在这两个时间之间随机选一个间隔"
        # 设得比建议的短时，提示变成黄色
        too_fast = self.interval_spin.value() < SUGGESTED_MIN_INTERVAL
        set_colored(self.interval_hint, tip, self._colors.warn if too_fast else self._colors.muted)
        self._update_estimate()

    def _update_limit_widgets(self) -> None:
        self.count_spin.setEnabled(self.count_check.isChecked())
        self.minutes_spin.setEnabled(self.minutes_check.isChecked())
        for edit in (self.window_start_edit, self.window_end_edit):
            edit.setEnabled(self.window_check.isChecked())

    def _update_save_button(self) -> None:
        if not hasattr(self, "buttons"):
            return
        mode = self._mode()
        if mode == MODE_STORY:
            content_ok = bool(self._story_hash)
        elif mode == MODE_REPEAT:
            content_ok = bool(self.msg_edit.text().strip()) or self.emoticon_strip.count() > 0
        else:
            content_ok = True
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(self._resolved is not None and content_ok)


# ---------------------------------------------------------------- 直接粘贴 / 编辑说书文字


class StoryTextDialog(QDialog):
    def __init__(self, parent: QWidget | None, store: FavoritesStore, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle("说书内容")
        self.resize(720, 560)
        self._store = store
        self.result_title = ""
        self.result_text = ""

        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("标题，比如小说名，不填就用内容的开头")
        self.content_edit = QPlainTextEdit()
        self.content_edit.setPlaceholderText("把要说的文字粘贴到这里，不限字数")
        self.content_edit.setPlainText(text)
        self.count_label = hint_label()
        import_btn = QPushButton("从 txt 导入")
        import_btn.setAutoDefault(False)
        import_btn.clicked.connect(self._import)
        bar = QHBoxLayout()
        bar.addWidget(self.count_label, 1)
        bar.addWidget(import_btn)

        self.save_fav_check = QCheckBox("同时存入收藏夹")
        self.tag_editor = TagEditor(store)
        self.tag_editor.setVisible(False)  # 勾了“同时存入收藏夹”才需要填标签
        self.save_fav_check.toggled.connect(self.tag_editor.setVisible)

        form = _form()
        form.addRow("标题：", self.title_edit)
        self.buttons = save_cancel_buttons(self, "确定")
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.content_edit, 1)
        layout.addLayout(bar)
        layout.addWidget(self.save_fav_check)
        layout.addWidget(self.tag_editor)
        layout.addWidget(self.buttons)

        self.content_edit.textChanged.connect(self._update_count)
        self._update_count()

    def _update_count(self) -> None:
        count = count_chars(self.content_edit.toPlainText())
        self.count_label.setText(f"共 {exact_chars(count)}")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(count > 0)

    def _import(self) -> None:
        result = import_txt(self)
        if result is None:
            return
        name, text = result
        self.content_edit.setPlainText(text)
        if not self.title_edit.text().strip():
            self.title_edit.setText(name)

    def accept(self) -> None:
        text = self.content_edit.toPlainText()
        if not text.strip():
            return
        self.result_text = text
        self.result_title = self.title_edit.text().strip()
        if self.save_fav_check.isChecked():
            title = self.result_title or " ".join(text.split())[:20]
            self._store.add(title, self.tag_editor.tags(), text)
        super().accept()
