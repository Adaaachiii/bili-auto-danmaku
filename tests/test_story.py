"""说书人模式测试：切分规则、说完停止 / 循环、状态继承、过滤跳过、限流重发、随机间隔、一百万字的速度。

运行：python tests/test_story.py（不联网，B 站接口都换成假的）。
"""
import os
import random
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

from app import bili_api, story
from app.config import MODE_STORY, ON_START_SEND_ONLY, RoomConfig
from app.engine import Engine, Session
from app.story import StoryText

app = QCoreApplication(sys.argv)
OFF, ON = bili_api.LIVE_OFFLINE, bili_api.LIVE_ON
sent: list[str] = []
next_outcome = [bili_api.SendOutcome.OK]
bili_api.send_danmaku = lambda s, c, rid, msg: (sent.append(msg), bili_api.SendResult(next_outcome[0], "f"))[1]

# ---- 切分规则
raw = "　　第一章 开始\n\n这是第一行，内容比较长一些。\n短行\n"
t = StoryText(raw)
assert t.stream == "第一章 开始这是第一行，内容比较长一些。短行", t.stream
# 忽略换行：原换行处加空格，空格也算字数；进度只按原文算
assert t.next_chunk(0, 10, by_line=False) == ("第一章 开始 这是第", 9), t.next_chunk(0, 10, by_line=False)
assert t.next_chunk(0, 7, by_line=False) == ("第一章 开始", 6)  # 放不下“空格+字”，到行尾为止，不把两行粘在一起
assert t.next_chunk(9, 10, by_line=False) == ("一行，内容比较长一些", 19)
assert t.next_chunk(19, 10, by_line=False) == ("。 短行", 22)
# 整段走一遍：拼回去等于原文（空格只出现在原来的换行处），而且没有弹幕以空格开头或结尾
pos, pieces = 0, []
while pos < t.length:
    chunk, pos = t.next_chunk(pos, 5, by_line=False)
    assert chunk and chunk == chunk.strip() and len(chunk) <= 5, chunk
    pieces.append(chunk)
assert "".join(pieces).replace(" ", "") == t.stream.replace(" ", "")
assert t.next_chunk(0, 10, by_line=True) == ("第一章 开始", 6)  # 到行尾为止
assert t.next_chunk(6, 10, by_line=True) == ("这是第一行，内容比较", 16)  # 行太长时照样按字数切
assert t.next_chunk(16, 10, by_line=True) == ("长一些。", 20)
print("切分规则 OK")


def make(text, **kw):
    hash_, chars = story.text_fingerprint(text)
    story.save_room_story(1, text)
    e = Engine(30, ON_START_SEND_ONLY)
    e.set_credentials(bili_api.Credentials("x", "y"))
    room = RoomConfig(room_id=1, uid=1, mode=MODE_STORY, story_hash=hash_, story_chars=chars, story_chunk_size=4, **kw)
    e.set_rooms([room])
    return e, room


def tick(e):
    with e._lock:
        e._rooms[1].next_send_at = 0
    e._send_due(None)


# ---- 按顺序说，说完停止
sent.clear()
e, room = make("一二三四五六七八九十")
e._apply_status(1, ON, "", "")
for _ in range(5):
    tick(e)
assert sent == ["一二三四", "五六七八", "九十"], sent
assert e.view(1).session is Session.FINISHED
print("说完停止 OK")

# ---- 从头循环
sent.clear()
e, room = make("一二三四五六", story_loop=True)
e._apply_status(1, ON, "", "")
for _ in range(4):
    tick(e)
assert sent == ["一二三四", "五六", "一二三四", "五六"], sent
print("从头循环 OK")

# ---- 状态继承：下播后再开播，从停下的地方接着说；不继承就从头
for inherit, expect in ((True, "五六七八"), (False, "一二三四")):
    sent.clear()
    e, room = make("一二三四五六七八九十", story_inherit=inherit)
    e._apply_status(1, ON, "", ""); tick(e)
    e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", "")
    assert e.view(1).session is Session.IDLE
    e._apply_status(1, ON, "", ""); tick(e)
    assert sent[-1] == expect, (inherit, sent)
print("状态继承 OK")

# ---- 程序重启后进度还在（继承开启时）
e2 = Engine(30, ON_START_SEND_ONLY)
e2.set_credentials(bili_api.Credentials("x", "y"))
e2.set_rooms([replace(room, story_inherit=True)])
assert e2.view(1).story_pos == 4, e2.view(1).story_pos
print("重启后进度保留 OK")

# ---- 被过滤的段落跳过，限流的段落重发
sent.clear()
e, room = make("一二三四五六七八九十")
e._apply_status(1, ON, "", "")
next_outcome[0] = bili_api.SendOutcome.FILTERED; tick(e)
next_outcome[0] = bili_api.SendOutcome.RATE_LIMITED; tick(e)
next_outcome[0] = bili_api.SendOutcome.OK; tick(e)
assert sent == ["一二三四", "五六七八", "五六七八"], sent
print("过滤跳过 / 限流重发 OK")

# ---- 换了文字，进度从头
e, room = make("一二三四五六七八九十", story_inherit=True)
e._apply_status(1, ON, "", ""); tick(e)
hash_, chars = story.text_fingerprint("甲乙丙丁戊己庚辛")
story.save_room_story(1, "甲乙丙丁戊己庚辛")
e.set_rooms([replace(room, story_hash=hash_, story_chars=chars)])
assert e.view(1).story_pos == 0
print("换文字后从头 OK")

# ---- 随机间隔
r = RoomConfig(room_id=9, interval_sec=5, interval_random=True, interval_max_sec=9)
delays = {r.next_delay() for _ in range(500)}
assert delays == set(range(5, 10)), delays
print("随机间隔 OK：", sorted(delays))

# ---- 一百万字的速度
novel = "\n".join("　　" + "".join(random.choice("天地玄黄宇宙洪荒日月盈昃辰宿列张，。") for _ in range(random.randint(10, 300))) for _ in range(8000))
novel = (novel * 3)[:1_000_000]
t0 = time.perf_counter(); big = StoryText(novel); t1 = time.perf_counter()
pos, n = 0, 0
while pos < big.length:
    _, pos = big.next_chunk(pos, 40, by_line=True)
    n += 1
t2 = time.perf_counter()
story.save_room_story(2, novel); t3 = time.perf_counter()
StoryText(story.load_room_story(2)); t4 = time.perf_counter()
print(f"一百万字：整理+指纹 {1000*(t1-t0):.0f} ms；切完全部 {n} 条共 {1000*(t2-t1):.0f} ms（每条 {1e6*(t2-t1)/n:.1f} 微秒）；"
      f"存文件 {1000*(t3-t2):.0f} ms；读文件并整理 {1000*(t4-t3):.0f} ms")
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
