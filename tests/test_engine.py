"""监控逻辑测试：开播 / 下播的判断、未开播时也发送、手动开始 / 停止、开始监控时已在播要手动开始。

运行：python tests/test_engine.py（不联网，B 站接口都换成假的）。
"""
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication

from app import bili_api
from app.config import RoomConfig
from app.engine import Engine, Session

app = QCoreApplication(sys.argv)
sent = []
bili_api.send_danmaku = lambda session, creds, rid, msg: (sent.append(rid), bili_api.SendResult(bili_api.SendOutcome.OK))[1]
OFF, ON = bili_api.LIVE_OFFLINE, bili_api.LIVE_ON


def make(**room_kw):
    e = Engine(30)
    e.set_credentials(bili_api.Credentials("x", "y"))
    room = RoomConfig(room_id=1, uid=1, message="hi", interval_sec=5, **room_kw)
    e.set_rooms([room])
    return e, room


def session(e):
    return e.view(1).session


def send_now(e):
    sent.clear()
    with e._lock:
        e._rooms[1].next_send_at = 0
    e._send_due(None)
    return len(sent)


# A. 默认（未开播不发）：未开播→不发；开播→发；下播要连续 2 次才算
e, room = make()
e._apply_status(1, OFF, "", ""); assert session(e) is Session.IDLE and send_now(e) == 0
e._apply_status(1, ON, "", ""); assert session(e) is Session.SENDING and send_now(e) == 1
e._apply_status(1, OFF, "", ""); assert session(e) is Session.SENDING  # 第一次未开播，还不算下播
e._apply_status(1, OFF, "", ""); assert session(e) is Session.IDLE and send_now(e) == 0
print("A 默认行为 OK")

# B. 开始监控时已在播：只监控、不发，手动开始才发；下播再开播（新的一场）自动发
e, room = make()
e._apply_status(1, ON, "", ""); assert session(e) is Session.WAIT_NEXT and send_now(e) == 0
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", ""); assert session(e) is Session.IDLE
e._apply_status(1, ON, "", ""); assert session(e) is Session.SENDING and send_now(e) == 1
e, room = make()
e._apply_status(1, ON, "", "")
assert e.start_session(1) is True and session(e) is Session.SENDING and send_now(e) == 1
# 直播中取消监控、再勾上：同样只监控，等手动开始
e.set_rooms([replace(room, enabled=False)]); e.set_rooms([room])
e._apply_status(1, ON, "", ""); assert session(e) is Session.WAIT_NEXT and send_now(e) == 0
print("B 开始监控时已在播 OK")

# C. 未开播时也发送：开始监控时未开播，要手动开始；开播重新计数；下播后自动接着发
e, room = make(send_when_offline=True)
e._apply_status(1, OFF, "", ""); assert session(e) is Session.WAIT_NEXT and send_now(e) == 0
assert e.start_session(1) and send_now(e) == 1
e._apply_status(1, OFF, "", ""); assert session(e) is Session.SENDING  # 一直未开播，不重置
e._apply_status(1, ON, "", ""); assert session(e) is Session.SENDING and e.view(1).sent_count == 0 and send_now(e) == 1
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", "")
assert session(e) is Session.SENDING and e.view(1).live is False and send_now(e) == 1
print("C 未开播时也发送 OK")

# D. 未开播期间切换这个选项：打开后等手动开始，关掉马上停
e, room = make()
e._apply_status(1, OFF, "", ""); assert session(e) is Session.IDLE
e.set_rooms([replace(room, send_when_offline=True)]); assert session(e) is Session.WAIT_NEXT and send_now(e) == 0
assert e.start_session(1) and send_now(e) == 1
e.set_rooms([replace(room, send_when_offline=False)]); assert session(e) is Session.IDLE and send_now(e) == 0
print("D 切换选项 OK")

# E. 手动“本场开始/停止”在未开播时的表现
e, room = make()
e._apply_status(1, OFF, "", ""); assert e.start_session(1) is False
e.set_rooms([replace(room, send_when_offline=True)])
assert e.start_session(1) is True and send_now(e) == 1
assert e.stop_session(1) is True and session(e) is Session.STOPPED and send_now(e) == 0
assert e.start_session(1) is True and send_now(e) == 1
print("E 手动开始/停止 OK")

# F. 开始监控时已在播、又勾了未开播也发送：这一场不发；下播后自动开始未开播时的发送
e, room = make(send_when_offline=True)
e._apply_status(1, ON, "", ""); assert session(e) is Session.WAIT_NEXT and send_now(e) == 0
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", ""); assert session(e) is Session.SENDING and send_now(e) == 1
print("F 下播后接着发 OK")
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
