"""独轮车用的表情：编辑直播间窗口里那一排选好的表情，以及“选择表情”窗口。"""

from __future__ import annotations

from PySide6.QtCore import QObject, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import bili_api, emoticons
from .bili_api import Credentials, EmoticonPackage
from .config import MAX_EMOTICONS
from .ui_common import StatusColors, hint_label, plain_tip, run_async, save_cancel_buttons, set_colored

ROLE_EMOTICON = Qt.ItemDataRole.UserRole  # 表情列表项上存的 {"unique": 表情 ID, "name": 名字, "url": 图片地址}
ROLE_COVER = Qt.ItemDataRole.UserRole + 1  # 表情包列表项上存的封面地址
GRID_ICON = QSize(88, 64)  # 选择窗口里每个表情的大小（通用表情是 200×60 的横条，装扮表情是正方形）
STRIP_ICON = QSize(40, 32)
DOWNLOAD_THREADS = 6


class _Images(QObject):
    """表情图片：后台下载（有缓存读缓存），下好了发 ready(url)。同一张只下载一次，所有窗口共用。"""

    ready = Signal(str)

    def __init__(self):
        super().__init__()
        self._pixmaps: dict[str, QPixmap] = {}
        self._loading: set[str] = set()
        self._failed: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(DOWNLOAD_THREADS)

    def get(self, url: str) -> QPixmap | None:
        """已经有了就返回；还没有就开始下载并返回 None，下好后发 ready(url)。"""
        pixmap = self._pixmaps.get(url)
        if pixmap is None and url and url not in self._loading and url not in self._failed:
            self._loading.add(url)
            run_async(
                self,
                emoticons.load_image,
                url,
                on_done=lambda data: self._loaded(url, data),
                on_error=lambda _message: self._give_up(url),
                pool=self._pool,
            )
        return pixmap

    def retry_failed(self) -> None:
        """上次没下载下来的（比如断网了），下次用到时再试。"""
        self._failed.clear()

    def _loaded(self, url: str, data: bytes) -> None:
        self._loading.discard(url)
        image = QPixmap()
        if not image.loadFromData(data):
            self._failed.add(url)
            return
        # 只留缩小后的图：一千多个表情的原图都放在内存里太占地方
        screen = QGuiApplication.primaryScreen()
        ratio = screen.devicePixelRatio() if screen else 1.0
        box = QSize(round(GRID_ICON.width() * ratio), round(GRID_ICON.height() * ratio))
        if image.width() > box.width() or image.height() > box.height():
            image = image.scaled(box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        image.setDevicePixelRatio(ratio)
        self._pixmaps[url] = image
        self.ready.emit(url)

    def _give_up(self, url: str) -> None:
        self._loading.discard(url)
        self._failed.add(url)


_images: _Images | None = None


def images() -> _Images:
    global _images
    if _images is None:
        _images = _Images()
    return _images


def _blank(size: QSize) -> QIcon:
    """图片还没下载好时先占住位置，免得图片到了以后列表跳一下。"""
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent)
    return QIcon(pixmap)


def _icon_for(url: str, size: QSize) -> QIcon:
    pixmap = images().get(url)
    return QIcon(pixmap) if pixmap is not None else _blank(size)


def _make_item(emoticon: dict, size: QSize, tooltip: str = "") -> QListWidgetItem:
    item = QListWidgetItem(_icon_for(emoticon["url"], size), emoticon["name"])
    item.setData(ROLE_EMOTICON, emoticon)
    item.setToolTip(plain_tip(tooltip or emoticon["name"]))  # 表情名字有的是主播起的
    return item


def _refresh_icons(widget: QListWidget, url: str, role: Qt.ItemDataRole, size: QSize) -> None:
    for row in range(widget.count()):
        item = widget.item(row)
        data = item.data(role)
        if (data["url"] if isinstance(data, dict) else data) == url:
            item.setIcon(_icon_for(url, size))


# ---------------------------------------------------------------- 选好的表情


