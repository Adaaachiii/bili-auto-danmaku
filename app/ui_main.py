"""主窗口：左侧功能栏（直播间、弹幕收藏夹、历史记录、设置、账号），右侧对应页面，以及右下角托盘图标。"""

from __future__ import annotations

import html
import logging
import os
import sys
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QPoint, QSize, Qt, QTimer, QTranslator
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import backup, bili_api, config, secure_store, story, system, updater
from .bili_api import AccountInfo, Credentials
from .config import (
    APP_ID,
    APP_TITLE,
    APP_VERSION,
    BROWSER_CHOICES,
    CLOSE_ASK,
    CLOSE_QUIT,
    CLOSE_TRAY,
    MODE_STORY,
    RoomConfig,
    data_dir,
    load_config,
    save_config,
)
from .engine import Engine, RoomView, Session
from .favorites import FavoritesStore
from .history import HistoryStore
from .ui_common import (
    NoFocusDelegate,
    StatusColors,
    circle_avatar,
    hint_label,
    log_timestamp,
    make_app_icon,
    nav_icon,
    plain_tip,
    run_async,
)
from .ui_dialogs import LoginDialog, fetch_account, show_notice
from .ui_favorites import FavoritesPage
from .ui_history import HistoryPage
from .ui_room_dialog import RoomDialog
from .ui_settings import SettingsPage

log = logging.getLogger(__name__)

COLUMNS = ["启用", "主播", "房间号", "状态", "弹幕内容", "间隔", "本场已发", "最近一次发送"]
COL_ENABLED, COL_NAME, COL_ROOM, COL_STATUS, COL_MESSAGE, COL_INTERVAL, COL_SENT, COL_LAST = range(len(COLUMNS))

SESSION_HINT = "只影响当前这一段（这场直播，或这次未开播期间），开播或下播后照常自动处理"
WAIT_HINT = "开始监控（勾选、程序启动）时主播已经在播，或者未开播但设置了“未开播时也发送”：\n这一段点“本场开始发送”才发送，下次开播会自动发送"

PAGE_ROOMS, PAGE_FAVORITES, PAGE_HISTORY, PAGE_SETTINGS = range(4)
AVATAR_SIZE = 36
NAV_ICON_SIZE = 18
LOG_LINES = 2000  # 运行日志面板最多留这么多条，内存不会越用越多
# 运行日志的筛选：（显示的文字, 显示哪些级别，None 表示全部）
LOG_FILTERS = [("全部", None), ("警告和错误", ("warn", "error")), ("只看错误", ("error",))]


def _dot(color: str) -> str:
    return f'<span style="color:{color}">●</span>'


