"""说书人模式：长文字的切分、文件存取、进度记录。

一本小说可能有上百万字，所以：
- 文字存成单独的文件 data\\stories\\<房间号>.txt，不放进 config.json；
- 不预先切好所有弹幕，每次只从“当前位置”切出下一条；
- 进度只记一个数字（读到第几个字），存在 data\\progress.json。

发送序列：一个直播间可以放好几组文字，说完一组接着说下一组。几组文字存在同一个文件里，
每组前面有一行“分组标记 + 标题”（见 join_parts）。只有一组时文件和以前一样，没有标记。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import data_dir

# 一行以这个字符开头：发送序列里新的一组从这里开始，后面跟着这一组的标题。
# 不能用 \x1c～\x1e：Python 的 splitlines 会把它们当成换行。
PART_MARK = "\x1f"


@dataclass
class StoryPart:
    """发送序列里的一组文字。"""

    title: str
    text: str


def _clean(text: str) -> str:
    return text.replace(PART_MARK, "")


def split_parts(raw: str, title: str = "") -> list[StoryPart]:
    """把说书文件拆回发送序列。没有分组标记的（只有一组、或者以前的文件）整个算一组，标题用 title。"""
    if PART_MARK not in raw:
        return [StoryPart(title, raw)] if raw.strip() else []
    parts: list[StoryPart] = []
    current: list[str] = []
    current_title = title
    for line in raw.splitlines():
        if line.startswith(PART_MARK):
            if parts or "".join(current).strip():
                parts.append(StoryPart(current_title, "\n".join(current)))
            current_title, current = line[len(PART_MARK):].strip(), []
        else:
            current.append(line)
    parts.append(StoryPart(current_title, "\n".join(current)))
    return [part for part in parts if part.text.strip()]


def join_parts(parts: list[StoryPart]) -> str:
    """把发送序列存成一个文件。只有一组时原样存，和以前的文件一样。"""
    if len(parts) == 1:
        return _clean(parts[0].text)
    return "\n".join(f"{PART_MARK}{' '.join(_clean(part.title).split())}\n{_clean(part.text)}" for part in parts)


def queue_title(parts: list[StoryPart]) -> str:
    """直播间列表、历史记录里显示的说书标题：只有一组就是它的标题，多组写成“第一组 等 3 组”。"""
    if not parts:
        return ""
    first = parts[0].title or " ".join(parts[0].text.split())[:20]
    return first if len(parts) == 1 else f"{first} 等 {len(parts)} 组"


class StoryText:
    """整理过的说书文字。

    每行去掉首尾空白（包括小说常见的全角缩进），丢掉空行，再把所有行连成一条“字流”。
    位置（pos）就是字流里的下标，所以“忽略换行”和“按行说书”两种切法可以随时切换，进度不会乱。
    发送序列的几组文字也连在同一条字流里，parts 记着每一组从哪里到哪里；一条弹幕不会跨两组。
    """

    def __init__(self, raw: str):
        self.line_ends: list[int] = []  # 每一行在字流里的结束位置
        self.parts: list[tuple[str, int, int]] = []  # 每一组：（标题, 开始位置, 结束位置）
        lines: list[str] = []
        title, start, total = "", 0, 0
        for line in raw.splitlines():
            if line.startswith(PART_MARK):
                if total > start:
                    self.parts.append((title, start, total))
                title, start = line[len(PART_MARK):].strip(), total
                continue
            line = line.strip()
            if line:
                lines.append(line)
                total += len(line)
                self.line_ends.append(total)
        if total > start or not self.parts:
            self.parts.append((title, start, total))
        self.stream = "".join(lines)
        self._part_starts = [part[1] for part in self.parts]
        self._part_ends = {part[2] for part in self.parts}
        digest = hashlib.sha1(self.stream.encode("utf-8"))
        if len(self.parts) > 1:  # 分组和标题也算进指纹；只有一组时和以前一样，旧的进度照样能用
            digest.update(json.dumps(self.parts, ensure_ascii=False).encode("utf-8"))
        self.hash = digest.hexdigest()

    @property
    def length(self) -> int:
        return len(self.stream)

    def part_at(self, pos: int) -> int:
        """pos 在第几组（从 0 数）。说完了算最后一组。"""
        return max(0, min(bisect.bisect_right(self._part_starts, pos) - 1, len(self.parts) - 1))

    def next_chunk(self, pos: int, size: int, by_line: bool) -> tuple[str, int]:
        """从 pos 开始切出下一条弹幕，返回（弹幕内容, 下一条的起始位置）。

        按行说书：这一行剩下的内容不够 size 个字，也到行尾为止，下一行留给下一条。
        忽略换行：一行说完接着说下一行，原来换行的地方加一个空格隔开；空格也算在 size 里。
        位置只按原文的字数算（不含加上去的空格），所以两种方式随时切换，进度都不会乱。
        发送序列里一组说到最后，不管还剩多少字数，这一条都到这组结尾为止，下一组从新的一条开始。
        """
        line = bisect.bisect_right(self.line_ends, pos)  # pos 所在的那一行
        if by_line:  # 每组的结尾一定也是一行的结尾
            end = min(pos + size, self.line_ends[line] if line < len(self.line_ends) else self.length)
            return self.stream[pos:end], end

        parts: list[str] = []
        used = 0
        while pos < self.length and used < size:
            take = min(self.line_ends[line] - pos, size - used)
            parts.append(self.stream[pos : pos + take])
            used += take
            pos += take
            if pos == self.line_ends[line] and pos < self.length:  # 说到行尾，还有下一行
                line += 1
                if used >= size - 1 or pos in self._part_ends:
                    break  # 放不下“空格 + 下一个字”了，或者这一组说完了：这条就到这里为止
                parts.append(" ")
                used += 1
        return "".join(parts), pos


def kept_position(old: StoryText | None, new: StoryText, pos: int) -> int:
    """说书内容改了以后从哪里接着说：已经说过的那部分没变（比如只是在发送序列后面加了几组），
    就从原来的进度接着说；否则从头说。"""
    if old is None or pos <= 0 or pos > old.length or pos > new.length:
        return 0
    return pos if old.stream[:pos] == new.stream[:pos] else 0


def count_chunks(text: StoryText, size: int, by_line: bool, start: int = 0) -> int:
    """从 start 说到结尾一共要发多少条。一百万字约 2.8 万条，十几毫秒就数完。"""
    pos, count = start, 0
    while pos < text.length:
        _, pos = text.next_chunk(pos, size, by_line)
        count += 1
    return count


def text_fingerprint(raw: str) -> tuple[str, int]:
    """返回（指纹, 有效字数），保存文字时记进配置。"""
    story = StoryText(raw)
    return story.hash, story.length


# ---------------------------------------------------------------- 读 txt 文件


def read_text_file(path: str | Path) -> str:
    """读取 txt。网上下载的小说常见 UTF-8 和 GBK 两种编码，依次尝试。"""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("认不出这个文件的文字编码，请另存为 UTF-8 编码的 txt 再导入")


# ---------------------------------------------------------------- 每个直播间的说书文字


def _stories_dir() -> Path:
    path = data_dir() / "stories"
    path.mkdir(exist_ok=True)
    return path


def story_path(room_id: int) -> Path:
    return _stories_dir() / f"{room_id}.txt"


def save_room_story(room_id: int, raw: str) -> None:
    path = story_path(room_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    os.replace(tmp, path)


def load_room_story(room_id: int) -> str | None:
    path = story_path(room_id)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def delete_room_story(room_id: int) -> None:
    story_path(room_id).unlink(missing_ok=True)


# ---------------------------------------------------------------- 进度


def _progress_path() -> Path:
    return data_dir() / "progress.json"


def load_progress() -> dict[int, tuple[int, str]]:
    """返回 {房间号: (位置, 文字指纹)}。指纹对不上说明文字换了，进度作废。"""
    try:
        raw = json.loads(_progress_path().read_text(encoding="utf-8"))
        return {int(rid): (int(item["pos"]), str(item["hash"])) for rid, item in raw.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def save_progress(progress: dict[int, tuple[int, str]]) -> None:
    path = _progress_path()
    tmp = path.with_suffix(".tmp")
    data = {str(rid): {"pos": pos, "hash": digest} for rid, (pos, digest) in progress.items()}
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