class EmoticonStrip(QListWidget):
    """选好的表情，一个挨一个排开，放不下就换行。拖动可以调整顺序，选中后按 Delete 或右键可以移除。"""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSpacing(2)
        self.setIconSize(STRIP_ICON)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.model().rowsMoved.connect(lambda *_: self.changed.emit())
        images().ready.connect(self._on_image)

    def emoticons(self) -> list[dict]:
        return [dict(self.item(row).data(ROLE_EMOTICON)) for row in range(self.count())]

    def uniques(self) -> list[str]:
        return [self.item(row).data(ROLE_EMOTICON)["unique"] for row in range(self.count())]

    def set_emoticons(self, items: list[dict]) -> None:
        self.clear()
        for emoticon in items:
            self._add_item(emoticon)
        self.changed.emit()

    def add(self, emoticon: dict) -> None:
        self._add_item(emoticon)
        self.changed.emit()

    def _add_item(self, emoticon: dict) -> None:
        item = _make_item(emoticon, STRIP_ICON)
        # 每项一样高，横条的通用表情和正方形的装扮表情，名字才对得齐
        width = STRIP_ICON.width() + self.fontMetrics().horizontalAdvance(emoticon["name"]) + 30
        item.setSizeHint(QSize(width, STRIP_ICON.height() + 8))
        self.addItem(item)

    def remove(self, unique: str) -> None:
        for row in reversed(range(self.count())):
            if self.item(row).data(ROLE_EMOTICON)["unique"] == unique:
                self.takeItem(row)
        self.changed.emit()

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self.changed.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selectedItems():
            self.remove_selected()
            return
        super().keyPressEvent(event)

    def _show_menu(self, pos) -> None:
        if self.count() == 0:
            return
        if not self.itemAt(pos):
            self.clearSelection()
        menu = QMenu(self)
        remove = menu.addAction("移除选中的表情")
        remove.setEnabled(bool(self.selectedItems()))
        remove.triggered.connect(self.remove_selected)
        menu.addAction("全部清空").triggered.connect(lambda: self.set_emoticons([]))
        menu.exec(self.viewport().mapToGlobal(pos))

    def _on_image(self, url: str) -> None:
        _refresh_icons(self, url, ROLE_EMOTICON, STRIP_ICON)


# ---------------------------------------------------------------- 选择表情窗口


def _fetch_packages(room_id: int, creds: Credentials) -> list[EmoticonPackage]:
    session = bili_api.new_session()
    bili_api.init_anonymous_cookies(session)
    return bili_api.get_emoticons(session, creds, room_id)


