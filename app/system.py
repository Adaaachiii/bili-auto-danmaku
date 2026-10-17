"""和 Windows 系统打交道的部分：开机自启、打开浏览器。"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
import winreg
from pathlib import Path

from .config import APP_ID, BROWSER_CHROME, BROWSER_DEFAULT, BROWSER_EDGE

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _launch_command() -> str:
    """开机自启时执行的命令：用 pythonw 启动（不弹黑色命令行窗口），并直接缩小到托盘。"""
    if getattr(sys, "frozen", False):  # 打包成 exe 之后
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    return f'"{exe}" "{PROJECT_DIR / "main.py"}" --minimized'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_ID)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool) -> None:
    """写入或删除当前用户的开机启动项（注册表 HKCU\\...\\Run，不需要管理员权限）。"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass


def refresh_autostart() -> None:
    """程序文件夹搬过位置的话，开机启动项里的路径会失效，启动时顺手更新一下。"""
    if is_autostart_enabled():
        set_autostart(True)


def live_room_url(room_id: int) -> str:
    return f"https://live.bilibili.com/{room_id}"


_BROWSER_EXE = {
    BROWSER_EDGE: ("Microsoft", "Edge", "Application", "msedge.exe"),
    BROWSER_CHROME: ("Google", "Chrome", "Application", "chrome.exe"),
}


def _find_browser(browser: str) -> str | None:
    parts = _BROWSER_EXE.get(browser)
    if parts is None:
        return None
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            exe = Path(base).joinpath(*parts)
            if exe.exists():
                return str(exe)
    return None


def open_live_page(room_id: int, browser: str) -> bool:
    """打开直播页。找不到选定的浏览器时改用系统默认浏览器打开，并返回 False。"""
    url = live_room_url(room_id)
    exe = _find_browser(browser)
    if exe:
        subprocess.Popen([exe, url], close_fds=True)
        return True
    webbrowser.open(url)
    return browser == BROWSER_DEFAULT
