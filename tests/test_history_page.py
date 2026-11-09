"""历史记录页的性能测试：记录多、一场里失败上万条时，打开、定时刷新、切换页签都不能卡。

以前每 5 秒刷新一次都要把整张记录表一格一格重填，表格每填一格就把“按内容定宽”的列从头量一遍，
几十条记录就要卡好几秒，CPU 也一直占着；选中的那一场的上万条失败也跟着每次重读、重建。

运行：python tests/test_history_page.py（不显示窗口，用临时数据库）。
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 不弹窗口
ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication, QHeaderView  # noqa: E402

from app.history import HistoryStore  # noqa: E402
from app.ui_history import HistoryPage  # noqa: E402

app = QApplication(sys.argv)
store = HistoryStore(Path(HOME) / "history.db")

# ---- 60 条已结束的记录，外加一条还在发送、失败了 12000 条的
now = time.time()
for i in range(60):
    sid = store.open_session(1000 + i % 5, f"主播{i % 5}", "repeat", "一句弹幕内容" * 3, now - 86400 * (i + 1))
    store.close_session(sid, now - 86400 * (i + 1) + 3600, "下播")
big = store.open_session(42, "测试主播", "story", "某本书", now - 7200)
store._db.executemany(
    "INSERT INTO failures (session_id, at, outcome, message, text) VALUES (?, ?, ?, ?, ?)",
    [(big, now - 7200 + i, "filtered", "f" if i % 3 else "k", f"第 {i} 条被过滤的弹幕") for i in range(12000)],
)
store._db.execute("UPDATE sessions SET sent = 20000, ok = 8000, last_at = ? WHERE id = ?", (now, big))
store._db.commit()

reads = []
real_failures = store.failures
store.failures = lambda session_id: reads.append(session_id) or real_failures(session_id)


def run(action) -> float:
    start = time.perf_counter()
    action()
    for _ in range(3):
        app.processEvents()
    return time.perf_counter() - start


page = HistoryPage(store)
page.resize(1240, 760)
run(page.show)
assert page.sessions.rowCount() == 61, page.sessions.rowCount()

# ---- 选中失败最多的那一场：下面列出全部失败，统计也对
row = next(i for i, r in enumerate(page._records) if r.id == big)
run(lambda: page.sessions.selectRow(row))
assert page.failure_model.rowCount() == 12000, page.failure_model.rowCount()
assert page.failure_model.text(5) == "第 5 条被过滤的弹幕"
assert "系统屏蔽 8000 条" in page.summary.text() and "主播屏蔽 4000 条" in page.summary.text(), page.summary.text()
assert reads == [big], reads
print("选中一场、列出 12000 条失败 OK")

# ---- 定时刷新：这一场没有新的失败就不重新读，而且要快
slowest = max(run(page.refresh) for _ in range(5))
assert reads == [big], f"没变也重新读了失败列表：{reads}"
assert slowest < 0.5, f"定时刷新一次要 {slowest:.2f} 秒"
assert page.sessions.selectionModel().selectedRows()[0].row() == row, "刷新后选中的记录不能丢"
print(f"定时刷新 OK（最慢 {slowest * 1000:.0f} ms）")

# ---- 这一场又失败了一条：刷新时才重新读，列表多一条
store.add_result(big, 42, "测试主播", now + 20000, "rate_limited", "您发送弹幕的频率过快", "新失败的一条")
run(page.refresh)
assert reads == [big, big], reads
assert page.failure_model.rowCount() == 12001 and page.failure_model.text(12000) == "新失败的一条"
print("有新失败才重新读 OK")

# ---- 切换页签、隐藏再打开都要快
slowest = max(run(lambda: page.tabs.setCurrentIndex(index)) for index in (1, 0, 1, 0))
assert slowest < 0.5, f"切换页签要 {slowest:.2f} 秒"
slowest = run(lambda: (page.hide(), page.show()))
assert slowest < 0.5, f"重新打开页面要 {slowest:.2f} 秒"
print("切换页签、重新打开 OK")

# ---- 填完表以后，“按内容定宽”的列要换回来
header = page.sessions.horizontalHeader()
assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch
assert page.room_stats.horizontalHeader().sectionResizeMode(1) == QHeaderView.ResizeMode.ResizeToContents
print("列宽设置复原 OK")

# ---- 取消选中：下面清空
run(page.sessions.clearSelection)
assert page.failure_model.rowCount() == 0
print("取消选中 OK")

page.close()
store.close()
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
