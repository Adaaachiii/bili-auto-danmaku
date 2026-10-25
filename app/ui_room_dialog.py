"""添加 / 编辑直播间：直播间、模式（独轮车 / 说书人 / 只提醒）、发送间隔、发送限制、其他选项。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRect, Qt, QTime, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
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
    RoomConfig,
    parse_hhmm,
)
from .favorites import FavoritesStore, count_chars
from .story import StoryText, count_chunks
from .ui_common import (
    MessageEdit,
    StatusColors,
    exact_chars,
    format_duration,
    help_icon,
    hint_label,
    run_async,
    save_cancel_buttons,
    set_colored,
    unit_row,
    with_help,
)

DEFAULT_LIMIT_COUNT = 200  # 勾上“每场最多发多少条”时的默认值
DEFAULT_LIMIT_MINUTES = 60
# 系统屏蔽在任何直播间被拦过都算，主播屏蔽只算这个直播间（见 history.HistoryStore.is_filtered）
SKIP_FILTERED_TIP = "若某一段落曾被系统屏蔽，或曾在直播间被主播屏蔽，在发送弹幕时自动跳过。"
REPEAT_ORDER_TIP = (
    "文字和表情按“文字→表情1→表情2→……”的顺序轮流发，拖动表情可以调整顺序；不填文字就只发表情。\n"
    "勾上“随机顺序”就每次随机挑一条，但不会连着发同一条。"
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


class RoomDialog(QDialog):
    """保存后用 result_config() 取设置；new_story_text 不为 None 表示换了说书文字，reset_progress 表示要从头说。"""

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
        self.new_story_text: str | None = None
        self.reset_progress = False
        self._story_title = room.story_title if room else ""
        self._story_hash = room.story_hash if room else ""
        self._story_chars = room.story_chars if room else 0
        self._progress = progress
        self._creds = creds
        self._story_cache: StoryText | None = None  # 算“预计多久说完”用，文字没换就不重新整理

        # 各项设置放在可以滚动的区域里，保存 / 取消按钮固定在最下面。
        # 窗口高度跟着内容走（切换模式时自动变高变矮），但不超过屏幕，放不下时出现滚动条。
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        content_layout.addWidget(self._build_room_group())
        content_layout.addWidget(self._build_mode_group())
        self.interval_group = self._build_interval_group()
        content_layout.addWidget(self.interval_group)
        self.limit_group = self._build_limit_group()
        content_layout.addWidget(self.limit_group)
        content_layout.addWidget(self._build_other_group())
        content_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.content)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(self.scroll, 1)
        self.buttons = save_cancel_buttons(self)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setAutoDefault(False)  # 输入框里按回车不要直接保存
        layout.addWidget(self.buttons)

        self._load(room)
        self._update_story_labels()
        self._update_save_button()
        if room:
            self._query()  # 顺便显示一下当前状态

    # ------------------------------------------------------------ 窗口大小

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_to_content()

    def _schedule_fit(self) -> None:
        """内容变了（切换模式、查询结果多了一行等）：等布局算好后重新定窗口大小。"""
        if self.isVisible():
            QTimer.singleShot(0, self._fit_to_content)

    def _fit_to_content(self, screen: QRect | None = None) -> None:
        """窗口高度＝内容的高度，但不超过屏幕可用高度；超出的部分用滚动条看。宽度刚好放下内容。

        screen 是屏幕上能用的区域（不含任务栏），默认是窗口所在的屏幕；测试时可以传一个小的进来。
        """
        self.content.adjustSize()
        hint = self.content.sizeHint()
        margins = self.layout().contentsMargins()
        chrome_h = margins.top() + margins.bottom() + self.layout().spacing() + self.buttons.sizeHint().height()
        chrome_w = margins.left() + margins.right()
        screen = screen or (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        title_bar = max(0, self.frameGeometry().height() - self.height()) or 32
        max_h = screen.height() - title_bar - 16
        need_h = hint.height() + chrome_h
        bar = self.scroll.verticalScrollBar().sizeHint().width() if need_h > max_h else 0
        width = min(hint.width() + chrome_w + bar, screen.width() - 16)
        self.scroll.setMinimumWidth(width - chrome_w)
        self.resize(width, min(need_h, max_h))
        # 变高以后底部跑到屏幕外面的话，往上挪
        frame = self.frameGeometry()
        if frame.bottom() > screen.bottom() or frame.top() < screen.top():
            self.move(frame.left(), max(screen.top(), screen.bottom() - frame.height()))

    # ------------------------------------------------------------ 搭界面

    def _build_room_group(self) -> QGroupBox:
        self.room_edit = QLineEdit()
        self.room_edit.setPlaceholderText("房间号或直播间链接，例如 6 或 https://live.bilibili.com/6")
        self.query_btn = QPushButton("查询")
        self.query_btn.setAutoDefault(False)
        row = QHBoxLayout()
        row.addWidget(self.room_edit, 1)
        row.addWidget(self.query_btn)
        self.room_info = hint_label("输入后点“查询”，确认是哪位主播")

        self.query_btn.clicked.connect(self._query)
        self.room_edit.returnPressed.connect(self._query)
        self.room_edit.textEdited.connect(self._on_room_edited)

        group = QGroupBox("直播间")
        group.setMinimumWidth(620)  # 窗口按内容定大小，这里定住宽度
        box = QVBoxLayout(group)
        box.addLayout(row)
        box.addWidget(self.room_info)
        return group

    def _build_mode_group(self) -> QGroupBox:
        self.repeat_radio = QRadioButton("独轮车模式")
        self.story_radio = QRadioButton("说书人模式")
        self.notify_radio = QRadioButton("只提醒（不发弹幕）")
        self.mode_group = QButtonGroup(self)
        mode_row = QHBoxLayout()
        for radio in (self.repeat_radio, self.story_radio, self.notify_radio):
            self.mode_group.addButton(radio)
            mode_row.addWidget(radio)
            mode_row.addSpacing(16)
        mode_row.addStretch(1)
        self.mode_hint = hint_label()

        self.repeat_page = self._build_repeat_page()
        self.story_page = self._build_story_page()
        self.mode_group.buttonToggled.connect(lambda _button, checked: checked and self._on_mode_changed())

        group = QGroupBox("模式")
        box = QVBoxLayout(group)
        box.addLayout(mode_row)
        box.addWidget(self.mode_hint)
        box.addWidget(self.repeat_page)
        box.addWidget(self.story_page)
        return group

    def _build_repeat_page(self) -> QWidget:
        self.msg_edit = MessageEdit(DANMAKU_MAX_LENGTH)
        self.msg_edit.setPlaceholderText("要循环发送的那句话（只发表情的话可以不填）")
        self.msg_count = hint_label()
        pick_btn = QPushButton("从收藏夹选择")
        pick_btn.setAutoDefault(False)
        pick_btn.clicked.connect(self._pick_message)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.msg_count, 1)
        pick_row.addWidget(pick_btn)
        self.msg_edit.textChanged.connect(self._update_count)

        page = QWidget()
        form = _form()
        form.setContentsMargins(0, 4, 0, 0)
        form.setVerticalSpacing(4)
        form.addRow("弹幕内容：", self.msg_edit)
        form.addRow("", pick_row)
        form.addRow("表情：", self._build_emoticon_box())
        page.setLayout(form)
        return page

    def _build_emoticon_box(self) -> QVBoxLayout:
        self.emoticon_empty = hint_label("没有选表情，只发文字")
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
        box.setSpacing(4)
        box.addWidget(self.emoticon_empty)
        box.addWidget(self.emoticon_strip)
        box.addLayout(row)
        return box

    def _build_story_page(self) -> QWidget:
        self.story_label = QLabel()
        self.story_label.setTextFormat(Qt.TextFormat.PlainText)  # 标题可能来自导入的文件名
        self.story_label.setWordWrap(True)
        edit_btn = QPushButton("编辑内容")
        pick_btn = QPushButton("从收藏夹选择")
        import_btn = QPushButton("导入 txt")
        source_row = QHBoxLayout()
        for button, handler in ((edit_btn, self._edit_story), (pick_btn, self._pick_story), (import_btn, self._import_story)):
            button.setAutoDefault(False)
            button.clicked.connect(handler)
            source_row.addWidget(button)
        source_row.addStretch(1)

        self.progress_label = QLabel()
        self.reset_btn = QPushButton("重置（从头开始说书）")
        self.reset_btn.setAutoDefault(False)
        self.reset_btn.clicked.connect(self._reset_progress)
        reset_row = QHBoxLayout()
        reset_row.addWidget(self.reset_btn)
        reset_row.addStretch(1)

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1, DANMAKU_MAX_LENGTH)
        self.chunk_spin.setFixedWidth(100)

        self.ignore_lines_radio = QRadioButton("忽略换行")
        self.by_line_radio = QRadioButton("按行说书")
        line_group = QButtonGroup(self)
        line_group.addButton(self.ignore_lines_radio)
        line_group.addButton(self.by_line_radio)
        line_row = QHBoxLayout()
        line_row.addWidget(self.ignore_lines_radio)
        line_row.addSpacing(16)
        line_row.addWidget(self.by_line_radio)
        line_row.addStretch(1)
        self.line_hint = hint_label()
        self.line_hint.setWordWrap(False)  # 一句短话，不用自动换行（自动换行会多留出一行空白）
        self.by_line_radio.toggled.connect(self._update_line_hint)
        line_box = QVBoxLayout()
        line_box.setSpacing(2)
        line_box.addLayout(line_row)
        line_box.addWidget(self.line_hint)

        self.estimate_label = QLabel()
        self.chunk_spin.valueChanged.connect(self._update_estimate)
        self.by_line_radio.toggled.connect(self._update_estimate)

        self.finish_combo = QComboBox()
        self.finish_combo.addItem("停止发送", False)
        self.finish_combo.addItem("从头循环", True)
        self.finish_combo.setFixedWidth(140)
        self.inherit_check = QCheckBox("说书状态继承（本场直播结束后停止说书，下次直播开始时从中断处继续）")
        self.skip_filtered_check = QCheckBox("跳过以前被屏蔽过的段落")

        page = QWidget()
        form = _form()
        form.setContentsMargins(0, 4, 0, 0)
        form.addRow("说书内容：", self.story_label)
        form.addRow("", source_row)
        form.addRow("说书进度：", self.progress_label)
        form.addRow("", reset_row)
        form.addRow("每条字数：", unit_row(self.chunk_spin, "字"))
        form.addRow("换行处理：", line_box)
        form.addRow("预计：", with_help(self.estimate_label, "不算被限流、被屏蔽和不在发送时段的时间"))
        form.addRow("说书结束后：", self.finish_combo)
        form.addRow("", self.inherit_check)
        form.addRow("", with_help(self.skip_filtered_check, SKIP_FILTERED_TIP))
        page.setLayout(form)
        return page

    def _build_interval_group(self) -> QGroupBox:
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
        self.interval_hint = hint_label()

        self.random_check.toggled.connect(self._update_interval_widgets)
        self.interval_spin.valueChanged.connect(self._update_interval_widgets)
        self.max_spin.valueChanged.connect(self._update_estimate)

        group = QGroupBox("发送间隔")
        box = QVBoxLayout(group)
        box.addLayout(row)
        box.addWidget(self.interval_hint)
        return group

    def _build_limit_group(self) -> QGroupBox:
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
        grid.setVerticalSpacing(6)
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

        group = QGroupBox("发送限制")
        box = QVBoxLayout(group)
        box.addLayout(grid)
        return group

    def _build_other_group(self) -> QGroupBox:
        self.open_check = QCheckBox("开播时自动打开直播页")
        self.offline_check = QCheckBox("未开播时也发送弹幕")
        self.enabled_check = QCheckBox("启用直播间状态监控")
        group = QGroupBox("其他")
        box = QVBoxLayout(group)
        for check in (self.open_check, self.offline_check, self.enabled_check):
            box.addWidget(check)
        return group

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
        set_colored(self.room_info, "输入后点“查询”，确认是哪位主播", self._colors.muted)
        self._update_save_button()

    def _query(self) -> None:
        room_id = bili_api.parse_room_input(self.room_edit.text())
        if room_id is None:
            set_colored(self.room_info, "请输入房间号，或者粘贴直播间链接", self._colors.error)
            return
        self._query_seq += 1
        seq = self._query_seq
        self.query_btn.setEnabled(False)
        set_colored(self.room_info, "正在查询…", self._colors.muted)
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
            set_colored(self.room_info, "这个直播间已经在列表里了", self._colors.error)
            self._update_save_button()
            return
        old_uname = self._resolved[2] if self._resolved else ""
        uname = status.uname if status and status.uname else old_uname
        self._resolved = (info.room_id, info.uid, uname)
        state = bili_api.LIVE_STATUS_TEXT.get(info.live_status, "未知")
        text = f"主播：{uname or '（没取到昵称）'}　　真实房间号：{info.room_id}　　当前：{state}"
        if status and status.title:
            text += f"\n直播标题：{status.title}"
        set_colored(self.room_info, text, self._colors.ok)
        self._update_save_button()
        self._schedule_fit()

    def _on_lookup_failed(self, seq: int, message: str) -> None:
        if seq != self._query_seq:
            return
        self.query_btn.setEnabled(True)
        set_colored(self.room_info, message, self._colors.error)

    # ------------------------------------------------------------ 模式

    def _on_mode_changed(self) -> None:
        mode = self._mode()
        sends = mode != MODE_NOTIFY
        self.repeat_page.setVisible(mode == MODE_REPEAT)
        self.story_page.setVisible(mode == MODE_STORY)
        self.interval_group.setVisible(sends)
        self.limit_group.setVisible(sends)
        self.offline_check.setVisible(sends)
        self.mode_hint.setText({
            MODE_REPEAT: "按间隔一直发送同一句弹幕；也可以选几个表情，和文字轮流发。",
            MODE_STORY: "把一段长文字按顺序切成多条弹幕，一条接一条地发，直到全部说完。适合发小说或大段文字。",
            MODE_NOTIFY: "开播、下播时弹系统通知（不受设置里“系统通知”开关的影响），开播时可以自动打开直播页，不发弹幕。",
        }[mode])
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

    def _current_story_text(self) -> str:
        if self.new_story_text is not None:
            return self.new_story_text
        if self._room and self._story_hash:
            return story.load_room_story(self._room.room_id) or ""
        return ""

    def _set_story(self, text: str, title: str) -> None:
        digest, chars = story.text_fingerprint(text)
        if chars == 0:
            QMessageBox.warning(self, "没有内容", "这段文字里没有可以发送的内容")
            return
        if digest != self._story_hash:
            self.reset_progress = False  # 换了文字本来就会从头说
        self.new_story_text = text
        self._story_title = title or text.strip().splitlines()[0][:20]
        self._story_hash = digest
        self._story_chars = chars
        self._update_story_labels()
        self._update_save_button()

    def _pick_story(self) -> None:
        dialog = FavoritePickerDialog(self, self._store)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_favorite:
            favorite = dialog.result_favorite
            self._set_story(favorite.content, favorite.title)

    def _import_story(self) -> None:
        result = import_txt(self)
        if result is not None:
            name, text = result
            self._set_story(text, name)

    def _edit_story(self) -> None:
        dialog = StoryTextDialog(self, self._store, self._story_title, self._current_story_text())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_story(dialog.result_text, dialog.result_title)

    def _update_line_hint(self) -> None:
        if self.by_line_radio.isChecked():
            self.line_hint.setText("不同行的内容不会在同一条弹幕中")
        else:
            self.line_hint.setText("一行说完接着说下一行，原来换行的地方用空格隔开")

    def _reset_progress(self) -> None:
        self.reset_progress = True
        self._update_story_labels()

    def _update_story_labels(self) -> None:
        if not self._story_hash:
            set_colored(self.story_label, "还没有设置说书内容", self._colors.warn)
        else:
            self.story_label.setStyleSheet("")
            self.story_label.setText(f"《{self._story_title}》　共 {exact_chars(self._story_chars)}（每行首尾空白和空行不计）")

        unchanged = self._room is not None and self.new_story_text is None and self._story_hash == self._room.story_hash
        if not self._story_hash:
            self.progress_label.setText("—")
        elif not unchanged:
            self.progress_label.setText("内容是新的，保存后从头开始说")
        elif self.reset_progress:
            self.progress_label.setText("保存后从头开始说")
        elif self._progress and self._progress[1]:
            pos, total = self._progress
            self.progress_label.setText(f"{pos / total:.1%}（已说到第 {pos:,} 字，剩余 {total - pos:,} 字）")
        else:
            self.progress_label.setText("还没开始说")
        self.reset_btn.setEnabled(unchanged and not self.reset_progress and bool(self._progress and self._progress[0]))
        self._update_estimate()

    def _story_for_estimate(self) -> StoryText | None:
        if not self._story_hash:
            return None
        if self._story_cache is None or self._story_cache.hash != self._story_hash:
            raw = self._current_story_text()
            self._story_cache = StoryText(raw) if raw else None
        return self._story_cache

    def _resume_pos(self) -> int:
        """保存后从哪里开始说：内容没换、没点重置、已经说了一部分，就从进度那里接着说。"""
        unchanged = self._room is not None and self.new_story_text is None and self._story_hash == self._room.story_hash
        if unchanged and not self.reset_progress and self._progress:
            return self._progress[0]
        return 0

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
        if random_on:
            self.interval_hint.setText(f"弹幕发送间隔最短为{MIN_SEND_INTERVAL}秒；每条弹幕在这两个时间之间随机选一个间隔")
        else:
            self.interval_hint.setText(f"弹幕发送间隔最短为{MIN_SEND_INTERVAL}秒")
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
