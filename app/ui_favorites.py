"""弹幕收藏夹：收藏夹页面、编辑对话框、从收藏夹挑选弹幕的对话框。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .favorites import Favorite, FavoritesStore, FavoriteSummary, count_chars
from .story import read_text_file
from .ui_common import NoFocusDelegate, exact_chars, format_chars, hint_label, save_cancel_buttons
from .ui_tags import TagEditor

# 预览区只显示开头这么多字，整本小说塞进预览框会很慢，也没必要
PREVIEW_LIMIT = 5000
TXT_FILTER = "文本文件 (*.txt);;所有文件 (*)"


def import_txt(parent: QWidget) -> tuple[str, str] | None:
    """选一个 txt 文件读进来，返回（文件名作标题, 内容）。取消或失败返回 None。"""
    path, _ = QFileDialog.getOpenFileName(parent, "导入 txt 文件", "", TXT_FILTER)
    if not path:
        return None
    try:
        return Path(path).stem, read_text_file(path)
    except (OSError, ValueError) as e:
        QMessageBox.warning(parent, "导入失败", str(e))
        return None


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-size: 11pt; font-weight: bold;")
    return label


# ---------------------------------------------------------------- 列表（页面和挑选对话框共用）


class FavoritesBrowser(QWidget):
    """检索框 + 标签筛选 + 收藏列表 + 内容预览。"""

    selection_changed = Signal()
    activated = Signal(int)  # 双击某条收藏

    COLUMNS = ["标题", "标签", "字数", "内容预览"]

    def __init__(self, store: FavoritesStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self._rows: list[FavoriteSummary] = []

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("检索标题、内容或标签")
        self.search_edit.setClearButtonEnabled(True)
        self.tag_combo = QComboBox()
        self.tag_combo.setMinimumWidth(140)
        self.count_label = hint_label()
        top = QHBoxLayout()
        top.addWidget(self.search_edit, 1)
        top.addWidget(QLabel("标签："))
        top.addWidget(self.tag_combo)
        top.addWidget(self.count_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 150)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选中一条收藏，这里显示它的内容")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.table)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)

        # 打字时不要每敲一个字就查一次，停顿一下再查
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.refresh)
        self.search_edit.textChanged.connect(self._search_timer.start)
        self.tag_combo.currentIndexChanged.connect(lambda _index: self.refresh())
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)

        self.refresh()

    def refresh(self, select_id: int | None = None) -> None:
        keep = select_id if select_id is not None else self.selected_id()
        self.table.clearSelection()  # 换了列表内容，旧的选中行不能留着，不然选中的和预览对不上
        self._reload_tags()
        self._rows = self.store.search(self.search_edit.text(), self.tag_combo.currentData() or "")
        self.table.setRowCount(len(self._rows))
        for row, item in enumerate(self._rows):
            values = [item.title, "、".join(item.tags), format_chars(item.chars), item.preview]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value if column != 3 else "")
                self.table.setItem(row, column, cell)
        self.count_label.setText(f"共 {len(self._rows)} 条")
        for row, item in enumerate(self._rows):
            if item.id == keep:
                self.table.selectRow(row)
                break
        else:
            self.preview.clear()
            self.selection_changed.emit()

    def _reload_tags(self) -> None:
        current = self.tag_combo.currentData() or ""
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("全部标签", "")
        for tag in self.store.used_tags():
            self.tag_combo.addItem(tag, tag)
        index = self.tag_combo.findData(current)
        self.tag_combo.setCurrentIndex(index if index >= 0 else 0)
        self.tag_combo.blockSignals(False)

    def selected_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._rows):
            return None
        return self._rows[rows[0].row()].id

    def _on_selection_changed(self) -> None:
        favorite_id = self.selected_id()
        favorite = self.store.get(favorite_id) if favorite_id is not None else None
        if favorite is None:
            self.preview.clear()
        else:
            text = favorite.content
            if len(text) > PREVIEW_LIMIT:
                text = text[:PREVIEW_LIMIT] + f"\n\n……（共 {format_chars(len(favorite.content))}，这里只显示前 {PREVIEW_LIMIT} 字）"
            self.preview.setPlainText(text)
        self.selection_changed.emit()

    def _on_double_clicked(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._rows):
            self.activated.emit(self._rows[row].id)


# ---------------------------------------------------------------- 新建 / 编辑一条收藏


class FavoriteEditorDialog(QDialog):
    def __init__(self, parent: QWidget | None, store: FavoritesStore, favorite: Favorite | None = None,
                 title: str = "", content: str = ""):
        super().__init__(parent)
        self.setWindowTitle("编辑收藏" if favorite else "新建收藏")
        self.resize(720, 560)
        self.store = store
        self._favorite = favorite
        self.saved_id: int | None = None

        self.title_edit = QLineEdit(favorite.title if favorite else title)
        self.title_edit.setPlaceholderText("给这条收藏起个标题，不填就用内容的开头")
        self.tag_editor = TagEditor(store, favorite.tags if favorite else [])

        self.content_edit = QPlainTextEdit()
        self.content_edit.setPlaceholderText("弹幕内容，不限字数。可以是一句话，也可以是一整段文字或小说")
        self.content_edit.setPlainText(favorite.content if favorite else content)
        self.count_label = hint_label()
        import_btn = QPushButton("从 txt 导入")
        import_btn.setAutoDefault(False)
        import_btn.clicked.connect(self._import)
        content_bar = QHBoxLayout()
        content_bar.addWidget(self.count_label, 1)
        content_bar.addWidget(import_btn)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("标题：", self.title_edit)
        form.addRow("标签：", self.tag_editor)

        self.buttons = save_cancel_buttons(self)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("内容："))
        layout.addWidget(self.content_edit, 1)
        layout.addLayout(content_bar)
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
        if self.content_edit.document().characterCount() > 1 and not _confirm_replace(self):
            return
        self.content_edit.setPlainText(text)
        if not self.title_edit.text().strip():
            self.title_edit.setText(name)

    def accept(self) -> None:
        content = self.content_edit.toPlainText()
        if not content.strip():
            QMessageBox.warning(self, "不能保存", "内容是空的")
            return
        title = self.title_edit.text().strip() or " ".join(content.split())[:20]
        tags = self.tag_editor.tags()
        if self._favorite:
            self.store.update(self._favorite.id, title, tags, content)
            self.saved_id = self._favorite.id
        else:
            self.saved_id = self.store.add(title, tags, content)
        super().accept()


def _confirm_replace(parent: QWidget) -> bool:
    answer = QMessageBox.question(parent, "替换内容", "导入的文件会替换掉现在的内容，确定吗？")
    return answer == QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------- 收藏夹页面


class FavoritesPage(QWidget):
    def __init__(self, store: FavoritesStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.browser = FavoritesBrowser(store)

        self.new_btn = QPushButton("＋ 新建")
        self.new_btn.clicked.connect(lambda: self._new())  # clicked 会带一个参数，不能直接连到 _new
        self.import_btn = QPushButton("导入 txt")
        self.import_btn.clicked.connect(self._import)
        self.edit_btn = QPushButton("编辑")
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.clicked.connect(self._delete)

        bar = QHBoxLayout()
        bar.addWidget(_section_title("弹幕收藏夹"))
        bar.addSpacing(8)
        for button in (self.new_btn, self.import_btn, self.edit_btn, self.delete_btn):
            bar.addWidget(button)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(bar)
        layout.addWidget(hint_label("每条收藏有自己的标题（小类），可以贴多个标签（大类）。收藏不限字数，长文字可以用在说书人模式。"))
        layout.addWidget(self.browser, 1)

        self.browser.selection_changed.connect(self._update_buttons)
        self.browser.activated.connect(lambda _id: self._edit())
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.browser.selected_id() is not None
        self.edit_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)

    def _new(self, title: str = "", content: str = "") -> None:
        dialog = FavoriteEditorDialog(self, self.store, title=title, content=content)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.browser.refresh(select_id=dialog.saved_id)

    def _import(self) -> None:
        result = import_txt(self)
        if result is not None:
            self._new(*result)

    def _edit(self) -> None:
        favorite_id = self.browser.selected_id()
        favorite = self.store.get(favorite_id) if favorite_id is not None else None
        if favorite is None:
            return
        dialog = FavoriteEditorDialog(self, self.store, favorite)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.browser.refresh(select_id=favorite.id)

    def _delete(self) -> None:
        favorite_id = self.browser.selected_id()
        favorite = self.store.get(favorite_id) if favorite_id is not None else None
        if favorite is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("删除收藏")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"确定要删除「{favorite.title}」吗？")
        box.setInformativeText("已经用到直播间里的内容不受影响。")
        yes = box.addButton("删除", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.store.delete(favorite.id)
            self.browser.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.browser.refresh()  # 别处（比如直播间里“同时存入收藏夹”）可能加了新收藏


# ---------------------------------------------------------------- 从收藏夹挑选


class FavoritePickerDialog(QDialog):
    """挑一条收藏。max_chars 不为 None 时，超过这个字数的不能选（独轮车模式一条最多 40 字）。"""

    def __init__(self, parent: QWidget | None, store: FavoritesStore, max_chars: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("从收藏夹选择")
        self.resize(820, 560)
        self.store = store
        self.max_chars = max_chars
        self.result_favorite: Favorite | None = None

        self.browser = FavoritesBrowser(store)
        self.buttons = save_cancel_buttons(self, "使用这条")
        layout = QVBoxLayout(self)
        if max_chars is not None:
            layout.addWidget(hint_label(f"独轮车模式一条弹幕最多 {max_chars} 字；更长的收藏请在说书人模式里使用。"))
        layout.addWidget(self.browser, 1)
        layout.addWidget(self.buttons)

        self.browser.selection_changed.connect(self._update_button)
        self.browser.activated.connect(lambda _id: self.accept())
        self._update_button()

    def _update_button(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(self.browser.selected_id() is not None)

    def accept(self) -> None:
        favorite_id = self.browser.selected_id()
        favorite = self.store.get(favorite_id) if favorite_id is not None else None
        if favorite is None:
            return
        if self.max_chars is not None and len(favorite.content.strip()) > self.max_chars:
            QMessageBox.information(
                self, "太长了", f"这条收藏有 {len(favorite.content.strip())} 字，独轮车模式一条最多 {self.max_chars} 字。"
            )
            return
        self.result_favorite = favorite
        super().accept()
