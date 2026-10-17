"""说书人模式：长文字的切分、文件存取、进度记录。

一本小说可能有上百万字，所以：
- 文字存成单独的文件 data\\stories\\<房间号>.txt，不放进 config.json；
- 不预先切好所有弹幕，每次只从“当前位置”切出下一条；
- 进度只记一个数字（读到第几个字），存在 data\\progress.json。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
from pathlib import Path

from .config import data_dir


class StoryText:
    """整理过的说书文字。

    每行去掉首尾空白（包括小说常见的全角缩进），丢掉空行，再把所有行连成一条“字流”。
    位置（pos）就是字流里的下标，所以“忽略换行”和“按行说书”两种切法可以随时切换，进度不会乱。
    """

    def __init__(self, raw: str):
        lines = [line.strip() for line in raw.splitlines()]
        lines = [line for line in lines if line]
        self.stream = "".join(lines)
        self.line_ends: list[int] = []  # 每一行在字流里的结束位置
        total = 0
        for line in lines:
            total += len(line)
            self.line_ends.append(total)
        self.hash = hashlib.sha1(self.stream.encode("utf-8")).hexdigest()

    @property
    def length(self) -> int:
        return len(self.stream)

    def next_chunk(self, pos: int, size: int, by_line: bool) -> tuple[str, int]:
        """从 pos 开始切出下一条弹幕，返回（弹幕内容, 下一条的起始位置）。

        按行说书：这一行剩下的内容不够 size 个字，也到行尾为止，下一行留给下一条。
        忽略换行：一行说完接着说下一行，原来换行的地方加一个空格隔开；空格也算在 size 里。
        位置只按原文的字数算（不含加上去的空格），所以两种方式随时切换，进度都不会乱。
        """
        line = bisect.bisect_right(self.line_ends, pos)  # pos 所在的那一行
        if by_line:
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
                if used >= size - 1:
                    break  # 放不下“空格 + 下一个字”了，这条就到行尾为止，免得两行粘在一起
                parts.append(" ")
                used += 1
        return "".join(parts), pos


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
