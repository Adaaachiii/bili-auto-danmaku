"""数据文件夹位置的测试：程序文件夹能写就用它下面的 data，写不进去改用 %LOCALAPPDATA%，旧数据跟着复制过去。

运行：python tests/test_config.py（用临时文件夹，不碰真实数据）。
“写不进去”用两种办法模拟：程序文件夹放在一个文件下面（建不了文件夹），或者把写入检查换成假的。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
WORK = Path(tempfile.mkdtemp(dir=ROOT / ".test"))
os.environ.pop("BILI_AUTO_DANMAKU_HOME", None)
os.environ["LOCALAPPDATA"] = str(WORK / "localappdata")
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

real_writable = config._writable


def case(name: str) -> tuple[Path, Path]:
    """一个程序文件夹和一个 LOCALAPPDATA 下的数据文件夹（都还不存在）。"""
    return WORK / name / "app", WORK / name / "local" / config.APP_ID


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def readonly(*folders: Path):
    """让这些文件夹在写入检查里“写不进去”。"""
    blocked = {f.resolve() for f in folders}
    config._writable = lambda folder: folder.resolve() not in blocked and real_writable(folder)


# ---- 写入检查本身：能写的文件夹返回 True，不留下试写的文件
assert real_writable(WORK)
assert not list(WORK.glob(".write-test-*"))
assert not real_writable(WORK / "不存在的文件夹")
print("写入检查 OK")

# ---- 全新安装，程序文件夹能写：用程序文件夹下的 data，不碰 LOCALAPPDATA
program, local = case("fresh")
program.mkdir(parents=True)
path, note = config.resolve_data_dir(program, local)
assert path == program / "data" and path.is_dir() and note == "", (path, note)
assert not local.exists()
print("全新安装 OK")

# ---- 全新安装，程序文件夹建不了 data（比如 C:\Program Files）：改用 LOCALAPPDATA，并说明原因
program, local = case("locked")
write(program.parent / "file", "x")
program = program.parent / "file" / "app"  # 在文件下面，建不了文件夹
path, note = config.resolve_data_dir(program, local)
assert path == local and local.is_dir(), path
assert "不能写入" in note and str(local) in note, note
print("程序文件夹建不了 data OK")

# ---- 程序文件夹能建 data 但写不进文件：删掉空的 data，改用 LOCALAPPDATA
program, local = case("mkdir-only")
program.mkdir(parents=True)
readonly(program / "data")
path, note = config.resolve_data_dir(program, local)
config._writable = real_writable
assert path == local and not (program / "data").exists(), path
print("能建文件夹但写不进 OK")

# ---- 已有 data 能写：继续用它，哪怕 LOCALAPPDATA 里也有数据
program, local = case("portable")
write(program / "data" / "config.json", "程序文件夹里的")
write(local / "config.json", "LOCALAPPDATA 里的")
path, note = config.resolve_data_dir(program, local)
assert path == program / "data" and note == "", path
print("已有 data 能写 OK")

# ---- 已有 data 但写不进去，LOCALAPPDATA 里还没有：复制过去接着用，临时文件不复制，原来的不动
program, local = case("migrate")
write(program / "data" / "config.json", "我的直播间")
write(program / "data" / "stories" / "123.txt", "说书文字")
write(program / "data" / "config.tmp", "写到一半的")
readonly(program / "data")
path, note = config.resolve_data_dir(program, local)
config._writable = real_writable
assert path == local, path
assert read(local / "config.json") == "我的直播间" and read(local / "stories" / "123.txt") == "说书文字"
assert not (local / "config.tmp").exists()
assert read(program / "data" / "config.json") == "我的直播间"
assert "复制" in note, note
print("复制旧数据 OK")

# ---- 已有 data 但写不进去，LOCALAPPDATA 里已经有数据：用 LOCALAPPDATA 的，不覆盖
program, local = case("migrated-before")
write(program / "data" / "config.json", "旧的")
write(local / "config.json", "新的")
readonly(program / "data")
path, note = config.resolve_data_dir(program, local)
config._writable = real_writable
assert path == local and read(local / "config.json") == "新的", path
assert "没有从程序文件夹复制" in note, note
print("不覆盖已有数据 OK")

# ---- 复制失败：照样能启动（用空的 LOCALAPPDATA），说明里写上原因
program, local = case("copy-fails")
write(program / "data" / "config.json", "x")
readonly(program / "data")
real_copytree = config.shutil.copytree
config.shutil.copytree = lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满了"))
path, note = config.resolve_data_dir(program, local)
config.shutil.copytree = real_copytree
config._writable = real_writable
assert path == local and local.is_dir() and "磁盘满了" in note, (path, note)
print("复制失败 OK")

# ---- 程序文件夹挪了位置（里面没有 data），LOCALAPPDATA 里有：接着用 LOCALAPPDATA，不另起一份空的
program, local = case("moved")
program.mkdir(parents=True)
write(local / "config.json", "以前的直播间")
path, note = config.resolve_data_dir(program, local)
assert path == local and note == "" and not (program / "data").exists(), path
print("挪了位置 OK")

# ---- 环境变量指定位置时，直接用它（测试和调试用）
override = WORK / "override"
os.environ["BILI_AUTO_DANMAKU_HOME"] = str(override)
assert config.data_dir_for(WORK / "whatever") == (override, "") and override.is_dir()
assert config.data_dir() == override
del os.environ["BILI_AUTO_DANMAKU_HOME"]
assert config.fallback_data_dir() == WORK / "localappdata" / config.APP_ID
print("环境变量 OK")

shutil.rmtree(WORK, ignore_errors=True)
print("全部通过")
