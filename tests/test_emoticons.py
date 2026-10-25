"""表情弹幕的测试：解析表情包、发表情时带的参数、独轮车里文字和表情轮流发、表情图片缓存、备份不带缓存。

运行：python tests/test_emoticons.py（不联网，B 站接口换成假的）。
"""
import json
import os
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication  # noqa: E402

from app import backup, bili_api, emoticons  # noqa: E402
from app.config import MAX_EMOTICONS, RoomConfig, config_path, load_config  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.history import HistoryStore  # noqa: E402

app = QCoreApplication(sys.argv)
O = bili_api.SendOutcome
OFF, ON = bili_api.LIVE_OFFLINE, bili_api.LIVE_ON


def emo(unique, name, perm=1, lock=""):
    return {"emoticon_unique": unique, "emoji": name, "url": f"https://i0.hdslb.com/bfs/live/{name}.png",
            "perm": perm, "unlock_show_text": lock}


# ---- 解析表情包：不要 [dog] 那种小表情；装扮表情的名字从表情 ID 里取；没解锁的标出来
data = {"data": [
    {"pkg_id": 100, "pkg_type": 3, "pkg_name": "emoji", "emoticons": [emo("emoji_208", "[dog]")]},
    {"pkg_id": 1, "pkg_type": 1, "pkg_name": "通用表情", "current_cover": "https://i0.hdslb.com/c.png",
     "emoticons": [emo("official_331", "啊"), emo("official_332", "冲鸭")]},
    {"pkg_id": 5470, "pkg_type": 5, "pkg_name": "装扮表情",
     "emoticons": [emo("upower_[東雪蓮音乐收藏集_不喜欢]", "不喜欢"), emo("upower_[東雪蓮音乐收藏集_打call]", "打call")]},
    {"pkg_id": 2, "pkg_type": 5, "pkg_name": "装扮表情", "emoticons": [emo("upower_[tv_doge]", "doge")]},
    {"pkg_id": 1, "pkg_type": 5, "pkg_name": "装扮表情",
     "emoticons": [emo("upower_[doge_金箍]", "金箍"), emo("upower_[笑哭]", "笑哭"), emo("upower_[蹲蹲]", "蹲蹲")]},
    {"pkg_id": 7, "pkg_type": 2, "pkg_name": "房间专属表情",
     "emoticons": [emo("room_1_1", "专属", perm=0, lock="粉丝团 5 级解锁"), {"emoji": "没有 ID 的不要"}]},
    {"pkg_id": 8, "pkg_type": 2, "pkg_name": "空的", "emoticons": []},
]}
packages = bili_api.parse_emoticon_packages(data)
names = [p.name for p in packages]
assert names == ["通用表情", "東雪蓮音乐收藏集", "小电视", "装扮表情（金箍等）", "房间专属表情"], names
assert packages[0].cover == "https://i0.hdslb.com/c.png"
assert [e.unique for e in packages[1].emoticons] == ["upower_[東雪蓮音乐收藏集_不喜欢]", "upower_[東雪蓮音乐收藏集_打call]"]
locked = packages[-1].emoticons
assert len(locked) == 1 and not locked[0].usable and locked[0].lock_text == "粉丝团 5 级解锁"
print("解析表情包 OK")


# ---- 获取表情要带登录 Cookie；没登录时给一句看得懂的话
class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.payload)


creds = bili_api.Credentials("sess", "jct")
s = FakeSession({"code": 0, "data": data})
assert len(bili_api.get_emoticons(s, creds, 123)) == 5
method, url, kwargs = s.calls[0]
assert method == "GET" and url == bili_api.EMOTICONS_URL
assert kwargs["params"] == {"platform": "pc", "room_id": 123} and kwargs["cookies"]["SESSDATA"] == "sess"
try:
    bili_api.get_emoticons(FakeSession({"code": -101, "message": "账号未登录"}), creds, 123)
except bili_api.BiliApiError as e:
    assert "登录" in str(e)
else:
    raise AssertionError("没登录应该报错")
print("获取表情 OK")

# ---- 发表情：表情 ID 当弹幕内容，多带 dm_type=1；发文字不带
s = FakeSession({"code": 0, "message": "", "data": {}})
assert bili_api.send_emoticon(s, creds, 123, "official_331").outcome is O.OK
form = s.calls[0][2]["data"]
assert form["msg"] == "official_331" and form["dm_type"] == "1" and form["emoticonOptions"] == "[object Object]"
assert form["roomid"] == "123" and form["csrf"] == "jct"
bili_api.send_danmaku(s, creds, 123, "主播加油")
form = s.calls[1][2]["data"]
assert form["msg"] == "主播加油" and "dm_type" not in form and "emoticonOptions" not in form
print("发表情的参数 OK")

# ---- 配置：轮流的顺序、显示的文字、读配置时清理坏数据
E1 = {"unique": "official_331", "name": "啊", "url": "https://i0.hdslb.com/a.png"}
E2 = {"unique": "upower_[x_打call]", "name": "打call", "url": "https://i0.hdslb.com/b.png"}
room = RoomConfig(room_id=1, message="主播加油", emoticons=[E1, E2])
assert [(i.text, i.emoticon) for i in room.repeat_items()] == [
    ("主播加油", ""), ("表情「啊」", "official_331"), ("表情「打call」", "upower_[x_打call]")]
assert room.repeat_text() == "主播加油 ＋ 表情：啊、打call"
assert RoomConfig(room_id=1, emoticons=[E1]).repeat_text() == "表情：啊"
assert RoomConfig(room_id=1, message="只有字").repeat_text() == "只有字"
many = RoomConfig(room_id=1, emoticons=[{"unique": f"u{i}", "name": f"n{i}", "url": ""} for i in range(7)])
assert many.repeat_text() == "表情：n0、n1、n2、n3、n4等 7 个"

