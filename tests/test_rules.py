"""发送规则的测试：被屏蔽不算连续失败、只提醒、每场发送上限、发送时段、跳过以前被屏蔽过的段落、
发送期间换了一轮、查开播状态慢的时候照样能发弹幕（两个后台线程）。

运行：python tests/test_rules.py（不联网，B 站接口和时钟都换成假的）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication  # noqa: E402

from app import bili_api, engine as eng, story  # noqa: E402
from app.config import MODE_NOTIFY, MODE_STORY, RoomConfig  # noqa: E402
from app.engine import Engine, Session  # noqa: E402
from app.history import HistoryStore  # noqa: E402

app = QCoreApplication(sys.argv)
O = bili_api.SendOutcome
OFF, ON = bili_api.LIVE_OFFLINE, bili_api.LIVE_ON

# 假时钟：time.time() 由测试控制（从本地时间 2026-10-25 21:00 开始），其余照旧
clock = [datetime(2026, 10, 25, 21, 0).timestamp()]


class FakeTime:
    monotonic = staticmethod(time.monotonic)
    strftime = staticmethod(time.strftime)
    time = staticmethod(lambda: clock[0])


eng.time = FakeTime
outcomes: list = []  # 接下来每次发送的结果，空了就当成功
sent: list[str] = []


def fake_send(s, c, rid, msg):
    sent.append(msg)
    return bili_api.SendResult(*(outcomes.pop(0) if outcomes else (O.OK, "")))


bili_api.send_danmaku = fake_send
bili_api.get_account_info = lambda s, c: bili_api.AccountInfo(1, "x", 1)
store = HistoryStore()



def live(e, room_id=1):
    """模拟“开播”：先查到未开播，再查到开播（开始监控时就已在播的那一场要手动开始，见 engine.Session.WAIT_NEXT）。"""
    e._apply_status(room_id, OFF, "", "")
    e._apply_status(room_id, ON, "", "")

def make(**kw):
    e = Engine(30, history=store)
    e.set_credentials(bili_api.Credentials("x", "y"))
    room = RoomConfig(room_id=kw.pop("room_id", 1), uid=1, uname="测试主播", message="测试弹幕", interval_sec=5, **kw)
    e.set_rooms([room])
    return e, room


def tick(e, minutes=0.0, result=None, room_id=1):
    """往后拨 minutes 分钟，到时间就发一条。返回这次发了几条。"""
    clock[0] += minutes * 60
    if result:
        outcomes.append(result)
    before = len(sent)
    with e._lock:
        e._rooms[room_id].next_send_at = 0
    e._send_due(None)
    return len(sent) - before


def session(e, room_id=1):
    return e.view(room_id).session


# ---- 被屏蔽（f / k）不算连续失败：一直被屏蔽、f 和 k 混着来，都不停
e, room = make()
live(e)
for i in range(12):
    tick(e, result=(O.FILTERED, "fk"[i % 2]))
assert session(e) is Session.SENDING and len(sent) >= 12, session(e)
print("一直被屏蔽不停发 OK")

# ---- 别的失败照旧：连续 5 次出错就停
e, room = make()
live(e)
for _ in range(5):
    tick(e, result=(O.ERROR, "网络错误"))
assert session(e) is Session.STOPPED, session(e)
print("连续 5 次出错停发 OK")

# ---- 被屏蔽夹在出错中间：不加一，也不清零，出错满 5 次照样停
e, room = make()
live(e)
for result in [(O.ERROR, "x"), (O.FILTERED, "f"), (O.ERROR, "x"), (O.FILTERED, "k"), (O.ERROR, "x"), (O.ERROR, "x")]:
    tick(e, result=result)
assert session(e) is Session.SENDING and e._rooms[1].failures == 4
tick(e, result=(O.RATE_LIMITED, "msg in 1s"))
assert session(e) is Session.STOPPED, "被限流照旧算失败"
# 成功一次就清零
e, room = make()
live(e)
for result in [(O.ERROR, "x")] * 4 + [(O.OK, "")] + [(O.ERROR, "x")] * 4:
    tick(e, result=result)
assert session(e) is Session.SENDING
print("屏蔽夹在出错中间 OK")

# ---- 只提醒：开播 / 下播提醒、开播打开直播页，从不发弹幕，也不记历史记录
e, room = make(mode=MODE_NOTIFY, room_id=2)
reminds, opened = [], []
e.remind.connect(lambda title, text: reminds.append(title))
e.open_page.connect(opened.append)
e._apply_status(2, OFF, "", "")
e._apply_status(2, ON, "", "新的一期")
assert reminds == ["测试主播 开播了"] and opened == [2], (reminds, opened)
assert tick(e, room_id=2) == 0 and session(e, 2) is Session.IDLE
assert e.start_session(2) is False, "只提醒的直播间不能手动开始发送"
e._apply_status(2, OFF, "", ""); e._apply_status(2, OFF, "", "")
assert reminds[-1] == "测试主播 下播了"
assert store.sessions(2) == []
# 直播中改成发弹幕：这一场等手动开始；再改回只提醒：马上停
live(e, 2)
e.set_rooms([replace(room, mode="repeat")])
assert session(e, 2) is Session.WAIT_NEXT and tick(e, room_id=2) == 0
assert e.start_session(2) and tick(e, room_id=2) == 1
e.set_rooms([replace(room, mode=MODE_NOTIFY)])
assert session(e, 2) is Session.IDLE and tick(e, room_id=2) == 0
print("只提醒 OK")

# ---- 每场最多发多少条：按成功的条数算，到了就停；下一场重新计数
e, room = make(limit_count=3, room_id=3)
notes = []
e.notify.connect(lambda title, text: notes.append(text))
live(e, 3)
tick(e, room_id=3); tick(e, result=(O.FILTERED, "f"), room_id=3); tick(e, room_id=3)
assert session(e, 3) is Session.SENDING
tick(e, room_id=3)
assert session(e, 3) is Session.LIMITED and e.view(3).sent_count == 3, (session(e, 3), e.view(3).sent_count)
assert tick(e, room_id=3) == 0 and "达到本场上限 3 条" in notes[-1]
e._apply_status(3, OFF, "", ""); e._apply_status(3, OFF, "", ""); live(e, 3)
assert session(e, 3) is Session.SENDING and tick(e, room_id=3) == 1
print("每场最多发多少条 OK")

# ---- 每场最多发多少分钟：从发出第一条算起
e, room = make(limit_minutes=30, room_id=4)
live(e, 4)
tick(e, 10, room_id=4)  # 开播 10 分钟后才发第一条
tick(e, 29, room_id=4)
assert session(e, 4) is Session.SENDING
assert tick(e, 2, room_id=4) == 0 and session(e, 4) is Session.LIMITED
clock[0] += 60
e._check_records()
r = store.sessions(4)[0]
assert r.end_reason == "达到本场上限 30 分钟" and r.sent == 2, r
print("每场最多发多少分钟 OK")

# ---- 发送时段：时段外不发，记录在时段结束时结束；回到时段里另起一条；跨过半夜的时段
clock[0] = datetime(2026, 10, 25, 22, 50).timestamp()
e, room = make(window_enabled=True, window_start="20:00", window_end="23:00", room_id=5)
live(e, 5)
tick(e, room_id=5)
assert tick(e, 9, room_id=5) == 1  # 22:59 还在时段里
assert tick(e, 2, room_id=5) == 0 and session(e, 5) is Session.SENDING  # 23:01 不发，但这一场没结束
e._check_records()
r = store.sessions(5)[0]
assert not r.ongoing and r.end_reason == "到了发送时段的结束时间（23:00）", r
clock[0] = datetime(2026, 10, 26, 20, 0).timestamp()
assert tick(e, room_id=5) == 1 and store.sessions(5)[0].ongoing  # 第二天 20:00 又开始发，另起一条记录
night = RoomConfig(room_id=0, window_enabled=True, window_start="22:00", window_end="02:00")
assert night.in_window(datetime(2026, 1, 1, 23, 30)) and night.in_window(datetime(2026, 1, 2, 1, 59))
assert not night.in_window(datetime(2026, 1, 2, 2, 0)) and not night.in_window(datetime(2026, 1, 1, 21, 59))
assert RoomConfig(room_id=0, window_enabled=True, window_start="10:00", window_end="10:00").in_window(datetime(2026, 1, 1, 3, 0))
print("发送时段 OK")

# ---- 说书时跳过以前被屏蔽过的段落：可选；系统屏蔽在所有直播间都跳过，主播屏蔽只在那个直播间跳过
text = "一二三四五六七八九十"
digest, chars = story.text_fingerprint(text)
for rid in (6, 7):
    story.save_room_story(rid, text)
store.add_result(store.open_session(99, "别的主播", MODE_STORY, "", clock[0]), 99, "别的主播", clock[0], "filtered", "f", "五六七八")
store.add_result(store.open_session(6, "测试主播", MODE_STORY, "", clock[0]), 6, "测试主播", clock[0], "filtered", "k", "九十")
assert store.is_filtered("五六七八", 6) and store.is_filtered("九十", 6) and not store.is_filtered("九十", 7)
for rid, skip, expect in ((6, True, ["一二三四"]), (7, True, ["一二三四", "九十"]), (6, False, ["一二三四", "五六七八", "九十"])):
    sent.clear()
    e, room = make(room_id=rid, mode=MODE_STORY, story_hash=digest, story_chars=chars, story_chunk_size=4, story_skip_filtered=skip)
    live(e, rid)
    for _ in range(4):
        tick(e, room_id=rid)
    assert sent == expect and session(e, rid) is Session.FINISHED, (rid, skip, sent)
# 循环说书时每一段都屏蔽过：停下来，不卡死
story.save_room_story(8, "五六七八")
digest8, chars8 = story.text_fingerprint("五六七八")
e, room = make(room_id=8, mode=MODE_STORY, story_hash=digest8, story_chars=chars8, story_chunk_size=4, story_skip_filtered=True, story_loop=True)
live(e, 8)
assert tick(e, room_id=8) == 0 and session(e, 8) is Session.STOPPED
print("跳过以前被屏蔽过的段落 OK")

# ---- 发送期间开始了新的一轮（比如刚好开播）：旧一轮的结果不算进新的一轮
e, room = make(room_id=10, send_when_offline=True)
e._apply_status(10, OFF, "", "")
with e._lock:
    item = eng._Pending(10, "测试主播", "测试弹幕", generation=e._rooms[10].generation)
live(e, 10)  # 开播：新的一轮
e._handle_send_result(item, bili_api.SendResult(O.OK, ""), clock[0])
assert e.view(10).sent_count == 0 and session(e, 10) is Session.SENDING
print("发送期间换了一轮 OK")

# ---- 两个后台线程：查开播状态卡住（这里卡 3 秒）的时候，弹幕照样按时发
bili_api.init_anonymous_cookies = lambda s: None
polling = threading.Event()


def slow_status(s, uids):
    polling.set()
    time.sleep(3)
    return {}


bili_api.get_live_status_by_uids = slow_status
bili_api.get_room_info = lambda s, rid: bili_api.RoomInfo(rid, 0, 1, ON)
e, room = make(room_id=12)
live(e, 12)
sent.clear()
started = time.monotonic()
e.start()
assert polling.wait(2), "检测线程没有开始"
deadline = time.monotonic() + 2
while not sent and time.monotonic() < deadline:
    time.sleep(0.05)
waited = time.monotonic() - started
e.stop()
assert sent and waited < 2, f"检测卡住时弹幕等了 {waited:.1f} 秒才发"
print(f"检测卡住时照样发弹幕 OK（{waited:.2f} 秒内发出）")

store.close()
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
