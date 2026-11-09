"""添加 / 编辑直播间对话框的布局测试：标题、进度很长时按钮不跑出窗口，宽度固定；三种模式下标签页的显示；
小屏幕（1366×768）上高度放得下、保存按钮看得见；三种模式保存出来的设置和界面上填的一致。

运行：python tests/test_room_dialog_layout.py（不显示窗口，不联网，用临时数据目录）。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 不弹窗口
ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPoint, QRect, QTime  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea  # noqa: E402

from app import bili_api, story, ui_room_dialog  # noqa: E402
from app.config import MODE_NOTIFY, MODE_REPEAT, MODE_STORY, RoomConfig  # noqa: E402
from app.favorites import FavoritesStore  # noqa: E402
from app.story import StoryPart  # noqa: E402
from app.ui_common import plain_tip  # noqa: E402
from app.ui_room_dialog import DIALOG_WIDTH, MODE_TIPS, PART_ROLE, QUEUE_HINT, RoomDialog  # noqa: E402

app = QApplication(sys.argv)


def offline(_room_id):
    raise bili_api.BiliApiError("测试里不联网")


ui_room_dialog._lookup_room = offline
store = FavoritesStore(Path(HOME) / "favorites.db")


def right_edge(dialog, widget) -> int:
    return widget.mapTo(dialog, QPoint(widget.width(), 0)).x()


def check_no_overflow(dialog) -> None:
    """标签页里的内容都放得进去：内容最窄也比可见区域宽的话，右边就会被裁掉（横向滚动条是关掉的）。"""
    for scroll in dialog.findChildren(QScrollArea):
        if not scroll.isVisibleTo(dialog):
            continue
        need = scroll.widget().minimumSizeHint().width()
        assert need <= scroll.viewport().width(), (scroll, need, scroll.viewport().width())


# ---- 说书人模式：3 组、标题很长、进度很长
long_title = "这是一个特别特别长的标题，用来检查发送序列列表和说书进度会不会把窗口撑宽，后面再加一些字凑够五十个字以上吧"
assert len(long_title) >= 50
parts = [StoryPart(f"{long_title}{i}", (str(i) + "一二三四五六七八九") * 300) for i in range(3)]
raw = story.join_parts(parts)
story.save_room_story(1, raw)
text = story.StoryText(raw)
title = story.queue_title(parts)
room = RoomConfig(room_id=1, uid=1, uname="主播", mode=MODE_STORY, story_title=title, story_hash=text.hash, story_chars=text.length)
dialog = RoomDialog(None, room, set(), store, progress=(text.length // 2, text.length))
assert dialog.part_list.count() == 3
assert sum("← 目前在这" in dialog.part_list.item(i).text() for i in range(3)) == 1
assert long_title in dialog.progress_label.text()  # 详细进度放在鼠标提示里，界面上只显示一句短的
assert dialog.progress_short.text().startswith("总进度 5") and dialog.progress_short.toolTip()
assert "3 组" in dialog.story_label.text() and "9,000 字" in dialog.story_label.text(), dialog.story_label.text()

# ---- 发送序列每一组右边的小字：已说完 / ← 目前在这（这一组里的百分比）/ 还没说
marks = dialog._part_marks()
assert marks == ["3,000 字　已说完", "← 目前在这　50%", "3,000 字　还没说"], marks
rows = [dialog.part_list.item(i).data(PART_ROLE) for i in range(3)]
assert [row[2] for row in rows] == marks and [row[3] for row in rows] == [False, True, False], rows
assert rows[1][:2] == (2, f"{long_title}1")
assert dialog.part_list.item(1).text().endswith("← 目前在这")  # item.text() 还是原来的格式
# 刚好说完第一组：第一组已说完，第二组 0%
other = RoomDialog(None, room, set(), store, progress=(text.parts[1][1], text.length))
assert other._part_marks() == ["3,000 字　已说完", "← 目前在这　0%", "3,000 字　还没说"], other._part_marks()
# 进度为 0、内容改过（说过的部分变了）、点了重置：都只写字数
for progress in (None, (0, text.length)):
    plain = RoomDialog(None, room, set(), store, progress=progress)
    assert plain._part_marks() == ["3,000 字"] * 3 and all(not plain.part_list.item(i).data(PART_ROLE)[3] for i in range(3))
other.part_list.setCurrentRow(0)
other._move_part(1)  # 第一组挪到后面：说过的部分变了，从头说
assert other._resume_pos() == 0 and other._part_marks() == ["3,000 字"] * 3
other._move_part(-1)
assert other._part_marks()[1] == "← 目前在这　0%"
other._reset_progress()
assert other._part_marks() == ["3,000 字"] * 3
print("发送序列的进度标记 OK")

# ---- 编辑已有直播间：输入框藏起来但还保存着房间号，显示主播名片
assert not dialog.room_edit.isVisibleTo(dialog) and dialog.room_edit.text() == "1" and dialog.room_edit.isReadOnly()
assert dialog.query_btn.text() == "刷新" and dialog.room_name.text() == "主播" and "房间号 1" in dialog.room_sub.text()
dialog._query_seq += 1
dialog._on_lookup(dialog._query_seq, (bili_api.RoomInfo(1, 0, 1, bili_api.LIVE_ON),
                                     bili_api.LiveStatus(1, 1, "新昵称", "直播标题", bili_api.LIVE_ON)))
assert dialog.room_name.text() == "新昵称" and dialog.room_state.text() == "直播中" and "直播标题" in dialog.room_sub.text()
assert not dialog.room_info.isVisibleTo(dialog)  # 名片上已经有了，结果那一行不重复显示
assert dialog.result_config().uname == "新昵称" and dialog.result_config().room_id == 1
dialog._on_lookup_failed(dialog._query_seq, "查询失败了")
assert dialog.room_info.isVisibleTo(dialog) and dialog.room_info.text() == "查询失败了"  # 出错的红字照样显示
new_room = RoomDialog(None, None, set(), store)
assert new_room.room_edit.isVisibleTo(new_room) and new_room.query_btn.text() == "查询"
print("主播名片 OK")
dialog.show()
app.processEvents()
assert dialog.width() <= DIALOG_WIDTH, dialog.width()
assert dialog.tabs.currentIndex() == 0 and dialog.tabs.tabText(0) == "说书内容"
dialog.part_list.setCurrentRow(1)
app.processEvents()
buttons = [dialog.part_edit_btn, dialog.part_up_btn, dialog.part_down_btn, dialog.part_remove_btn,
           *dialog.story_add_buttons, dialog.reset_btn]
for button in buttons:
    assert button.isVisibleTo(dialog), button.text()
    assert right_edge(dialog, button) <= dialog.width(), (button.text(), right_edge(dialog, button), dialog.width())
    # 也在标签页的可见区域里（不是被裁到右边外面）
    viewport = dialog.tabs.currentWidget().viewport()
    assert button.mapTo(viewport, QPoint(button.width(), 0)).x() <= viewport.width(), button.text()
# 四个按钮横排在选中的那一行里面（列表的第 2 行），不在列表外面
ys = {dialog.part_edit_btn.mapTo(dialog, QPoint()).y() == b.mapTo(dialog, QPoint()).y() for b in buttons[:4]}
assert ys == {True}
row_rect = dialog.part_list.visualItemRect(dialog.part_list.item(1))
bar = dialog.part_actions
assert bar.isVisibleTo(dialog) and row_rect.contains(bar.geometry()), (row_rect, bar.geometry())
dialog.part_list.setCurrentRow(2)
app.processEvents()
assert dialog.part_list.visualItemRect(dialog.part_list.item(2)).contains(bar.geometry())  # 换一行选，按钮跟着走
dialog.part_list.clearSelection()
app.processEvents()
assert not bar.isVisibleTo(dialog)  # 没选中时不显示
dialog.part_list.setCurrentRow(1)
app.processEvents()
assert all(b.isEnabled() for b in buttons[:4])
check_no_overflow(dialog)
assert any(QUEUE_HINT == label.text() for label in dialog.findChildren(ui_room_dialog.QLabel))
width_before = dialog.width()
dialog.estimate_label.setText("很长的预计文字" * 30)  # 再长也不能撑宽窗口
dialog.room_info.setText("很长的直播标题" * 30)
app.processEvents()
assert dialog.width() == width_before <= DIALOG_WIDTH
check_no_overflow(dialog)
print("长标题、长进度 OK")

# ---- 切换标签页、切换模式时窗口大小不变
dialog._fit_to_content()  # 上面改了文字，按现在的内容重新定一次大小
app.processEvents()
size = dialog.size()
for index in (1, 2, 0):
    dialog.tabs.setCurrentIndex(index)
    app.processEvents()
    assert dialog.size() == size, (index, dialog.size(), size)
print("切换标签页 OK")

# ---- 三种模式下标签页的显示
def visible_tabs(d) -> list[str]:
    return [d.tabs.tabText(i) for i in range(d.tabs.count()) if d.tabs.isTabVisible(i)]


def selected_cards(d) -> list[str]:
    return [card.radio.text() for card in d._mode_cards if card.property("selected")]


assert selected_cards(dialog) == ["说书人模式"] and dialog.mode_hint.text() == MODE_TIPS[MODE_STORY]
assert [card.toolTip() for card in dialog._mode_cards] == [plain_tip(MODE_TIPS[m]) for m in (MODE_REPEAT, MODE_STORY, MODE_NOTIFY)]
dialog.repeat_radio.setChecked(True)
app.processEvents()
assert selected_cards(dialog) == ["独轮车模式"] and dialog.mode_hint.text() == MODE_TIPS[MODE_REPEAT]
assert visible_tabs(dialog) == ["弹幕内容", "间隔与限制", "其他"] and dialog.tabs.currentIndex() == 0
assert dialog.repeat_page.isVisibleTo(dialog) and not dialog.story_page.isVisibleTo(dialog)
dialog.tabs.setCurrentIndex(2)
assert dialog.offline_check.isVisibleTo(dialog)
dialog.notify_radio.setChecked(True)
app.processEvents()
assert selected_cards(dialog) == ["只提醒"] and dialog.mode_hint.text() == MODE_TIPS[MODE_NOTIFY]
assert visible_tabs(dialog) == ["其他"] and dialog.tabs.currentIndex() == 2
assert not dialog.offline_check.isVisibleTo(dialog) and dialog.offline_check.isHidden()
assert dialog.open_check.isVisibleTo(dialog) and dialog.enabled_check.isVisibleTo(dialog)
assert dialog.size().height() == size.height(), (dialog.size(), size)
dialog.story_radio.setChecked(True)
app.processEvents()
assert visible_tabs(dialog) == ["说书内容", "间隔与限制", "其他"] and dialog.tabs.currentIndex() == 0
assert dialog.story_page.isVisibleTo(dialog) and not dialog.offline_check.isHidden()  # 在“其他”页里，没被隐藏
assert dialog.size() == size, (dialog.size(), size)
print("三种模式的标签页 OK")

# ---- 1366×768 的屏幕（可用高度 720）：高度放得下，保存按钮看得见，内容在页里面滚动
small = QRect(0, 0, 1366, 720)
dialog._fit_to_content(small)
app.processEvents()
assert dialog.frameGeometry().height() <= small.height(), dialog.frameGeometry()
save = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
assert save.isVisibleTo(dialog) and save.mapTo(dialog, QPoint(0, save.height())).y() <= dialog.height()
check_no_overflow(dialog)
scroll = dialog.tabs.currentWidget()
assert scroll.horizontalScrollBar().maximum() == 0
# 页里面滚到底，最后一个勾选框也能看见
scroll.ensureWidgetVisible(dialog.skip_filtered_check)
app.processEvents()
pos = dialog.skip_filtered_check.mapTo(scroll.viewport(), QPoint(0, dialog.skip_filtered_check.height()))
assert 0 <= pos.y() <= scroll.viewport().height(), pos
dialog.close()
print("小屏幕 OK")

# ---- 新添加的直播间：说书人模式没有内容时序列列表隐藏，提示还在
new = RoomDialog(None, None, set(), store)
new.story_radio.setChecked(True)
new.show()
app.processEvents()
assert not new.queue_box.isVisibleTo(new) and "还没有设置说书内容" in new.story_label.text()
assert new.width() <= DIALOG_WIDTH
check_no_overflow(new)
new.close()
print("新直播间 OK")

# ---- 三种模式保存出来的设置：和界面上填的一致
story.save_room_story(5, "一二三四五六")
digest, chars = story.text_fingerprint("一二三四五六")
for mode in (MODE_REPEAT, MODE_STORY, MODE_NOTIFY):
    room = RoomConfig(room_id=5, uid=9, uname="主播", mode=mode, message="hi", story_title="甲", story_hash=digest, story_chars=chars)
    d = RoomDialog(None, room, set(), store, progress=(2, 6))
    d.msg_edit.setText("你好 世界")
    d.interval_spin.setValue(12)
    d.random_check.setChecked(True)
    d.max_spin.setValue(20)
    d.count_check.setChecked(True)
    d.count_spin.setValue(33)
    d.window_check.setChecked(True)
    d.window_start_edit.setTime(QTime(8, 30))
    d.chunk_spin.setValue(7)
    d.by_line_radio.setChecked(True)
    d.finish_combo.setCurrentIndex(1)
    d.inherit_check.setChecked(True)
    d.skip_filtered_check.setChecked(True)
    d.open_check.setChecked(True)
    d.offline_check.setChecked(True)
    d.enabled_check.setChecked(False)
    assert d.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
    expected = RoomConfig(
        room_id=5, uid=9, uname="主播", mode=mode, message="你好 世界", emoticons=[], repeat_random=False,
        interval_sec=12, interval_random=True, interval_max_sec=20, open_browser=True, send_when_offline=True,
        enabled=False, story_title="甲", story_hash=digest, story_chars=chars, story_chunk_size=7,
        story_by_line=True, story_inherit=True, story_loop=True, story_skip_filtered=True, limit_count=33,
        limit_minutes=0, window_enabled=True, window_start="08:30", window_end=d.window_end_edit.time().toString("HH:mm"),
    )
    assert d.result_config() == expected, (mode, d.result_config())
    assert d.room_edit.text() == "5" and not d.room_edit.isVisibleTo(d)  # 编辑时输入框藏起来，房间号还在
    assert d.new_story_text is None and not d.reset_progress
print("保存结果 OK")

store.close()
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
