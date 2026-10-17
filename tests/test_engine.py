"""监控逻辑测试：开播 / 下播的判断、未开播时也发送、手动开始 / 停止、本次直播不发送。

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

from app import bili_api, engine as eng
from app.config import ON_START_SEND_ONLY, ON_START_WAIT, RoomConfig
from app.engine import Engine, Session

app = QCoreApplication(sys.argv)
sent = []
bili_api.send_danmaku = lambda session, creds, rid, msg: (sent.append(rid), bili_api.SendResult(bili_api.SendOutcome.OK))[1]
OFF, ON = bili_api.LIVE_OFFLINE, bili_api.LIVE_ON


def make(on_start=ON_START_SEND_ONLY, **room_kw):
    e = Engine(30, on_start)
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

# B. 未开播时也发送：一开始未开播就发；开播重新计数；下播后继续发
e, room = make(send_when_offline=True)
e._apply_status(1, OFF, "", ""); assert session(e) is Session.SENDING and send_now(e) == 1
e._apply_status(1, OFF, "", ""); assert session(e) is Session.SENDING  # 一直未开播，不重置
e._apply_status(1, ON, "", ""); assert session(e) is Session.SENDING and e.view(1).sent_count == 0 and send_now(e) == 1
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", "")
assert session(e) is Session.SENDING and e.view(1).live is False and send_now(e) == 1
print("B 未开播时也发送 OK")

# C. 未开播期间切换这个选项，立即生效
e, room = make()
e._apply_status(1, OFF, "", ""); assert session(e) is Session.IDLE
e.set_rooms([replace(room, send_when_offline=True)]); assert session(e) is Session.SENDING and send_now(e) == 1
e.set_rooms([replace(room, send_when_offline=False)]); assert session(e) is Session.IDLE and send_now(e) == 0
print("C 切换选项 OK")

# D. 手动“本场开始/停止”在未开播时的表现
e, room = make()
e._apply_status(1, OFF, "", ""); assert e.start_session(1) is False
e.set_rooms([replace(room, send_when_offline=True)])
assert e.stop_session(1) is True and session(e) is Session.STOPPED and send_now(e) == 0
assert e.start_session(1) is True and send_now(e) == 1
print("D 手动开始/停止 OK")

# E. “本次直播不发送”：启动时在播不发；下播后若勾了未开播也发送，就开始发
e, room = make(on_start=ON_START_WAIT, send_when_offline=True)
e._apply_status(1, ON, "", ""); assert session(e) is Session.WAIT_NEXT and send_now(e) == 0
e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", ""); assert session(e) is Session.SENDING and send_now(e) == 1
print("E 本次直播不发送 OK")
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