class MainWindow(QMainWindow):
    def __init__(self, use_tray: bool = True):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_app_icon())
        self.resize(1240, 760)
        self.setMinimumSize(1000, 600)

        self.cfg = load_config()
        self.colors = StatusColors()
        self.favorites = FavoritesStore()
        self.history = HistoryStore()
        # 上次程序没正常退出时还在记的历史记录，按最后一次发送的时间结束掉
        self.history.close_unfinished("程序异常退出")
        self.creds: Credentials | None = None
        self.account: AccountInfo | None = None
        self._avatar: QPixmap | None = None
        self._account_state = "none"
        self._quitting = False
        self._tray_hint_shown = False
        self._notice_pending = False
        self._updating_table = False
        self._log_entries: deque[tuple[str, str, str]] = deque(maxlen=LOG_LINES)  # (时间, 级别, 内容)

        self.engine = Engine(self.cfg.poll_interval_sec, history=self.history)
        self.engine.room_changed.connect(self._on_room_changed)
        self.engine.log_message.connect(self._append_log)
        self.engine.notify.connect(self._notify)
        self.engine.remind.connect(self._remind)
        self.engine.open_page.connect(self._open_page)
        self.engine.auth_invalid.connect(self._on_auth_invalid)

        self._build_ui()
        self.tray = self._build_tray() if use_tray and QSystemTrayIcon.isSystemTrayAvailable() else None

        self.engine.set_rooms(self.cfg.rooms)
        self._rebuild_table()
        self._load_saved_login()
        if not self.cfg.auto_monitor:
            self.engine.set_paused(True)  # 在后台线程开始之前就暂停，一次都不检测
        self.engine.start()

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._update_monitor_label)
        self._clock.start(1000)
        self._update_monitor_label()
        self._log("info", "程序已启动")
        if not self.cfg.auto_monitor:
            self._show_paused(True)
            self._log("info", "按设置，启动后先不检测直播间，点“继续监控”开始")

    # ================================================================ 界面搭建

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.pages = QStackedWidget()
        rooms_page = QWidget()
        self.favorites_page = FavoritesPage(self.favorites)
        self.history_page = HistoryPage(self.history)
        self.engine.history_changed.connect(self.history_page.refresh_if_visible)
        self.settings_page = SettingsPage()
        self.settings_page.saved.connect(self._save_settings)
        self.settings_page.update_requested.connect(self._update_from_zip)
        self.settings_page.notice_requested.connect(lambda: show_notice(self))
        self.settings_page.backup_requested.connect(self._export_backup)
        self.settings_page.restore_requested.connect(self._restore_backup)
        self.pages.addWidget(rooms_page)
        self.pages.addWidget(self.favorites_page)
        self.pages.addWidget(self.history_page)
        self.pages.addWidget(self.settings_page)

        outer.addWidget(self._build_sidebar())
        outer.addWidget(self.pages, 1)

        root = QVBoxLayout(rooms_page)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # ---- 顶部卡片：监控状态
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 10, 16, 10)
        grid.setHorizontalSpacing(12)

        self.monitor_label = QLabel()
        self.pause_btn = QPushButton("暂停监控")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.poll_btn = QPushButton("立即检测")
        self.poll_btn.setToolTip("不等倒计时，马上查一次所有直播间的开播状态")
        self.poll_btn.clicked.connect(self.engine.request_poll)
        grid.addWidget(self._caption("监控"), 0, 0)
        grid.addWidget(self.monitor_label, 0, 1)
        grid.addWidget(self.pause_btn, 0, 2)
        grid.addWidget(self.poll_btn, 0, 3)
        grid.setColumnStretch(1, 1)

        # ---- 直播间列表
        self.add_btn = QPushButton("＋ 添加直播间")
        self.add_btn.clicked.connect(self._add_room)
        self.edit_btn = QPushButton("编辑")
        self.edit_btn.clicked.connect(self._edit_room)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.clicked.connect(self._delete_room)
        self.open_btn = QPushButton("打开直播间")
        self.open_btn.clicked.connect(self._open_selected_room)
        self.start_btn = QPushButton("本场开始发送")
        self.start_btn.setToolTip(SESSION_HINT)
        self.start_btn.clicked.connect(self._start_session)
        self.stop_btn = QPushButton("本场停止发送")
        self.stop_btn.setToolTip(SESSION_HINT)
        self.stop_btn.clicked.connect(self._stop_session)

        bar = QHBoxLayout()
        bar.addWidget(self._section_title("直播间"))
        bar.addSpacing(8)
        for button in (self.add_btn, self.edit_btn, self.delete_btn, self.open_btn):
            bar.addWidget(button)
        bar.addStretch(1)
        bar.addWidget(self.start_btn)
        bar.addWidget(self.stop_btn)

        # 第二行：全选（启用 / 停用全部）、搜索、多选提示
        self.all_check = QCheckBox("全选")
        self.all_check.setTristate(True)
        self.all_check.setToolTip("勾上＝监控列表里的全部直播间，取消＝全部不监控。搜索时只作用于搜到的直播间")
        self.all_check.clicked.connect(self._toggle_all_enabled)
        self.room_search = QLineEdit()
        self.room_search.setPlaceholderText("搜索主播、房间号或弹幕内容")
        self.room_search.setClearButtonEnabled(True)
        self.room_search.setMaximumWidth(340)
        self.room_search.textChanged.connect(self._apply_room_filter)
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(self.all_check)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(self.room_search, 1)
        filter_bar.addStretch(1)
        multi_hint = hint_label("按住 Ctrl 或 Shift 可多选，批量操作")
        multi_hint.setWordWrap(False)
        filter_bar.addWidget(multi_hint)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_MESSAGE, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_room_buttons)
        self.table.cellDoubleClicked.connect(lambda _row, _col: self._edit_room())

        empty = QLabel("还没有直播间。\n点上面的“＋ 添加直播间”，填上房间号和要发的弹幕就行。")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(f"color: {self.colors.muted}; font-size: 11pt;")
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(empty)
        self.table_stack.addWidget(self.table)

        rooms_panel = QWidget()
        rooms_layout = QVBoxLayout(rooms_panel)
        rooms_layout.setContentsMargins(0, 0, 0, 0)
        rooms_layout.setSpacing(8)
        rooms_layout.addLayout(bar)
        rooms_layout.addLayout(filter_bar)
        rooms_layout.addWidget(self.table_stack, 1)

        # ---- 运行日志
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)
        self.log_level_combo = QComboBox()
        for label, levels in LOG_FILTERS:
            self.log_level_combo.addItem(label, levels)
        self.log_level_combo.currentIndexChanged.connect(self._render_log)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜索日志")
        self.log_search.setClearButtonEnabled(True)
        self.log_search.setMaximumWidth(260)
        self.log_search.textChanged.connect(self._render_log)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._clear_log)
        log_bar = QHBoxLayout()
        log_bar.addWidget(self._section_title("运行日志"))
        log_bar.addSpacing(8)
        log_bar.addWidget(self.log_level_combo)
        log_bar.addWidget(self.log_search, 1)
        log_bar.addStretch(1)
        log_bar.addWidget(clear_btn)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log_view, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(rooms_panel)
        splitter.addWidget(log_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        root.addWidget(card)
        root.addWidget(splitter, 1)

        c = self.colors
        self.setStyleSheet(
            "QFrame#card { border: 1px solid palette(mid); border-radius: 8px; background: palette(base); }"
            # 选中行画成一整条，而不是每格一个圆角块
            f"QTableWidget {{ selection-background-color: {c.selection}; selection-color: palette(text); }}"
            "QTableWidget::item { padding: 0 6px; border: none; }"
            f"QTableWidget::item:selected {{ background: {c.selection}; }}"
            # 左侧功能栏
            f"QFrame#sidebar {{ background: {c.sidebar}; border-right: 1px solid palette(mid); }}"
            "QPushButton[nav='true'], QPushButton#account {"
            " text-align: left; padding: 9px 14px; border: none; border-radius: 6px; background: transparent; }"
            f"QPushButton[nav='true']:hover, QPushButton#account:hover {{ background: {c.hover}; }}"
            f"QPushButton[nav='true']:checked {{ background: {c.selection}; font-weight: bold; }}"
            "QPushButton#account { padding: 6px 8px; }"
        )

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)

        brand_icon = QLabel()
        brand_icon.setPixmap(make_app_icon().pixmap(24, 24))
        brand_text = QLabel(APP_TITLE)
        brand_text.setStyleSheet("font-weight: bold;")
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 4, 0, 10)
        brand.addWidget(brand_icon)
        brand.addWidget(brand_text, 1)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        def nav_button(text: str, page: int, icon: str) -> QPushButton:
            button = QPushButton(text)
            button.setIcon(nav_icon(icon))  # 图标在左，文字紧跟在图标右边
            button.setIconSize(QSize(NAV_ICON_SIZE, NAV_ICON_SIZE))
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav_group.addButton(button, page)
            return button

        self.nav_rooms = nav_button("直播间", PAGE_ROOMS, "rooms")
        self.nav_favorites = nav_button("弹幕收藏夹", PAGE_FAVORITES, "favorites")
        self.nav_history = nav_button("历史记录", PAGE_HISTORY, "history")
        self.nav_settings = nav_button("设置", PAGE_SETTINGS, "settings")
        self.nav_group.idClicked.connect(self._show_page)
        self.nav_rooms.setChecked(True)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: palette(mid);")

        self.account_btn = QPushButton()
        self.account_btn.setObjectName("account")
        self.account_btn.setIconSize(QSize(AVATAR_SIZE, AVATAR_SIZE))
        self.account_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_btn.clicked.connect(self._on_account_clicked)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 14, 10, 12)
        layout.setSpacing(4)
        layout.addLayout(brand)
        layout.addWidget(self.nav_rooms)
        layout.addWidget(self.nav_favorites)
        layout.addWidget(self.nav_history)
        layout.addStretch(1)
        layout.addWidget(self.nav_settings)
        layout.addSpacing(6)
        layout.addWidget(line)
        layout.addSpacing(6)
        layout.addWidget(self.account_btn)
        return sidebar

    def _show_page(self, page: int) -> None:
        if page == PAGE_SETTINGS:
            self.settings_page.load(self.cfg, system.is_autostart_enabled())
        self.pages.setCurrentIndex(page)
        self.nav_group.button(page).setChecked(True)

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {self.colors.muted}; font-weight: bold;")
        return label

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 11pt; font-weight: bold;")
        return label

    def _build_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(make_app_icon(), self)
        tray.setToolTip(APP_TITLE)
        menu = QMenu(self)
        show_action = QAction("显示主窗口", menu)
        show_action.triggered.connect(self.show_window)
        self.tray_pause_action = QAction("暂停监控", menu)
        self.tray_pause_action.triggered.connect(self._toggle_pause)
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(lambda: self.quit_app())  # triggered 会带一个参数，不能直接连
        menu.addAction(show_action)
        menu.addAction(self.tray_pause_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    # ================================================================ 直播间列表

    def _rebuild_table(self) -> None:
        self._updating_table = True
        try:
            self.table.setRowCount(len(self.cfg.rooms))
            for row, room in enumerate(self.cfg.rooms):
                self._fill_row(row, room)
        finally:
            self._updating_table = False
        self.table_stack.setCurrentIndex(1 if self.cfg.rooms else 0)
        self._apply_room_filter()

    def _room_matches(self, room: RoomConfig, keyword: str) -> bool:
        if not keyword:
            return True
        fields = [self._room_name(room), str(room.room_id), room.message, room.story_title, *(e["name"] for e in room.emoticons)]
        return any(keyword in field.lower() for field in fields)

    def _apply_room_filter(self) -> None:
        """按搜索框的内容藏起不相关的直播间。"""
        keyword = self.room_search.text().strip().lower()
        for row, room in enumerate(self.cfg.rooms):
            hidden = not self._room_matches(room, keyword)
            self.table.setRowHidden(row, hidden)
            if hidden:  # 藏起来的行不能还留在“选中”里，不然批量操作会误伤
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    if item is not None:
                        item.setSelected(False)
        self._update_all_check()
        self._update_room_buttons()

    def _visible_rooms(self) -> list[RoomConfig]:
        return [room for row, room in enumerate(self.cfg.rooms) if not self.table.isRowHidden(row)]

    def _update_all_check(self) -> None:
        """“全选”框：全部启用打勾，全部停用不勾，一部分启用显示半选。"""
        rooms = self._visible_rooms()
        enabled = sum(1 for room in rooms if room.enabled)
        if rooms and enabled == len(rooms):
            state = Qt.CheckState.Checked
        elif enabled:
            state = Qt.CheckState.PartiallyChecked
        else:
            state = Qt.CheckState.Unchecked
        self.all_check.blockSignals(True)
        self.all_check.setCheckState(state)
        self.all_check.blockSignals(False)
        self.all_check.setEnabled(bool(rooms))

    def _toggle_all_enabled(self) -> None:
        rooms = self._visible_rooms()
        if not rooms:
            return
        target = not all(room.enabled for room in rooms)  # 全部启用时点一下＝全部停用，否则＝全部启用
        changed = [room for room in rooms if room.enabled != target]
        for room in changed:
            room.enabled = target
        if changed:
            self._log("info", f"已{'开始' if target else '取消'}监控 {len(changed)} 个直播间")
            self._rooms_changed()
        else:
            self._update_all_check()

    def _refresh_row(self, room_id: int) -> None:
        row = self._row_of(room_id)
        if row is None:
            return
        self._updating_table = True
        try:
            self._fill_row(row, self.cfg.rooms[row])
        finally:
            self._updating_table = False

    def _row_of(self, room_id: int) -> int | None:
        return next((i for i, room in enumerate(self.cfg.rooms) if room.room_id == room_id), None)

    def _cell(self, row: int, column: int) -> QTableWidgetItem:
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, column, item)
        return item

    def _set_cell(self, row: int, column: int, text: str, color: str | None = None, bold: bool = False, tooltip: str = "") -> None:
        item = self._cell(row, column)
        item.setText(text)
        item.setForeground(QColor(color) if color else self.table.palette().text())
        font = item.font()
        font.setBold(bold)
        item.setFont(font)
        item.setToolTip(plain_tip(tooltip))  # 直播标题、主播昵称、表情名字都是别人定的

    def _fill_row(self, row: int, room: RoomConfig) -> None:
        view = self.engine.view(room.room_id)

        enabled_item = self._cell(row, COL_ENABLED)
        enabled_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        enabled_item.setCheckState(Qt.CheckState.Checked if room.enabled else Qt.CheckState.Unchecked)
        enabled_item.setData(Qt.ItemDataRole.UserRole, room.room_id)
        enabled_item.setToolTip("勾选＝监控这个直播间")

        title = view.title if view else ""
        self._set_cell(row, COL_NAME, self._room_name(room), tooltip=f"直播标题：{title}" if title else "")
        self._set_cell(row, COL_ROOM, str(room.room_id))
        text, color, bold = self._status_of(room, view)
        status_tip = WAIT_HINT if room.enabled and view is not None and view.session is Session.WAIT_NEXT and room.sends else ""
        self._set_cell(row, COL_STATUS, text, color, bold, tooltip=status_tip)
        content, content_tip = self._content_text(room, view)
        self._set_cell(row, COL_MESSAGE, content, tooltip=content_tip)
        self._set_cell(row, COL_INTERVAL, room.interval_text(), tooltip=room.limits_text())
        counting = view is not None and view.session in (
            Session.SENDING, Session.STOPPED, Session.MUTED, Session.FINISHED, Session.LIMITED
        )
        self._set_cell(row, COL_SENT, f"{view.sent_count} 条" if counting and room.sends else "—")
        last = view.last_result if view else ""
        last_color = None if not last else (self.colors.ok if view.last_ok else self.colors.error)
        self._set_cell(row, COL_LAST, last, last_color)

    @staticmethod
    def _content_text(room: RoomConfig, view: RoomView | None) -> tuple[str, str]:
        """“弹幕内容”一列：独轮车显示那句话（和选的表情）；说书人显示标题和进度。返回（文字, 提示）。"""
        if not room.sends:
            return "【只提醒】", "开播、下播时提醒，不发弹幕"
        if room.mode != MODE_STORY:
            tip = room.repeat_text(max_names=len(room.emoticons))
            if room.repeat_random and len(room.repeat_items()) > 1:
                tip += "（随机顺序）"
            return room.repeat_text(), tip
        if view and view.story_len:
            progress = f"{view.story_pos / view.story_len:.1%}"
            tip = f"说书进度：已说到第 {view.story_pos:,} 字，剩余 {view.story_len - view.story_pos:,} 字"
        else:
            progress, tip = "", ""
        return f"【说书】《{room.story_title}》　{progress}", tip

    def _status_of(self, room: RoomConfig, view: RoomView | None) -> tuple[str, str, bool]:
        """返回（状态文字, 颜色, 是否加粗）。"""
        c = self.colors
        if not room.enabled:
            return "未监控", c.muted, False
        if self.engine.is_paused():
            return "监控已暂停", c.muted, False
        if view and view.story_missing:
            return "找不到说书内容，请编辑重新设置", c.error, False
        if view is None or view.live is None:
            if view and view.error:
                return f"检测失败：{view.error}", c.error, False
            return "检测中…", c.muted, False
        if view.live:
            prefix = "直播中"
        else:
            prefix = "未开播（轮播中）" if view.live_status == bili_api.LIVE_ROUND else "未开播"
        if not room.sends:
            return f"{prefix} · 只提醒", (c.ok if view.live else c.muted), bool(view.live)
        if view.session is Session.IDLE:
            return prefix, c.muted, False
        if view.session is Session.WAIT_NEXT:
            return f"{prefix} · 等待手动开始发送", c.warn, True
        if view.session is Session.STOPPED:
            return f"{prefix} · 已停发", c.warn, True
        if view.session is Session.MUTED:
            return f"{prefix} · 被禁言，已停发", c.error, True
        if view.session is Session.FINISHED:
            return f"{prefix} · 说书已说完", c.warn, True
        if view.session is Session.LIMITED:
            return f"{prefix} · 已达本场上限，已停发", c.warn, True
        if not self.engine.has_credentials():
            return f"{prefix} · 未登录，没有发送", c.warn, True
        if not room.in_window(datetime.now()):
            return f"{prefix} · 不在发送时段（{room.window_text()}）", c.warn, True
        return f"{prefix} · 正在发送", c.ok, True

    def _room_name(self, room: RoomConfig) -> str:
        view = self.engine.view(room.room_id)
        return (view.uname if view and view.uname else room.uname) or f"房间 {room.room_id}"

    def _selected_rooms(self) -> list[RoomConfig]:
        """选中的直播间（可以多选），按列表顺序。"""
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        return [self.cfg.rooms[row] for row in rows if 0 <= row < len(self.cfg.rooms) and not self.table.isRowHidden(row)]

    def _selected_room(self) -> RoomConfig | None:
        """只选中一个直播间时返回它（编辑只能一个一个来）。"""
        rooms = self._selected_rooms()
        return rooms[0] if len(rooms) == 1 else None

    def _update_room_buttons(self) -> None:
        count = len(self._selected_rooms())
        self.edit_btn.setEnabled(count == 1)
        for button in (self.delete_btn, self.open_btn, self.start_btn, self.stop_btn):
            button.setEnabled(count > 0)

    def _rooms_changed(self) -> None:
        self._save_config()
        self.engine.set_rooms(self.cfg.rooms)
        self._rebuild_table()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != COL_ENABLED:
            return
        room = self.cfg.find_room(item.data(Qt.ItemDataRole.UserRole))
        enabled = item.checkState() == Qt.CheckState.Checked
        if room is None or room.enabled == enabled:
            return
        room.enabled = enabled
        self._log("info", f"[{self._room_name(room)}] 已{'开始' if enabled else '取消'}监控")
        self._rooms_changed()

    def _on_room_changed(self, room_id: int) -> None:
        self._refresh_row(room_id)
        view = self.engine.view(room_id)
        room = self.cfg.find_room(room_id)
        if view and room and view.uname and view.uname != room.uname:
            room.uname = view.uname  # 主播改了昵称，顺手记下来
            self._save_config()

    def _show_context_menu(self, pos) -> None:
        rooms = self._selected_rooms()
        if not rooms:
            return
        menu = QMenu(self)
        if len(rooms) > 1:
            header = menu.addAction(f"已选中 {len(rooms)} 个直播间")
            header.setEnabled(False)
            menu.addSeparator()
        items = [("编辑", self._edit_room)] if len(rooms) == 1 else []
        items += [
            ("打开直播间", self._open_selected_room),
            (None, None),
            ("本场开始发送", self._start_session),
            ("本场停止发送", self._stop_session),
        ]
        if any(room.mode == MODE_STORY for room in rooms):
            items.append(("说书从头开始", self._reset_story))
        items += [(None, None), ("删除", self._delete_room)]
        for text, handler in items:
            if text is None:
                menu.addSeparator()
            else:
                action = menu.addAction(text)
                action.triggered.connect(handler)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ---- 直播间操作

    def _add_room(self) -> None:
        dialog = RoomDialog(self, None, {room.room_id for room in self.cfg.rooms}, self.favorites, creds=lambda: self.creds)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        room = dialog.result_config()
        if not self._save_story_text(room, dialog.new_story_text):
            return
        self.cfg.rooms.append(room)
        self._log("info", f"已添加直播间：{room.uname or room.room_id}（房间号 {room.room_id}）")
        self._rooms_changed()
        self.table.selectRow(len(self.cfg.rooms) - 1)

    def _edit_room(self) -> None:
        room = self._selected_room()
        if room is None:
            return
        view = self.engine.view(room.room_id)
        progress = (view.story_pos, view.story_len) if view and view.story_len else None
        taken = {r.room_id for r in self.cfg.rooms if r.room_id != room.room_id}
        dialog = RoomDialog(self, room, taken, self.favorites, progress, creds=lambda: self.creds)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.result_config()
        if not self._save_story_text(updated, dialog.new_story_text):
            return
        index = self.cfg.rooms.index(room)
        self.cfg.rooms[index] = updated
        self._log("info", f"[{self._room_name(updated)}] 设置已修改")
        self._rooms_changed()
        if dialog.reset_progress:
            self.engine.reset_story(updated.room_id)
        self.table.selectRow(index)

    def _save_story_text(self, room: RoomConfig, text: str | None) -> bool:
        """对话框里换了说书文字的话，写进 data\\stories。失败时提示并返回 False。"""
        if text is None:
            return True
        try:
            story.save_room_story(room.room_id, text)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"说书内容没能保存：{e}")
            return False
        return True

    def _reset_story(self) -> None:
        for room in self._selected_rooms():
            if room.mode == MODE_STORY:
                self.engine.reset_story(room.room_id)

    def _delete_room(self) -> None:
        rooms = self._selected_rooms()
        if not rooms:
            return
        names = [self._room_name(room) for room in rooms]
        if len(rooms) == 1:
            question = f"确定要删除「{names[0]}」（房间号 {rooms[0].room_id}）吗？"
        else:
            listed = "、".join(names[:5]) + ("等" if len(names) > 5 else "")
            question = f"确定要删除选中的 {len(rooms)} 个直播间吗？\n（{listed}）"
        if not self._confirm("删除直播间", question, "删除"):
            return
        for room in rooms:
            self.cfg.rooms.remove(room)
            story.delete_room_story(room.room_id)
        self._log("info", f"已删除直播间：{'、'.join(names)}")
        self._rooms_changed()

    def _open_selected_room(self) -> None:
        for room in self._selected_rooms():
            self._open_page(room.room_id)

    def _start_session(self) -> None:
        rooms = self._selected_rooms()
        if not rooms:
            return
        notify_only = [room for room in rooms if not room.sends]
        rooms = [room for room in rooms if room.sends]
        if notify_only and not rooms:
            QMessageBox.information(
                self, APP_TITLE, "“只提醒”的直播间不发弹幕。想发弹幕的话，编辑直播间，把模式改成独轮车或说书人。"
            )
            return
        disabled = [room for room in rooms if not room.enabled]
        not_live = [room for room in rooms if room.enabled and not self.engine.start_session(room.room_id)]
        if len(rooms) == 1 and not notify_only:
            if disabled:
                QMessageBox.information(self, APP_TITLE, "这个直播间没有监控，先在“启用”那一列打勾。")
            elif not_live:
                QMessageBox.information(
                    self,
                    APP_TITLE,
                    "主播现在没在直播（或还没检测出结果），开播后会自动开始发送。\n"
                    "想在未开播时也发送，可以编辑这个直播间，勾选“未开播时也发送弹幕”。",
                )
        elif disabled or not_live or notify_only:
            started = len(rooms) - len(disabled) - len(not_live)
            parts = [f"{started} 个已开始发送"]
            if not_live:
                parts.append(f"{len(not_live)} 个主播没在直播（开播后会自动发送）")
            if disabled:
                parts.append(f"{len(disabled)} 个没有监控")
            if notify_only:
                parts.append(f"{len(notify_only)} 个是“只提醒”，不发弹幕")
            QMessageBox.information(self, APP_TITLE, "，".join(parts) + "。")

    def _stop_session(self) -> None:
        rooms = self._selected_rooms()
        if not rooms:
            return
        stopped = [room for room in rooms if self.engine.stop_session(room.room_id)]
        if not stopped:
            QMessageBox.information(self, APP_TITLE, "选中的直播间现在都没有在发送。" if len(rooms) > 1 else "这个直播间现在没有在发送。")

    def _open_page(self, room_id: int) -> None:
        try:
            if not system.open_live_page(room_id, self.cfg.browser):
                self._log("warn", f"没找到 {BROWSER_CHOICES[self.cfg.browser]}，已改用系统默认浏览器打开直播页")
        except OSError as e:
            self._log("error", f"打开直播页失败：{e}")

    # ================================================================ 账号

    def _load_saved_login(self) -> None:
        creds = secure_store.load_credentials()
        if creds is None:
            self._show_account("none")
            return
        self._use_credentials(creds)
        self._show_account("checking")
        run_async(
            self,
            fetch_account,
            creds,
            on_done=lambda account: self._on_saved_login_checked(creds, account),
            on_error=lambda message: self._on_saved_login_check_failed(creds, message),
        )

    def _on_saved_login_checked(self, creds: Credentials, account: AccountInfo | None) -> None:
        if creds is not self.creds:  # 验证期间已经换了账号
            return
        if account is None:
            self.engine.set_credentials(None)
            self._show_account("invalid")
            self._log("error", "保存的登录已失效，请重新登录")
            self._notify("登录已失效", "请打开程序重新登录，否则开播后发不了弹幕")
            self._rebuild_table()
            return
        self.account = account
        self._show_account("ok")
        self._load_avatar(account)
        self._log("info", f"已登录：{account.uname}")

    def _on_saved_login_check_failed(self, creds: Credentials, message: str) -> None:
        if creds is not self.creds:
            return
        self._show_account("unverified", message)
        self._log("warn", f"暂时无法验证登录状态（{message}），先继续用保存的登录")

    def _use_credentials(self, creds: Credentials | None) -> None:
        self.creds = creds
        self.engine.set_credentials(creds)
        self._rebuild_table()

    def _show_account(self, state: str, detail: str = "") -> None:
        """左下角的账号按钮：已登录显示头像和昵称，没登录显示“点击登录”。"""
        self._account_state = state
        if state == "ok" and self.account:
            # 昵称太长就省略后半截，留出头像和边距的位置
            name = self.account_btn.fontMetrics().elidedText(self.account.uname, Qt.TextElideMode.ElideRight, 116)
            text = f"{name}\nLV{self.account.level}"
            tip = f"{self.account.uname}（UID {self.account.uid}）\n点击切换账号或退出登录"
        elif state == "checking":
            text, tip = "正在验证登录…", "正在验证保存的登录"
        elif state == "unverified":
            text, tip = "已保存登录\n暂时无法验证", f"暂时无法验证登录（{detail}），会继续用保存的登录发送"
        elif state == "invalid":
            text, tip = "登录已失效\n点击重新登录", "保存的登录已经失效，点击重新登录"
        else:
            text, tip = "未登录\n点击登录", "登录后才能发送弹幕"
        self.account_btn.setText(text)
        self.account_btn.setToolTip(plain_tip(tip))
        avatar = self._avatar if state == "ok" else None
        self.account_btn.setIcon(QIcon(circle_avatar(avatar, AVATAR_SIZE)))
        color = {"invalid": self.colors.error, "none": self.colors.warn}.get(state)
        self.account_btn.setStyleSheet(f"color: {color};" if color else "")

    def _on_account_clicked(self) -> None:
        if self._account_state in ("none", "invalid"):
            self._login()
            return
        menu = QMenu(self)
        if self.account:
            header = menu.addAction(f"{self.account.uname}（UID {self.account.uid}）")
            header.setEnabled(False)
            menu.addSeparator()
        menu.addAction("切换账号").triggered.connect(self._login)
        menu.addAction("退出登录").triggered.connect(self._logout)
        # 菜单弹在按钮上方（按钮在窗口最底下）
        menu.exec(self.account_btn.mapToGlobal(QPoint(0, 0)) - QPoint(0, menu.sizeHint().height()))

    def _load_avatar(self, account: AccountInfo) -> None:
        """后台下载头像，下好后换上。下载失败就继续用默认头像。"""
        self._avatar = None
        if not account.face:
            return

        def on_done(data: bytes) -> None:
            if self.account is not account:  # 下载期间换了账号
                return
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self._avatar = pixmap
                self._show_account(self._account_state)

        run_async(self, bili_api.fetch_avatar, account.face, on_done=on_done, on_error=lambda m: log.info("头像下载失败：%s", m))

    def _login(self) -> None:
        dialog = LoginDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        creds, account = dialog.credentials, dialog.account
        try:
            secure_store.save_credentials(creds)
        except Exception as e:  # 凭据管理器的错误类型很杂，这里只需要告诉用户结果
            QMessageBox.warning(
                self,
                "保存登录失败",
                f"登录信息没能存进 Windows 凭据管理器（{e}）。\n这次运行可以正常使用，下次打开程序需要重新登录。",
            )
        self.account = account
        self._use_credentials(creds)
        self._avatar = None
        self._show_account("ok")
        self._load_avatar(account)
        self._log("info", f"已登录：{account.uname}（UID {account.uid}）")

    def _logout(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("退出登录")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要退出当前账号吗？退出后不会再发送弹幕。")
        box.setInformativeText(
            "“退出并注销”会让这份登录在 B 站服务器上也失效，更安全。\n"
            "注意：如果 Cookie 是从浏览器里复制来的，浏览器那边也会一起被退出。"
        )
        full_btn = box.addButton("退出并注销", QMessageBox.ButtonRole.AcceptRole)
        local_btn = box.addButton("只删除本机保存的", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (full_btn, local_btn):
            return

        creds = self.creds
        if clicked is full_btn and creds is not None:
            run_async(
                self,
                lambda: bili_api.logout(bili_api.new_session(), creds),
                on_done=lambda _: self._log("info", "已在 B 站服务器上注销这份登录"),
                on_error=lambda message: self._log("warn", f"服务器注销失败（{message}），本机保存的登录已删除"),
            )
        try:
            secure_store.clear_credentials()
        except Exception as e:  # 同上，只需要告诉用户结果
            self._log("error", f"删除凭据管理器里的登录失败：{e}")
        self.account = None
        self._avatar = None
        self._use_credentials(None)
        self._show_account("none")
        self._log("info", "已退出登录")

    def _on_auth_invalid(self, _message: str) -> None:
        self._show_account("invalid")
        self._notify("登录已失效", "已停止发送弹幕，请打开程序重新登录")
        self._rebuild_table()

    # ================================================================ 监控、设置、日志

    def _update_monitor_label(self) -> None:
        c = self.colors
        rooms = [room for room in self.cfg.rooms if room.enabled]
        views = [view for view in (self.engine.view(room.room_id) for room in rooms) if view]
        live = sum(1 for view in views if view.live)
        sending = sum(1 for view in views if view.session is Session.SENDING)
        if not self.engine.has_credentials():
            sending = 0
        if self.engine.is_paused():
            text = f"{_dot(c.muted)} 暂停监控"
        else:
            wait = self.engine.seconds_until_poll()
            next_text = "正在检测…" if wait == 0 else f"{wait} 秒后再次检测"
            text = (
                f"{_dot(c.ok)} 运行中　{next_text}"
                f'　<span style="color:{c.muted}">｜　监控 {len(rooms)} 个直播间，{live} 个在播，{sending} 个正在发送</span>'
            )
        self.monitor_label.setText(text)

    def _toggle_pause(self) -> None:
        paused = not self.engine.is_paused()
        self.engine.set_paused(paused)
        self._show_paused(paused)
        self._log("info", "已暂停监控" if paused else "已继续监控")

    def _show_paused(self, paused: bool) -> None:
        """按钮文字、托盘图标、表格和监控状态跟着暂停 / 继续一起变。"""
        label = "继续监控" if paused else "暂停监控"
        self.pause_btn.setText(label)
        self.poll_btn.setEnabled(not paused)  # 暂停时不检测，“立即检测”没有意义
        if self.tray:
            self.tray_pause_action.setText(label)
            self.tray.setIcon(make_app_icon(paused))
        self._rebuild_table()
        self._update_monitor_label()

    def _save_settings(self) -> None:
        page = self.settings_page
        page.apply_to(self.cfg)
        self._save_config()
        self.engine.set_poll_interval(self.cfg.poll_interval_sec)
        if page.autostart_checked() != system.is_autostart_enabled():
            try:
                system.set_autostart(page.autostart_checked())
                self._log("info", "已开启开机自动启动" if page.autostart_checked() else "已关闭开机自动启动")
            except OSError as e:
                QMessageBox.warning(self, "设置开机启动失败", str(e))
        page.show_saved()
        self._log("info", "设置已保存")

    def _save_config(self) -> None:
        try:
            save_config(self.cfg)
        except OSError as e:
            self._log("error", f"保存配置失败：{e}")

    def _log(self, level: str, text: str) -> None:
        """界面自己产生的日志：写进日志文件，并显示在日志面板。"""
        getattr(log, {"warn": "warning"}.get(level, level))(text)
        self._append_log(level, text)

    def _append_log(self, level: str, text: str) -> None:
        entry = (log_timestamp(), level, text)
        self._log_entries.append(entry)  # 全部留着（最多 LOG_LINES 条），换筛选条件时重新显示
        if self._log_visible(entry):
            self.log_view.appendHtml(self._log_html(entry))

    def _log_visible(self, entry: tuple[str, str, str]) -> bool:
        _stamp, level, text = entry
        levels = self.log_level_combo.currentData()
        keyword = self.log_search.text().strip().lower()
        return (levels is None or level in levels) and (not keyword or keyword in text.lower())

    def _log_html(self, entry: tuple[str, str, str]) -> str:
        stamp, level, text = entry
        color = {"warn": self.colors.warn, "error": self.colors.error}.get(level)
        body = html.escape(text)
        if color:
            body = f'<span style="color:{color}">{body}</span>'
        return f'<span style="color:{self.colors.muted}">{stamp}</span>&nbsp;&nbsp;{body}'

    def _render_log(self) -> None:
        """换了筛选条件：按条件重新显示日志。"""
        self.log_view.setUpdatesEnabled(False)
        self.log_view.clear()
        for entry in self._log_entries:
            if self._log_visible(entry):
                self.log_view.appendHtml(self._log_html(entry))  # 一条一个段落，超过上限时才能一条条地挤掉旧的
        self.log_view.setUpdatesEnabled(True)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self.log_view.clear()

    def _notify(self, title: str, text: str) -> None:
        if self.tray and self.cfg.notify:
            self.tray.showMessage(title, text, make_app_icon(), 6000)

    def _remind(self, title: str, text: str) -> None:
        """“只提醒”直播间的开播 / 下播提醒：这些直播间就是为了提醒才加的，不看“系统通知”开关。"""
        if self.tray:
            self.tray.showMessage(title, text, make_app_icon(), 10000)

    def _confirm(self, title: str, text: str, yes_text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.PlainText)  # 里面有主播昵称、备份文件里写的信息
        box.setText(text)
        yes = box.addButton(yes_text, QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    # ================================================================ 窗口与退出

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 第一次打开窗口时弹一次“使用须知”（开机自启缩在托盘时不弹，等第一次打开窗口）
        if not self.cfg.notice_shown and not self._notice_pending:
            self._notice_pending = True
            QTimer.singleShot(300, self._show_notice_once)

    def _show_notice_once(self) -> None:
        show_notice(self)
        self.cfg.notice_shown = True
        self._save_config()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def closeEvent(self, event) -> None:
        if self._quitting:
            event.accept()
            return
        action = self.cfg.close_action if self.tray is not None else CLOSE_QUIT  # 没有托盘就只能退出
        if action == CLOSE_ASK:
            action = self._ask_close_action()
            if action is None:  # 点了取消
                event.ignore()
                return
        if action == CLOSE_TRAY:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    APP_TITLE,
                    "程序仍在运行中，已最小化到托盘",
                    make_app_icon(),
                    6000,
                )
            return
        event.accept()
        self.quit_app()

    def _ask_close_action(self) -> str | None:
        """问用户是缩到托盘还是退出。勾了“记住”就写进设置，以后不再问。"""
        box = QMessageBox(self)
        box.setWindowTitle("关闭窗口")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要最小化到托盘继续运行，还是退出程序？")
        box.setInformativeText("退出后不会再检测开播，也不会发送弹幕。")
        tray_btn = box.addButton("最小化到托盘", QMessageBox.ButtonRole.AcceptRole)
        quit_btn = box.addButton("退出程序", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        remember = QCheckBox("记住我的选择，不再询问（可以在设置里改）")
        box.setCheckBox(remember)
        box.exec()
        clicked = box.clickedButton()
        choice = CLOSE_TRAY if clicked is tray_btn else CLOSE_QUIT if clicked is quit_btn else None
        if choice and remember.isChecked():
            self.cfg.close_action = choice
            self._save_config()
        return choice

    # ------------------------------------------------------------ 数据备份和恢复

    def _export_backup(self) -> None:
        documents = Path.home() / "Documents"
        folder = documents if documents.exists() else Path.home()
        path, _ = QFileDialog.getSaveFileName(self, "导出备份", str(folder / backup.default_name()), "压缩包 (*.zip)")
        if not path:
            return

        def on_done(info: backup.BackupInfo) -> None:
            self._log("info", f"已导出备份：{path}（{len(info.files)} 个文件）")
            QMessageBox.information(self, "导出备份", f"已导出到：\n{path}\n\n共 {len(info.files)} 个文件。换电脑时在新电脑上“从备份恢复”就行。")

        run_async(self, backup.export_backup, Path(path), on_done=on_done,
                  on_error=lambda message: QMessageBox.warning(self, "导出备份", message))

    def _restore_backup(self) -> None:
        documents = Path.home() / "Documents"
        path, _ = QFileDialog.getOpenFileName(
            self, "选择备份", str(documents if documents.exists() else Path.home()), "压缩包 (*.zip)"
        )
        if not path:
            return
        try:
            info = backup.read_backup(Path(path))
        except backup.BackupError as e:
            QMessageBox.warning(self, "从备份恢复", str(e))
            return
        made = f"{info.created} 导出" if info.created else "导出时间不明"
        question = (
            f"恢复这个备份（{made}，版本 {info.version or '不明'}，{len(info.files)} 个文件）？\n\n"
            "现在的直播间和设置、收藏夹、历史记录、说书文字和进度会被替换成备份里的。"
            f"替换前会自动把现在的数据另存到数据文件夹里的“{backup.PRE_RESTORE_PREFIX}时间.zip”。\n\n"
            "程序会关闭并自动重新打开。"
        )
        if not self._confirm("从备份恢复", question, "恢复"):
            return
        try:
            backup.stage_restore(Path(path))
        except backup.BackupError as e:
            QMessageBox.warning(self, "从备份恢复", str(e))
            return
        self._log("info", f"准备恢复备份：{path}，程序重新打开时换上")
        try:
            system.relaunch(["--wait-pid", str(os.getpid())])
        except OSError as e:
            QMessageBox.information(self, "从备份恢复", f"没能自动重新打开程序（{e}）。\n请稍后自己打开程序，打开时会自动恢复。")
        self.quit_app("恢复备份")

    def show_restore_result(self, result: backup.RestoreResult) -> None:
        """恢复备份后重新打开时调用：告诉用户恢复得怎么样。"""
        previous = f"\n\n恢复前的数据另存在：\n{result.previous}" if result.previous else ""
        if result.ok:
            QMessageBox.information(self, "从备份恢复", result.message + previous)
        else:
            QMessageBox.warning(self, "从备份恢复", result.message + previous)

    # ------------------------------------------------------------ 从压缩包更新

    def _update_from_zip(self) -> None:
        downloads = Path.home() / "Downloads"
        path, _ = QFileDialog.getOpenFileName(
            self, "选择新版本的压缩包", str(downloads if downloads.exists() else Path.home()), "压缩包 (*.zip)"
        )
        if not path:
            return
        progress = QProgressDialog("正在解压新版本……", "", 0, 0, self)
        progress.setWindowTitle(APP_TITLE)
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        target = updater.app_dir()

        def on_done(staged: updater.StagedUpdate) -> None:
            progress.close()
            self._confirm_update(staged, target)

        def on_error(message: str) -> None:
            progress.close()
            updater.discard(target)
            QMessageBox.warning(self, "没法更新", message)

        run_async(self, updater.prepare_from_zip, Path(path), target, on_done=on_done, on_error=on_error)

    def _confirm_update(self, staged: updater.StagedUpdate, target: Path) -> None:
        if updater.version_tuple(staged.version) <= updater.version_tuple(APP_VERSION):
            same = staged.version == APP_VERSION
            question = f"压缩包里是 {staged.version}，{'和现在的版本一样' if same else '比现在的 ' + APP_VERSION + ' 还旧'}，仍要替换吗？"
        else:
            question = f"从 {APP_VERSION} 更新到 {staged.version}？"
        sending = any(view and view.session is Session.SENDING for view in (self.engine.view(r.room_id) for r in self.cfg.rooms))
        detail = "程序会关闭并自动重新打开，直播间、收藏夹、历史记录等数据不受影响。"
        if sending:
            detail += "\n正在发送的直播间会中断十几秒，重新打开后照常处理。"
        answer = QMessageBox.question(self, "从压缩包更新", f"{question}\n\n{detail}")
        if answer != QMessageBox.StandardButton.Yes:
            updater.discard(target)
            return
        try:
            updater.launch_install(staged, target)
        except OSError as e:
            updater.discard(target)
            QMessageBox.warning(self, "没法更新", f"启动新版本失败：{e}")
            return
        self.quit_app("程序更新")

    def after_update(self, old_version: str) -> None:
        """更新完重新打开后调用：删掉临时文件，告诉用户已经更新。"""
        self._log("info", f"已从 {old_version} 更新到 {APP_VERSION}")
        self._notify("更新完成", f"已从 {old_version} 更新到 {APP_VERSION}")

        def cleanup(attempts_left: int = 5) -> None:
            # 刚才负责替换文件的新版进程可能还没完全退出，删不掉就过几秒再试
            if not updater.cleanup(updater.app_dir()) and attempts_left > 1:
                QTimer.singleShot(5000, lambda: cleanup(attempts_left - 1))

        QTimer.singleShot(3000, cleanup)

    def quit_app(self, reason: str = "程序退出") -> None:
        self._quitting = True
        self.engine.stop()
        self.engine.close_records(reason)  # 还在发送的直播间，历史记录记到这一刻为止
        self.favorites.close()
        self.history.close()
        if self.tray:
            self.tray.hide()
        QApplication.quit()


# ==================================================================== 启动

SINGLE_INSTANCE_KEY = f"{APP_ID}-single-instance"


class _TimezoneFormatter(logging.Formatter):
    """日志文件里的时间也带上日期和时区，和界面上的运行日志一致。"""

    def formatTime(self, record, datefmt=None) -> str:
        return log_timestamp(record.created)


def _setup_logging() -> None:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(_TimezoneFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    def excepthook(exc_type, exc, tb):
        logging.getLogger("crash").error("未处理的异常", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook


def _install_qt_translation(app: QApplication) -> bool:
    """让 Qt 自带的文字（比如输入框右键菜单）显示中文。"""
    translator = QTranslator(app)
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not translator.load(QLocale(QLocale.Language.Chinese, QLocale.Country.China), "qtbase", "_", path):
        return False
    app.installTranslator(translator)
    return True


def run(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setWindowIcon(make_app_icon())
    app.setQuitOnLastWindowClosed(False)  # 窗口缩到托盘时程序不能退出
    translated = _install_qt_translation(app)

    if "--wait-pid" in argv:  # 恢复备份后重新打开：等旧程序完全退出，不然会被当成“已经在运行”
        index = argv.index("--wait-pid")
        try:
            updater.wait_for_exit(int(argv[index + 1]), timeout_ms=30_000)
        except (IndexError, ValueError):
            pass

    # 同一时间只允许运行一个，否则两个程序会重复发弹幕。再打开一次就把已有的窗口叫出来。
    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(500):
        probe.write(b"show")
        probe.waitForBytesWritten(500)
        probe.disconnectFromServer()
        return 0
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)

    _setup_logging()
    log.info("%s %s 启动，数据文件夹：%s", APP_TITLE, APP_VERSION, data_dir())
    if config.data_dir_note:
        log.warning(config.data_dir_note)
    if not translated:
        log.warning("没找到 Qt 的中文翻译，右键菜单等会显示英文")
    try:
        system.refresh_autostart()
    except OSError:
        pass

    restored = backup.apply_pending_restore()  # 要在打开数据库（创建 MainWindow）之前
    if restored is not None:
        (log.info if restored.ok else log.warning)("从备份恢复：%s 恢复前的数据：%s", restored.message, restored.previous)

    window = MainWindow()

    def on_new_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.disconnected.connect(connection.deleteLater)
        window.show_window()

    server.newConnection.connect(on_new_connection)

    if "--updated-from" in argv:
        index = argv.index("--updated-from")
        window.after_update(argv[index + 1] if index + 1 < len(argv) else "旧版本")

    if "--minimized" in argv and window.tray is not None and restored is None:
        if window.creds is None:
            window._notify("还没有登录", "打开程序登录后，开播时才能自动发送弹幕")
    else:
        window.show()
    if restored is not None:
        QTimer.singleShot(800, lambda: window.show_restore_result(restored))
    return app.exec()
