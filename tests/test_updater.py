"""从压缩包更新的测试：检查压缩包、替换文件、出错时恢复旧版本、data 文件夹不受影响。

运行：python tests/test_updater.py（用假的程序文件夹，不碰真实安装）。
打包过（dist 里有压缩包）的话，顺便检查真实压缩包能不能通过检查。
"""
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
WORK = Path(tempfile.mkdtemp(dir=ROOT / ".test"))
os.environ.pop("BILI_AUTO_DANMAKU_HOME", None)
os.environ["LOCALAPPDATA"] = str(WORK / "localappdata")  # 不碰真实的 %LOCALAPPDATA%
sys.path.insert(0, str(ROOT))

from app import updater  # noqa: E402
from app.config import APP_VERSION  # noqa: E402
from app.updater import EXE_NAME, UpdateError  # noqa: E402

started: list[list[str]] = []
messages: list[str] = []
updater._start = lambda target, extra: started.append(extra)
updater._message = messages.append
updater.time.sleep = lambda seconds: None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_dirs(name: str) -> tuple[Path, Path]:
    """一个“旧版”程序文件夹和一个“新版”文件夹。"""
    target, source = WORK / name / "app", WORK / name / "new"
    write(target / EXE_NAME, "old exe")
    write(target / "_internal" / "lib.txt", "old lib")
    write(target / "_internal" / "only_in_old.txt", "old")
    write(target / "data" / "config.json", "我的数据")
    write(source / EXE_NAME, "new exe")
    write(source / "_internal" / "lib.txt", "new lib")
    write(source / "licenses" / "LICENSE.txt", "MIT")
    return target, source


dead_pid = "999999"  # 不存在的进程号，当作“旧程序已经退出”

# ---- 替换成功：新文件到位、旧的多余文件跟着 _internal 一起换掉、data 不动、启动新版本
target, source = make_dirs("ok")
assert updater.install_main([str(target), dead_pid, "1.0.0"], source=source) == 0, messages
assert (target / EXE_NAME).read_text(encoding="utf-8") == "new exe"
assert (target / "_internal" / "lib.txt").read_text(encoding="utf-8") == "new lib"
assert not (target / "_internal" / "only_in_old.txt").exists()
assert (target / "licenses" / "LICENSE.txt").exists()
assert (target / "data" / "config.json").read_text(encoding="utf-8") == "我的数据"
assert started[-1] == ["--updated-from", "1.0.0"], started
assert "文件替换完成" in (target / "data" / "logs" / "update.log").read_text(encoding="utf-8")
assert updater.cleanup(target) and not (target / updater.UPDATE_DIR).exists()
print("替换成功 OK")

# ---- 复制到一半出错：旧版本原样恢复，并重新打开旧版本
target, source = make_dirs("fail")
real_copytree = shutil.copytree
updater.shutil.copytree = lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满了"))
assert updater.install_main([str(target), dead_pid, "1.0.0"], source=source) == 1
updater.shutil.copytree = real_copytree
assert (target / EXE_NAME).read_text(encoding="utf-8") == "old exe"
assert (target / "_internal" / "only_in_old.txt").exists()
assert (target / "data" / "config.json").read_text(encoding="utf-8") == "我的数据"
assert started[-1] == [] and "已恢复旧版本" in messages[-1], (started, messages)
print("出错恢复旧版本 OK")

# ---- 数据在 %LOCALAPPDATA% 的话（程序文件夹里没有 data）：更新记录写到那边，不在程序文件夹里另起一个 data
target, source = make_dirs("local-data")
shutil.rmtree(target / "data")
local = WORK / "localappdata" / "BiliAutoDanmaku"
write(local / "config.json", "我的数据")
assert updater.install_main([str(target), dead_pid, "1.0.0"], source=source) == 0, messages
assert "文件替换完成" in (local / "logs" / "update.log").read_text(encoding="utf-8")
assert not (target / "data").exists()
print("数据在 LOCALAPPDATA OK")

# ---- 检查压缩包：缺 exe、路径不正常、exe 不是本程序
def make_zip(name: str, entries: dict[str, bytes]) -> Path:
    path = WORK / name
    with zipfile.ZipFile(path, "w") as zf:
        for entry, data in entries.items():
            zf.writestr(entry, data)
    return path


install = WORK / "install"
install.mkdir()
for name, entries, expect in [
    ("no_exe.zip", {"B站弹幕自动化/_internal/a.txt": b"x"}, "没有"),
    ("evil.zip", {f"B站弹幕自动化/{EXE_NAME}": b"x", "B站弹幕自动化/_internal/a": b"x", "B站弹幕自动化/../../evil.txt": b"x"}, "不正常"),
    ("other.zip", {f"B站弹幕自动化/{EXE_NAME}": Path(sys.executable).read_bytes(), "B站弹幕自动化/_internal/a": b"x"}, "不是本程序"),
]:
    try:
        updater.prepare_from_zip(make_zip(name, entries), install)
    except UpdateError as e:
        assert expect in str(e), (name, e)
    else:
        raise AssertionError(f"{name} 应该被拒绝")
    assert not (install / updater.UPDATE_DIR).exists(), "检查不通过时要清掉解压出来的东西"
assert not (WORK / "evil.txt").exists() and not (ROOT / "evil.txt").exists()
print("压缩包检查 OK")

real_zip = ROOT / "dist" / f"BiliAutoDanmaku-v{APP_VERSION}-win-x64.zip"
if real_zip.exists():
    staged = updater.prepare_from_zip(real_zip, install)
    assert staged.version == APP_VERSION and staged.exe.exists(), staged
    print(f"真实压缩包 OK：版本 {staged.version}")
    updater.discard(install)

shutil.rmtree(WORK, ignore_errors=True)
print("全部通过")
