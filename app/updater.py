"""从压缩包更新（只用于打包好的 exe 版）。

1. 正在运行的程序把选中的压缩包解压到程序文件夹下的 _update\\new，检查确实是本程序、读出版本号；
2. 启动解压出来的新版 exe（带 --install-update 参数），自己退出；
3. 新版 exe 等旧程序退出后，把旧的程序文件挪进 _update\\old，再把新的复制过去，然后启动新版本。
   中途出错就把旧文件挪回去，旧版本照样能用；
4. 新版本启动后删掉 _update。

data 文件夹（直播间、收藏夹、历史记录等）全程不碰。程序文件夹不能写入时（比如放在 C:\\Program Files）没法用这个功能，
第 1 步就会提示。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .config import APP_TITLE, APP_VERSION, data_dir_for

EXE_NAME = f"{APP_TITLE}.exe"
UPDATE_DIR = "_update"
KEEP = {"data", UPDATE_DIR}  # 更新时不动的东西
_DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


class UpdateError(Exception):
    pass


@dataclass
class StagedUpdate:
    version: str
    folder: Path  # 解压出来的新版本，里面有 exe

    @property
    def exe(self) -> Path:
        return self.folder / EXE_NAME


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent


def version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def exe_info(path: Path) -> tuple[str, str] | None:
    """读 exe 文件属性里的（产品名称, 版本号），读不到返回 None。"""
    api = ctypes.windll.version
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None

    def query(key: str) -> ctypes.c_void_p | None:
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        return pointer if api.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(length)) and length.value else None

    fixed = query("\\")
    name = query("\\StringFileInfo\\080404b0\\ProductName")
    if fixed is None or name is None:
        return None
    words = ctypes.cast(fixed, ctypes.POINTER(wintypes.DWORD * 4)).contents  # 签名、结构版本、版本号高位、低位
    version = f"{words[2] >> 16}.{words[2] & 0xFFFF}.{words[3] >> 16}"
    return ctypes.wstring_at(name.value), version


# ---------------------------------------------------------------- 第 1、2 步：在旧程序里运行


def prepare_from_zip(zip_path: Path, target: Path) -> StagedUpdate:
    """检查并解压压缩包，返回解压出来的新版本。出问题抛 UpdateError（说明是给人看的）。"""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise UpdateError(f"打不开这个压缩包：{e}") from e
    with archive:
        names = archive.namelist()
        exe_entries = [n for n in names if n.split("/")[-1] == EXE_NAME and n.count("/") <= 1]
        if not exe_entries:
            raise UpdateError(f"压缩包里没有 {EXE_NAME}，请选从 GitHub Releases 下载的 BiliAutoDanmaku-v版本号-win-x64.zip。")
        prefix = exe_entries[0][: -len(EXE_NAME)]  # 一般是“B站弹幕自动化/”
        if not any(n.startswith(prefix + "_internal/") for n in names):
            raise UpdateError("压缩包不完整（缺少 _internal 文件夹），请重新下载。")

        staging = target / UPDATE_DIR
        folder = staging / "new"
        try:
            shutil.rmtree(staging, ignore_errors=True)
            folder.mkdir(parents=True)
        except OSError as e:
            raise UpdateError(
                f"程序所在的文件夹不能写入，没法自动更新：{e}\n"
                "可以退出程序，把整个程序文件夹挪到 D 盘等有写入权限的位置再更新。"
            ) from e
        root = folder.resolve()
        try:
            for name in names:
                if not name.startswith(prefix) or name == prefix:
                    continue
                dest = (folder / name[len(prefix):]).resolve()
                if root not in dest.parents:
                    raise UpdateError("压缩包里有不正常的文件路径，已停止。")
                if name.endswith("/"):
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        except UpdateError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except (OSError, zipfile.BadZipFile) as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateError(f"解压失败：{e}") from e

    info = exe_info(folder / EXE_NAME)
    if info is None or info[0] != APP_TITLE:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"压缩包里的 {EXE_NAME} 不是本程序。")
    return StagedUpdate(version=info[1], folder=folder)


def discard(target: Path) -> None:
    shutil.rmtree(target / UPDATE_DIR, ignore_errors=True)


def launch_install(staged: StagedUpdate, target: Path, wait_pid: int | None = None) -> None:
    """启动新版 exe 去替换文件。调用之后旧程序要尽快退出，新版会等它退出再动手。"""
    subprocess.Popen(
        [str(staged.exe), "--install-update", str(target), str(wait_pid or os.getpid()), APP_VERSION],
        cwd=str(staged.folder),
        creationflags=_DETACHED,
        close_fds=True,
    )


# ---------------------------------------------------------------- 第 3 步：在解压出来的新版 exe 里运行


def install_main(args: list[str], source: Path | None = None) -> int:
    """参数：程序文件夹、旧程序的进程号、旧版本号。source 是新版本所在的文件夹，默认就是自己所在的文件夹。"""
    target, wait_pid, old_version = Path(args[0]), int(args[1]), args[2]
    try:  # 和程序用同一个数据文件夹（不一定在程序文件夹里，见 config.resolve_data_dir）
        log_file = data_dir_for(target)[0] / "logs" / "update.log"
    except OSError:
        log_file = target / UPDATE_DIR / "update.log"

    def log(text: str) -> None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    log(f"开始从 {old_version} 更新到 {APP_VERSION}")
    if not _wait_for_exit(wait_pid, timeout_ms=60_000):
        log("旧程序一分钟内没有退出，放弃更新")
        _message("旧程序没有退出，这次没有更新。请退出程序后重新选择压缩包更新。")
        return 1

    source = source or Path(sys.executable).resolve().parent
    items =[p.name for p in source.iterdir() if p.name not in KEEP]
    backup = target / UPDATE_DIR / "old"
    moved: list[str] = []
    copied: list[str] = []
    try:
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        for name in items:  # 旧文件先挪开（同一个盘里挪动很快，出错还能挪回来）
            if (target / name).exists():
                _retry(lambda: os.replace(target / name, backup / name))
                moved.append(name)
        for name in items:
            src, dest = source / name, target / name
            copied.append(name)
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)
    except Exception:
        log("更新失败，恢复旧版本：\n" + traceback.format_exc())
        for name in copied:
            path = target / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        for name in moved:
            try:
                os.replace(backup / name, target / name)
            except OSError:
                log(f"恢复 {name} 失败")
        _message(f"更新失败，已恢复旧版本。\n原因：{traceback.format_exc(limit=1).strip().splitlines()[-1]}\n详情见 {log_file}")
        _start(target, [])
        return 1

    log("文件替换完成，启动新版本")
    _start(target, ["--updated-from", old_version])
    return 0


def _retry(action, attempts: int = 20) -> None:
    """杀毒软件扫描时文件可能被短暂占用，等一下再试。"""
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def _wait_for_exit(pid: int, timeout_ms: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # 已经退出了
    try:
        return kernel32.WaitForSingleObject(wintypes.HANDLE(handle), timeout_ms) == 0
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _start(target: Path, extra: list[str]) -> None:
    exe = target / EXE_NAME
    if exe.exists():
        subprocess.Popen([str(exe), *extra], cwd=str(target), creationflags=_DETACHED, close_fds=True)


def _message(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, f"{APP_TITLE} 更新", 0x10)


# ---------------------------------------------------------------- 第 4 步：新版本启动后


def cleanup(target: Path) -> bool:
    """删掉更新用的临时文件夹，删干净了返回 True。"""
    folder = target / UPDATE_DIR
    if not folder.exists():
        return True
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()
