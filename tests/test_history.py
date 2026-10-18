"""历史记录测试：开始 / 结束的各种情况、不到 5 分钟不保存、失败原因、屏蔽词整理表、程序异常退出后的补记。

运行：python tests/test_history.py（不联网，B 站接口和时钟都换成假的）。
"""
import csv
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication

from app import bili_api, engine as eng, history, story
from app.config import MODE_STORY, ON_START_SEND_ONLY, RoomConfig
from app.engine import Engine, Session
from app.history import HistoryStore

app = QCoreApplication(sys.argv)
OFF, ON = bili_api.LIVE_OFFLINE, bili_api.LIVE_ON
Outcome = bili_api.SendOutcome

# 假时钟：time.time() 由测试控制，其余照旧
clock = [1_000_000.0]


class FakeTime:
    monotonic = staticmethod(time.monotonic)
    strftime = staticmethod(time.strftime)
    time = staticmethod(lambda: clock[0])


eng.time = FakeTime
outcomes: list = []  # 接下来每次发送的结果，空了就当成功
bili_api.send_danmaku = lambda s, c, rid, msg: bili_api.SendResult(*(outcomes.pop(0) if outcomes else (Outcome.OK, "")))
bili_api.get_account_info = lambda s, c: bili_api.AccountInfo(1, "x", 1)

store = HistoryStore()


def make(**kw):
    e = Engine(30, ON_START_SEND_ONLY, history=store)
    e.set_credentials(bili_api.Credentials("x", "y"))
    room = RoomConfig(room_id=kw.pop("room_id", 1), uid=1, uname="测试主播", message="独轮车测试", interval_sec=5, **kw)
    e.set_rooms([room])
    return e, room


def send(e, minutes=0.0, result=None, room_id=1):
    """往后拨 minutes 分钟，再发一条。"""
    clock[0] += minutes * 60
    if result:
        outcomes.append(result)
    with e._lock:
        e._rooms[room_id].next_send_at = 0
    e._send_due(None)


def check(e, minutes=0.0):
    clock[0] += minutes * 60
    e._check_records()


def latest(room_id=1):
    rows = store.sessions(room_id)
    return rows[0] if rows else None


# ---- A. 一场直播：下播时结束，统计和失败原因都对
e, room = make()
e._apply_status(1, ON, "", "")
send(e)
send(e, 2, (Outcome.FILTERED, "f"))
send(e, 2, (Outcome.RATE_LIMITED, "msg in 1s"))
send(e, 2)
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", "")
check(e, 0.5)
r = latest()
assert r and not r.ongoing and r.end_reason == "下播", r
assert (r.sent, r.ok, r.failed) == (4, 2, 2), r
assert round(r.duration()) == 6.5 * 60, r.duration()
fails = store.failures(r.id)
assert [f.reason for f in fails] == ["系统屏蔽（f）", "被限流（msg in 1s）"], [f.reason for f in fails]
assert all(f.text == "独轮车测试" for f in fails)
print("A 下播结束、统计、失败原因 OK")

# ---- B. 不到 5 分钟：记录不保存，但被过滤的弹幕照样进整理表
before = len(store.sessions(1))
e._apply_status(1, ON, "", "")
send(e, 1, (Outcome.FILTERED, "k"))
send(e, 1)
e.stop_session(1)
check(e)
assert len(store.sessions(1)) == before, "不到 5 分钟的不该保存"
assert store.filtered_count() == 1  # 同一直播间同样的内容合并成一行
path = store.write_filtered_csv()
with open(path, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))
assert rows[0][:5] == ["弹幕内容", "主播", "房间号", "被过滤次数", "屏蔽类型"] and rows[1][:5] == ["独轮车测试", "测试主播", "1", "2", "主播屏蔽（k）"], rows
assert open(path, "rb").read(3) == b"\xef\xbb\xbf"  # 带 BOM，Excel 打开不乱码
print("B 不到 5 分钟不保存、整理表合并计数 OK")

# ---- C. 手动停止 / 暂停监控 / 登录失效 / 删除直播间 / 程序退出，各自的结束原因
cases = [
    ("手动停止", lambda e: e.stop_session(1)),
    ("暂停监控", lambda e: e.set_paused(True)),
    ("登录失效或退出登录", lambda e: e.set_credentials(None)),
    ("停用了这个直播间", lambda e: e.set_rooms([replace(room, enabled=False)])),
    ("删除了直播间", lambda e: e.set_rooms([])),
    ("程序退出", lambda e: e.close_records("程序退出")),
]
for reason, action in cases:
    e, room = make()
    e._apply_status(1, ON, "", "")
    send(e)
    send(e, 6)
    action(e)
    check(e)
    r = latest()
    assert r and r.end_reason == reason and r.sent == 2, (reason, r)
print("C 各种结束原因 OK：", "、".join(reason for reason, _ in cases))

# ---- D. 未开播时也发送：开播、下播都另起一条记录
e, room = make(send_when_offline=True)
e._apply_status(1, OFF, "", "")
send(e); send(e, 6)
e._apply_status(1, ON, "", "")
check(e)
assert latest().end_reason == "开播了，另起一条记录"
send(e); send(e, 6)
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", "")
check(e)
assert latest().end_reason == "下播了，按设置继续发送（另起一条记录）", latest()
print("D 开播 / 下播另起记录 OK")

# ---- E. 连续失败自动停发、被禁言、说书说完
e, room = make()
e._apply_status(1, ON, "", "")
send(e)
for _ in range(5):
    send(e, 1.5, (Outcome.ERROR, "网络错误"))
check(e)
assert latest().end_reason == "连续 5 次发送失败，自动停发" and latest().failed == 5, latest()

e, room = make()
e._apply_status(1, ON, "", "")
send(e); send(e, 6, (Outcome.MUTED, "你已被禁言"))
check(e)
assert latest().end_reason == "被禁言", latest()

text = "一二三四五六七八"
digest, chars = story.text_fingerprint(text)
story.save_room_story(7, text)
e, room = make(room_id=7, mode=MODE_STORY, story_title="短篇", story_hash=digest, story_chars=chars, story_chunk_size=4)
e._apply_status(7, ON, "", "")
send(e, room_id=7); send(e, 6, room_id=7)
assert e.view(7).session is Session.FINISHED
check(e)
r = latest(7)
assert r.end_reason == "说书说完了" and r.content == "短篇" and r.mode == MODE_STORY, r
print("E 连续失败、被禁言、说书说完 OK")

# ---- F. 程序异常退出：下次启动时按最后一次发送的时间补上结束
e, room = make(room_id=9)
e._apply_status(9, ON, "", "")
send(e, room_id=9); send(e, 7, room_id=9)
last_send = clock[0]
clock[0] += 3600  # 程序崩了，一小时后才重新打开
store2 = HistoryStore()
assert store2.close_unfinished("程序异常退出") == 1
r = store2.sessions(9)[0]
assert r.end_reason == "程序异常退出" and r.ended_at == last_send, r
store2.close()
print("F 异常退出后补记 OK")

# ---- G. 删除记录：进行中的不删
e, room = make(room_id=11)
e._apply_status(11, ON, "", "")
send(e, room_id=11)
ongoing = latest(11)
assert ongoing.ongoing
store.delete_sessions([ongoing.id, latest().id])
assert latest(11) is not None, "进行中的记录不该被删"
print("G 删除记录 OK")

store.close()
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
