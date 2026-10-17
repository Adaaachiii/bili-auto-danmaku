"""把程序打包成 exe（文件夹版），并压缩成 zip。由“打包.cmd”调用，也可以在装好 PyInstaller 的环境里直接运行。

结果：
  dist\\B站弹幕自动化\\B站弹幕自动化.exe     双击就能运行，数据存在旁边的 data 文件夹
  dist\\BiliAutoDanmaku-v版本号-win-x64.zip  发布用的压缩包（文件名用英文，GitHub 上不会被改名）
"""

from __future__ import annotations

import os
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT))

from app.config import APP_TITLE, APP_VERSION  # noqa: E402

# Qt 自带几十种语言的翻译，程序只用简体中文这一个（加载的是 qtbase）
KEEP_TRANSLATIONS = {"qtbase_zh_CN.qm"}
# 软件模拟的 OpenGL，只有用到 OpenGL 的程序才需要，这个程序全是普通窗口控件
DROP_FILES = {"opengl32sw.dll"}


def make_icon(path: Path) -> None:
    """用程序里画图标的代码生成 exe 的图标（.ico 里放多种尺寸的 PNG）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWidgets import QApplication

    from app.ui_common import make_app_icon

    app = QApplication.instance() or QApplication([])
    icon = make_app_icon()
    images: list[tuple[int, bytes]] = []
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(size, size).save(buffer, "PNG")
        images.append((size, bytes(data)))
    del app

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size  # ICO 里 0 表示 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)


def make_version_file(path: Path) -> None:
    """exe 文件“属性 → 详细信息”里显示的名称和版本。"""
    numbers = tuple(int(n) for n in APP_VERSION.split(".")) + (0,) * (4 - len(APP_VERSION.split(".")))
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('FileDescription', '{APP_TITLE}'),
      StringStruct('ProductName', '{APP_TITLE}'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('ProductVersion', '{APP_VERSION}'),
      StringStruct('OriginalFilename', '{APP_TITLE}.exe'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])]),
  ]
)
""",
        encoding="utf-8",
    )


def slim(app_dir: Path) -> int:
    """删掉用不到的文件，返回省下的字节数。"""
    saved = 0
    for file in app_dir.rglob("*"):
        if not file.is_file():
            continue
        unused_translation = file.suffix == ".qm" and file.parent.name == "translations" and file.name not in KEEP_TRANSLATIONS
        if unused_translation or file.name.lower() in DROP_FILES:
            saved += file.stat().st_size
            file.unlink()
    return saved


def make_zip(app_dir: Path) -> Path:
    archive = DIST / f"BiliAutoDanmaku-v{APP_VERSION}-win-x64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file in sorted(app_dir.rglob("*")):
            if file.is_file():
                zf.write(file, Path(app_dir.name) / file.relative_to(app_dir))
    return archive


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    import PyInstaller.__main__

    BUILD.mkdir(exist_ok=True)
    icon = BUILD / "icon.ico"
    version_file = BUILD / "version_info.txt"
    make_icon(icon)
    make_version_file(version_file)

    app_dir = DIST / APP_TITLE
    if (app_dir / "data").exists():
        sys.exit(f"{app_dir} 里有 data 文件夹（像是有人直接在这里运行过），为免误删请先把它挪走再打包。")

    PyInstaller.__main__.run(
        [
            str(ROOT / "main.py"),
            "--name", APP_TITLE,
            "--windowed",
            "--noconfirm",
            "--clean",
            "--icon", str(icon),
            "--version-file", str(version_file),
            "--distpath", str(DIST),
            "--workpath", str(BUILD / "work"),
            "--specpath", str(BUILD),
            "--exclude-module", "tkinter",
        ]
    )

    saved = slim(app_dir)
    archive = make_zip(app_dir)
    print()
    print(f"打包完成：{app_dir / (APP_TITLE + '.exe')}")
    print(f"  文件夹大小 {folder_size(app_dir) / 2**20:.1f} MB（删掉用不到的文件省下 {saved / 2**20:.1f} MB）")
    print(f"  压缩包 {archive.name}：{archive.stat().st_size / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