raw_rooms = [{"room_id": 1, "message": "", "repeat_random": True, "emoticons": [
    E1, dict(E1), {"unique": "", "name": "没有 ID"}, "不是字典", {"unique": "only_id"},
    *({"unique": f"x{i}"} for i in range(MAX_EMOTICONS + 5)),
]}, {"room_id": 2, "emoticons": "坏掉的"}]
config_path().write_text(json.dumps({"rooms": raw_rooms}, ensure_ascii=False), encoding="utf-8")
cfg = load_config()
cleaned = cfg.rooms[0].emoticons
assert cleaned[0] == E1 and cleaned[1] == {"unique": "only_id", "name": "only_id", "url": ""}
assert len(cleaned) == MAX_EMOTICONS and len({e["unique"] for e in cleaned}) == MAX_EMOTICONS
assert cfg.rooms[0].repeat_random is True and cfg.rooms[1].emoticons == []
print("配置 OK")

# ---- 引擎：文字和表情按顺序轮流；表情走发表情的接口；新的一轮从文字开始；历史记录里写全内容
sent: list[tuple[str, str]] = []


def fake_send(s, c, rid, msg):
    sent.append(("text", msg))
    return bili_api.SendResult(O.OK)


def fake_emoticon(s, c, rid, unique):
    sent.append(("emoticon", unique))
    return bili_api.SendResult(O.OK)


bili_api.send_danmaku = fake_send
bili_api.send_emoticon = fake_emoticon
bili_api.get_account_info = lambda s, c: bili_api.AccountInfo(1, "x", 1)
store = HistoryStore()


def make(room):
    e = Engine(30, history=store)
    e.set_credentials(creds)
    e.set_rooms([room])
    e._apply_status(room.room_id, OFF, "", "")
    e._apply_status(room.room_id, ON, "", "")  # 开播：自动开始发
    return e


def tick(e, n, room_id):
    for _ in range(n):
        with e._lock:
            e._rooms[room_id].next_send_at = 0
        e._send_due(None)


e = make(RoomConfig(room_id=11, uid=11, uname="轮流", message="主播加油", emoticons=[E1, E2], interval_sec=5))
tick(e, 5, 11)
assert sent == [("text", "主播加油"), ("emoticon", "official_331"), ("emoticon", "upower_[x_打call]"),
                ("text", "主播加油"), ("emoticon", "official_331")], sent
assert e.view(11).sent_count == 5
record = store.sessions(11)[0]
assert record.content == "主播加油 ＋ 表情：啊、打call", record.content
# 下播再开播：新的一轮从文字开始
e._apply_status(11, OFF, "", "")
e._apply_status(11, OFF, "", "")
e._apply_status(11, ON, "", "")
sent.clear()
tick(e, 1, 11)
assert sent == [("text", "主播加油")], sent
print("按顺序轮流 OK")

# 只选表情、不填文字：只发表情
sent.clear()
e = make(RoomConfig(room_id=12, uid=12, emoticons=[E2], interval_sec=5))
tick(e, 3, 12)
assert sent == [("emoticon", "upower_[x_打call]")] * 3, sent
print("只发表情 OK")

# 随机顺序：每样都发到，但不会连着发同一条
sent.clear()
e = make(RoomConfig(room_id=13, uid=13, message="字", emoticons=[E1, E2], repeat_random=True, interval_sec=5))
tick(e, 300, 13)
assert all(a != b for a, b in zip(sent, sent[1:])), "随机时连着发了同一条"
counts = Counter(sent)
assert len(counts) == 3 and min(counts.values()) > 50, counts
print("随机顺序 OK")

# 文字和表情都没有：什么也不发
sent.clear()
e = make(RoomConfig(room_id=14, uid=14, interval_sec=5))
tick(e, 3, 14)
assert sent == []
store.close()

# ---- 表情图片缓存：第一次下载并存起来，第二次直接读，不再联网
downloads = []
real_fetch_image = bili_api.fetch_image


def fake_fetch(url, what="图片"):
    downloads.append(url)
    return b"PNG" + url.encode()


bili_api.fetch_image = fake_fetch
url = "https://i0.hdslb.com/bfs/live/abc.png"
assert emoticons.load_image(url) == b"PNG" + url.encode()
assert emoticons.load_image(url) == b"PNG" + url.encode() and downloads == [url]
cached = emoticons.cache_path(url)
assert cached.exists() and cached.suffix == ".png" and cached.parent == Path(HOME) / "cache" / "emoticons"
assert not list(cached.parent.glob("*.tmp"))
assert emoticons.cache_path("https://i0.hdslb.com/x?y=1").suffix == ".img"
print("图片缓存 OK")

# ---- 备份不带表情图片缓存
info = backup.export_backup(Path(HOME) / "备份.zip")
with zipfile.ZipFile(Path(HOME) / "备份.zip") as z:
    assert not any(name.startswith("cache/") for name in z.namelist()), z.namelist()
    assert "config.json" in z.namelist()
print("备份不带缓存 OK")

# ---- 只接受 B 站自己的图片服务器
try:
    real_fetch_image("https://example.com/a.png", "表情图片")
except bili_api.BiliApiError as e:
    assert "表情图片地址不是 B 站的图片服务器" in str(e)
else:
    raise AssertionError("别的网站的图片不应该下载")
print("图片地址检查 OK")

shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