class EmoticonPickerDialog(QDialog):
    """从这个账号在这个直播间能用的表情里挑：点一下选上，再点一下取消。

    选的先后就是发送顺序，下面那一排可以拖动调整。确定后用 result_emoticons 取结果。
    """

    def __init__(self, parent: QWidget | None, room_id: int, creds: Credentials, selected: list[dict]):
        super().__init__(parent)
        self.setWindowTitle("选择表情")
        self.resize(940, 660)
        self._colors = StatusColors()
        self._packages: list[EmoticonPackage] = []
        self._syncing = False  # 程序自己改选中状态时，不当成用户点了
        self.result_emoticons: list[dict] = []

        self.status = hint_label("正在获取这个账号在这个直播间能用的表情…")
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索表情名字")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self._show_emoticons)
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.search)

        self.pkg_list = QListWidget()
        self.pkg_list.setIconSize(QSize(28, 28))
        self.pkg_list.currentRowChanged.connect(self._on_package_changed)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setIconSize(GRID_ICON)
        self.grid.setGridSize(QSize(GRID_ICON.width() + 20, GRID_ICON.height() + 40))
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.grid.itemSelectionChanged.connect(self._on_grid_selection)
        c = self._colors
        # 选中的要一眼看得出来，和鼠标停在上面的样子分开
        self.grid.setStyleSheet(
            "QListWidget::item { border: 2px solid transparent; border-radius: 6px; }"
            f"QListWidget::item:hover {{ background: {c.hover}; }}"
            f"QListWidget::item:selected {{ background: {c.selection}; border-color: palette(highlight); color: palette(text); }}"
        )
        splitter = QSplitter()
        splitter.addWidget(self.pkg_list)
        splitter.addWidget(self.grid)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 650])
        splitter.setChildrenCollapsible(False)

        self.chosen_label = QLabel()
        clear_btn = QPushButton("清空")
        clear_btn.setAutoDefault(False)
        clear_btn.clicked.connect(lambda: self.strip.set_emoticons([]))
        chosen_row = QHBoxLayout()
        chosen_row.addWidget(self.chosen_label)
        chosen_row.addWidget(hint_label("按选的先后轮流发送，拖动可以调整顺序；选中后按 Delete 可以移除"), 1)
        chosen_row.addWidget(clear_btn)
        self.strip = EmoticonStrip()
        self.strip.setFixedHeight(92)
        self.strip.changed.connect(self._on_strip_changed)

        self.buttons = save_cancel_buttons(self, "确定")
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addLayout(chosen_row)
        layout.addWidget(self.strip)
        layout.addWidget(self.buttons)

        images().ready.connect(self._on_image)
        images().retry_failed()
        self.strip.set_emoticons(selected)
        run_async(self, _fetch_packages, room_id, creds, on_done=self._on_loaded, on_error=self._on_failed)

    def accept(self) -> None:
        self.result_emoticons = self.strip.emoticons()
        super().accept()

    # ------------------------------------------------------------ 表情包

    def _on_loaded(self, packages: list[EmoticonPackage]) -> None:
        self._packages = packages
        if not packages:
            set_colored(self.status, "这个直播间没有能用的表情", self._colors.warn)
            return
        usable = sum(item.usable for package in packages for item in package.emoticons)
        locked = sum(not item.usable for package in packages for item in package.emoticons)
        text = f"共 {len(packages)} 个表情包、{usable} 个能用的表情"
        if locked:
            text += f"；灰色的 {locked} 个还没解锁，鼠标停在上面能看到解锁条件"
        self.status.setText(text)
        for package in packages:
            item = QListWidgetItem(_icon_for(package.cover, QSize(28, 28)), f"{package.name}（{len(package.emoticons)}）")
            item.setData(ROLE_COVER, package.cover)
            item.setToolTip(plain_tip(package.name))
            self.pkg_list.addItem(item)
        self.pkg_list.setCurrentRow(0)

    def _on_failed(self, message: str) -> None:
        set_colored(self.status, f"获取表情失败：{message}", self._colors.error)

    def _on_package_changed(self) -> None:
        if self.search.text().strip():
            self.search.clear()  # 会触发 _show_emoticons
        else:
            self._show_emoticons()

    def _visible(self) -> list[tuple[bili_api.Emoticon, str]]:
        """现在要显示的表情和它所在的表情包名字：搜索时在所有表情包里找，否则显示左边选中的表情包。"""
        keyword = self.search.text().strip().lower()
        if keyword:
            return [(item, p.name) for p in self._packages for item in p.emoticons if keyword in item.name.lower()]
        row = self.pkg_list.currentRow()
        if 0 <= row < len(self._packages):
            package = self._packages[row]
            return [(item, package.name) for item in package.emoticons]
        return []

    def _show_emoticons(self) -> None:
        chosen = set(self.strip.uniques())
        self._syncing = True
        self.grid.clear()
        for emoticon, package_name in self._visible():
            data = {"unique": emoticon.unique, "name": emoticon.name, "url": emoticon.url}
            tip = f"{emoticon.name}\n表情包：{package_name}"
            if not emoticon.usable:
                tip += f"\n还不能用：{emoticon.lock_text or '没有解锁'}"
            item = _make_item(data, GRID_ICON, tip)
            if not emoticon.usable:
                item.setFlags(Qt.ItemFlag.NoItemFlags)  # 变灰，点不了
            self.grid.addItem(item)
            item.setSelected(emoticon.usable and emoticon.unique in chosen)
        self._syncing = False
        self.grid.scrollToTop()

    # ------------------------------------------------------------ 选中

    def _on_grid_selection(self) -> None:
        """上面点选了 / 取消了哪个，下面那一排跟着加上 / 去掉。"""
        if self._syncing:
            return
        chosen = self.strip.uniques()
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if not item.flags() & Qt.ItemFlag.ItemIsSelectable:
                continue
            data = item.data(ROLE_EMOTICON)
            if item.isSelected() and data["unique"] not in chosen:
                if len(chosen) >= MAX_EMOTICONS:
                    set_colored(self.status, f"最多选 {MAX_EMOTICONS} 个表情", self._colors.warn)
                    break
                chosen.append(data["unique"])
                self.strip.add(dict(data))
            elif not item.isSelected() and data["unique"] in chosen:
                chosen.remove(data["unique"])
                self.strip.remove(data["unique"])
        self._sync_grid()

    def _on_strip_changed(self) -> None:
        self.chosen_label.setText(f"已选 {self.strip.count()} 个")
        self._sync_grid()

    def _sync_grid(self) -> None:
        """下面那一排移除了的，上面也取消选中（反过来也一样）。"""
        chosen = set(self.strip.uniques())
        self._syncing = True
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                item.setSelected(item.data(ROLE_EMOTICON)["unique"] in chosen)
        self._syncing = False

    def _on_image(self, url: str) -> None:
        _refresh_icons(self.grid, url, ROLE_EMOTICON, GRID_ICON)
        _refresh_icons(self.pkg_list, url, ROLE_COVER, QSize(28, 28))
