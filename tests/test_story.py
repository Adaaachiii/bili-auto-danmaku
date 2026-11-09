"""说书人模式测试：切分规则、说完停止 / 循环、状态继承、过滤跳过、限流重发、发送序列、随机间隔、一百万字的速度。

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
from app.config import MODE_STORY, RoomConfig
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
# 预计条数：和真的一条条切出来的一样多
assert story.count_chunks(t, 5, by_line=False) == len(pieces)
assert story.count_chunks(t, 10, by_line=True) == 4 and story.count_chunks(t, 10, by_line=True, start=16) == 2
print("切分规则 OK")



def live(e, room_id=1):
    """模拟“开播”：先查到未开播，再查到开播（开始监控时就已在播的那一场要手动开始，见 engine.Session.WAIT_NEXT）。"""
    e._apply_status(room_id, OFF, "", "")
    e._apply_status(room_id, ON, "", "")

def make(text, **kw):
    hash_, chars = story.text_fingerprint(text)
    story.save_room_story(1, text)
    e = Engine(30)
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
live(e)
for _ in range(5):
    tick(e)
assert sent == ["一二三四", "五六七八", "九十"], sent
assert e.view(1).session is Session.FINISHED
print("说完停止 OK")

# ---- 从头循环
sent.clear()
e, room = make("一二三四五六", story_loop=True)
live(e)
for _ in range(4):
    tick(e)
assert sent == ["一二三四", "五六", "一二三四", "五六"], sent
print("从头循环 OK")

# ---- 状态继承：下播后再开播，从停下的地方接着说；不继承就从头
for inherit, expect in ((True, "五六七八"), (False, "一二三四")):
    sent.clear()
    e, room = make("一二三四五六七八九十", story_inherit=inherit)
    live(e); tick(e)
    e._apply_status(1, OFF, "", ""); e._apply_status(1, OFF, "", "")
    assert e.view(1).session is Session.IDLE
    live(e); tick(e)
    assert sent[-1] == expect, (inherit, sent)
print("状态继承 OK")

# ---- 程序重启后进度还在（继承开启时）
e2 = Engine(30)
e2.set_credentials(bili_api.Credentials("x", "y"))
e2.set_rooms([replace(room, story_inherit=True)])
assert e2.view(1).story_pos == 4, e2.view(1).story_pos
print("重启后进度保留 OK")

# ---- 被过滤的段落跳过，限流的段落重发
sent.clear()
e, room = make("一二三四五六七八九十")
live(e)
next_outcome[0] = bili_api.SendOutcome.FILTERED; tick(e)
next_outcome[0] = bili_api.SendOutcome.RATE_LIMITED; tick(e)
next_outcome[0] = bili_api.SendOutcome.OK; tick(e)
assert sent == ["一二三四", "五六七八", "五六七八"], sent
print("过滤跳过 / 限流重发 OK")

# ---- 换了文字，进度从头
e, room = make("一二三四五六七八九十", story_inherit=True)
live(e); tick(e)
hash_, chars = story.text_fingerprint("甲乙丙丁戊己庚辛")
story.save_room_story(1, "甲乙丙丁戊己庚辛")
e.set_rooms([replace(room, story_hash=hash_, story_chars=chars)])
assert e.view(1).story_pos == 0
print("换文字后从头 OK")

# ---- 发送序列：存进一个文件再拆回来；只有一组时和以前的文件一模一样
Part = story.StoryPart
queue = [Part("甲", "一二三四五\n六七"), Part("乙", "八九十\n"), Part("丙", "子丑")]
raw = story.join_parts(queue)
assert [(p.title, p.text.strip()) for p in story.split_parts(raw)] == [(p.title, p.text.strip()) for p in queue]
assert story.join_parts([Part("书", "一二三")]) == "一二三"
assert story.split_parts("一二三", "书")[0].title == "书" and story.split_parts("  \n") == []
assert StoryText("一二三").hash == StoryText(story.join_parts([Part("书", "一二三")])).hash  # 旧的进度照样能用
mark = story.PART_MARK
weird = story.join_parts([Part(f"a{mark}b\nc", f"x{mark}y\n{mark}w"), Part("d", "z")])  # 标题、正文里的分组标记去掉，不会弄乱序列
assert [(p.title, p.text) for p in story.split_parts(weird)] == [("ab c", "xy\nw"), ("d", "z")]
assert story.queue_title(queue) == "甲 等 3 组" and story.queue_title(queue[:1]) == "甲"
print("发送序列存取 OK")

# ---- 发送序列的切分：组内照样按设置跨行，但一条弹幕不跨组
t = StoryText(raw)
assert t.stream == "一二三四五六七八九十子丑" and t.parts == [("甲", 0, 7), ("乙", 7, 10), ("丙", 10, 12)], t.parts
assert t.next_chunk(0, 10, by_line=False) == ("一二三四五 六七", 7)
assert t.next_chunk(5, 10, by_line=True) == ("六七", 7)
assert [t.part_at(p) for p in (0, 6, 7, 10, 12)] == [0, 0, 1, 2, 2]
assert t.hash != StoryText(story.join_parts([Part("甲", "一二三四五\n六七"), Part("乙改", "八九十"), Part("丙", "子丑")])).hash
# 改了以后从哪里接着说：说过的部分没变就接着说，变了就从头
longer = StoryText(story.join_parts(queue + [Part("丁", "寅卯")]))
assert story.kept_position(t, longer, 9) == 9 and story.kept_position(t, longer, 12) == 12
assert story.kept_position(t, StoryText(story.join_parts(queue[1:])), 9) == 0
assert story.kept_position(None, longer, 9) == 0 and story.kept_position(t, longer, 0) == 0
print("发送序列切分 OK")

# ---- 发送序列：说完一组接着说下一组，全部说完才停；日志里说正在说第几组
for loop, expect in ((False, ["一二三四", "五", "六七八"]), (True, ["一二三四", "五", "六七八", "一二三四"])):
    sent.clear()
    e, room = make(story.join_parts([Part("甲", "一二三四五"), Part("乙", "六七八")]), story_loop=loop)
    logs: list[str] = []
    e.log_message.connect(lambda _level, text: logs.append(text))
    live(e)
    assert e.view(1).story_part_titles == ("甲", "乙") and e.view(1).story_part == 0
    for _ in range(len(expect)):
        tick(e)
    assert sent == expect, (loop, sent)
    assert any("第 1/2 组《甲》" in line for line in logs) and any("第 2/2 组《乙》" in line for line in logs), logs
    if not loop:
        assert e.view(1).session is Session.FINISHED and e.view(1).story_part == 1
        assert any("2 组文字" in line for line in logs), logs
print("发送序列按顺序说 OK")

# ---- 说完以后在序列后面加一组：马上接着说新加的，不从头；改了说过的部分才从头
sent.clear()
e, room = make("一二三四")
live(e); tick(e)
assert e.view(1).session is Session.FINISHED
appended = story.join_parts([Part("甲", "一二三四"), Part("乙", "五六七")])
hash_, chars = story.text_fingerprint(appended)
story.save_room_story(1, appended)
e.set_rooms([replace(room, story_hash=hash_, story_chars=chars)])
assert e.view(1).story_pos == 4 and e.view(1).session is Session.SENDING, e.view(1)
tick(e)
assert sent == ["一二三四", "五六七"], sent
assert story.load_progress()[1] == (7, hash_), "进度要跟着新的内容存下来，重启后还能接着说"
changed = story.join_parts([Part("甲", "一二三X"), Part("乙", "五六七")])
hash_, chars = story.text_fingerprint(changed)
story.save_room_story(1, changed)
e.set_rooms([replace(room, story_hash=hash_, story_chars=chars)])
assert e.view(1).story_pos == 0
print("序列后面加内容接着说 OK")

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
