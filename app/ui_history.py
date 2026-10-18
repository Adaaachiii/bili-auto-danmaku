"""历史记录页面：每次发送的统计，选中一条后在下面列出这次所有失败的弹幕。"""

from __future__ import annotations

import os
import time
from collections import Counter

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import MODE_STORY
from .history import FILTERED_CSV_NAME, HistoryStore, SessionRecord
from .ui_common import NoFocusDelegate, StatusColors, hint_label, utc_offset_label

REFRESH_MS = 5000  # 页面开着时，进行中的记录每隔这么久刷新一次


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


def _time_text(timestamp: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return time.strftime(fmt, time.localtime(timestamp))


def _table(columns: list[str], multi: bool = False) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection if multi else QAbstractItemView.SelectionMode.SingleSelection
    )
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(30)
    table.setItemDelegate(NoFocusDelegate(table))
    return table


def _item(text: str, align_right: bool = False, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if align_right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tip:
        item.setToolTip(tip)
    return item


class HistoryPage(QWidget):
    SESSION_COLUMNS = ["主播", "模式", "内容", "开始时间", "结束时间", "时长", "发送", "成功", "失败", "结束原因"]
    FAILURE_COLUMNS = ["时间", "失败原因", "弹幕内容"]

    def __init__(self, store: HistoryStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.colors = StatusColors()
        self._records: list[SessionRecord] = []

        title = QLabel("历史记录")
        title.setStyleSheet("font-size: 11pt; font-weight: bold;")
        self.csv_btn = QPushButton("打开屏蔽词整理表")
        self.csv_btn.setToolTip(f"所有被 B 站过滤的弹幕，同一直播间同样的内容合并成一行（data\\{FILTERED_CSV_NAME}）")
        self.csv_btn.clicked.connect(self._open_csv)
        self.delete_btn = QPushButton("删除记录")
        self.delete_btn.clicked.connect(self._delete)
        bar = QHBoxLayout()
        bar.addWidget(title)
        bar.addSpacing(8)
        bar.addWidget(self.csv_btn)
        bar.addWidget(self.delete_btn)
        bar.addStretch(1)

        self.room_combo = QComboBox()
        self.room_combo.setMinimumWidth(220)
        self.room_combo.currentIndexChanged.connect(lambda _index: self.refresh())
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("直播间："))
        filter_row.addWidget(self.room_combo)
        filter_row.addSpacing(12)
        filter_row.addWidget(hint_label("每次从开始发弹幕到停止发弹幕记一条，发送不到 5 分钟的不保存"), 1)

        self.sessions = _table(self.SESSION_COLUMNS, multi=True)
        header = self.sessions.horizontalHeader()
        for column in range(len(self.SESSION_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.sessions.setHorizontalHeaderItem(3, QTableWidgetItem(f"开始时间（{utc_offset_label()}）"))
        self.sessions.itemSelectionChanged.connect(self._show_detail)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.failures = _table(self.FAILURE_COLUMNS, multi=True)
        header = self.failures.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.failures.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.failures.customContextMenuRequested.connect(self._failure_menu)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 6, 0, 0)
        detail_layout.setSpacing(6)
        detail_layout.addWidget(self.summary)
        detail_layout.addWidget(self.failures, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.sessions)
        splitter.addWidget(detail)
        splitter.setSizes([320, 300])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(bar)
        layout.addLayout(filter_row)
        layout.addWidget(splitter, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------ 刷新

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def refresh_if_visible(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        self._reload_rooms()
        selected = set(self._selected_ids())
        room_id = self.room_combo.currentData()
        self._records = self.store.sessions(room_id)
        now = time.time()

        table = self.sessions
        table.blockSignals(True)
        table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            story_mode = record.mode == MODE_STORY
            cells = [
                _item(record.uname or f"房间 {record.room_id}", tip=f"房间号 {record.room_id}"),
                _item("说书" if story_mode else "独轮车"),
                _item(f"《{record.content}》" if story_mode and record.content else record.content, tip=record.content),
                _item(_time_text(record.started_at), tip=_time_text(record.started_at, "%Y-%m-%d %H:%M:%S")),
                _item("进行中" if record.ongoing else _time_text(record.ended_at),
                      tip="" if record.ongoing else _time_text(record.ended_at, "%Y-%m-%d %H:%M:%S")),
                _item(format_duration(record.duration(now))),
                _item(str(record.sent), align_right=True),
                _item(str(record.ok), align_right=True),
                _item(str(record.failed), align_right=True),
                _item("" if record.ongoing else record.end_reason, tip=record.end_reason),
            ]
            if record.ongoing:
                cells[4].setForeground(QColor(self.colors.ok))
            if record.failed:
                cells[8].setForeground(QColor(self.colors.error))
            for column, cell in enumerate(cells):
                cell.setData(Qt.ItemDataRole.UserRole, record.id)
                table.setItem(row, column, cell)
        table.clearSelection()
        for row, record in enumerate(self._records):
            if record.id in selected:
                table.selectRow(row)
        table.blockSignals(False)
        self._show_detail()

    def _reload_rooms(self) -> None:
        current = self.room_combo.currentData()
        rooms = self.store.rooms()
        wanted = [None] + [room_id for room_id, _ in rooms]
        existing = [self.room_combo.itemData(i) for i in range(self.room_combo.count())]
        if wanted == existing:
            return
        self.room_combo.blockSignals(True)
        self.room_combo.clear()
        self.room_combo.addItem("全部直播间", None)
        for room_id, uname in rooms:
            self.room_combo.addItem(f"{uname or '房间'}（{room_id}）", room_id)
        index = self.room_combo.findData(current)
        self.room_combo.setCurrentIndex(max(0, index))
        self.room_combo.blockSignals(False)

    # ------------------------------------------------------------ 详情

    def _selected_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.sessions.selectionModel().selectedRows()})
        return [self.sessions.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in rows]

    def _show_detail(self) -> None:
        ids = self._selected_ids()
        self.delete_btn.setEnabled(any(not r.ongoing for r in self._records if r.id in ids))
        self.failures.setRowCount(0)
        if not self._records:
            self.summary.setText("还没有记录。发送超过 5 分钟后，停止发送时会在这里记一条。")
            return
        if len(ids) != 1:
            self.summary.setText("选中一条记录，这里会列出那次所有失败的弹幕。" if not ids else f"选中了 {len(ids)} 条记录。")
            return
        record = next((r for r in self._records if r.id == ids[0]), None)
        if record is None:
            return
        failures = self.store.failures(record.id)
        reasons = Counter(f.category for f in failures)
        reason_text = "，".join(f"{category} {n} 条" for category, n in reasons.most_common())
        end = "还在发送" if record.ongoing else f"{_time_text(record.ended_at, '%Y-%m-%d %H:%M:%S')}（{record.end_reason}）"
        self.summary.setText(
            f"<b>{record.uname}</b>（房间 {record.room_id}）　"
            f"{_time_text(record.started_at, '%Y-%m-%d %H:%M:%S')} 开始，{end}，共 {format_duration(record.duration())}<br>"
            f"发送 {record.sent} 条，成功 {record.ok} 条，失败 {record.failed} 条"
            + (f"：{reason_text}" if reason_text else "")
        )
        self.failures.setRowCount(len(failures))
        for row, failure in enumerate(failures):
            self.failures.setItem(row, 0, _item(_time_text(failure.at, "%m-%d %H:%M:%S")))
            self.failures.setItem(row, 1, _item(failure.reason))
            self.failures.setItem(row, 2, _item(failure.text, tip=failure.text))

    def _failure_menu(self, pos) -> None:
        rows = sorted({index.row() for index in self.failures.selectionModel().selectedRows()})
        if not rows:
            return
        menu = QMenu(self)
        copy = menu.addAction(f"复制选中的 {len(rows)} 条弹幕" if len(rows) > 1 else "复制这条弹幕")
        if menu.exec(self.failures.viewport().mapToGlobal(pos)) is copy:
            QGuiApplication.clipboard().setText("\n".join(self.failures.item(row, 2).text() for row in rows))

    # ------------------------------------------------------------ 按钮

    def _open_csv(self) -> None:
        if self.store.filtered_count() == 0:
            QMessageBox.information(self, "屏蔽词整理表", "还没有被 B 站过滤的弹幕。")
            return
        try:
            path = self.store.write_filtered_csv()
        except PermissionError:
            QMessageBox.warning(self, "屏蔽词整理表", "整理表正被其他程序（比如 Excel）打开着，先把它关掉再点。")
            return
        except OSError as e:
            QMessageBox.warning(self, "屏蔽词整理表", f"写入整理表失败：{e}")
            return
        os.startfile(path)

    def _delete(self) -> None:
        ids = [r.id for r in self._records if r.id in set(self._selected_ids()) and not r.ongoing]
        if not ids:
            return
        answer = QMessageBox.question(self, "删除记录", f"删除选中的 {len(ids)} 条记录？删除后不能恢复。\n（屏蔽词整理表不受影响）")
        if answer == QMessageBox.StandardButton.Yes:
            self.store.delete_sessions(ids)
            self.refresh()
