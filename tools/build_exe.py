"""把程序打包成 exe（文件夹版），并压缩成 zip。由“打包.cmd”调用，也可以在装好 PyInstaller 的环境里直接运行。

结果：
  dist\\B站弹幕自动化\\B站弹幕自动化.exe     双击就能运行，数据存在旁边的 data 文件夹
  dist\\BiliAutoDanmaku-v版本号-win-x64.zip  发布用的压缩包（文件名用英文，GitHub 上不会被改名）
"""

from __future__ import annotations

import platform
import shutil
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
# Qt 的 Python 包里没带 LGPL 原文，用仓库 licenses 文件夹里的
QT_PACKAGES = {"pyside6-essentials", "shiboken6"}
QT_LICENSE_FILES = ("LGPL-3.0.txt", "GPL-3.0.txt")


def make_icon(path: Path) -> None:
    """用程序里画图标的代码生成 exe 的图标（.ico 里放多种尺寸的 PNG，小尺寸和大尺寸画法不同）。

    每个尺寸直接按像素画，不经过 QIcon.pixmap：高分屏上那样拿到的图会按缩放比例放大，尺寸对不上。
    """
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWidgets import QApplication

    from app.ui_common import APP_ICON_SIZES, app_icon_pixmap

    app = QApplication.instance() or QApplication([])
    images: list[tuple[int, bytes]] = []
    for size in APP_ICON_SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        app_icon_pixmap(size).save(buffer, "PNG")
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


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def runtime_distributions() -> list:
    """requirements.txt 里的包，加上它们在 Windows 上要用到的依赖包（也就是会被打包进 exe 的那些）。"""
    from importlib.metadata import PackageNotFoundError, distribution

    from packaging.requirements import Requirement

    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    todo = [Requirement(line).name for line in lines if line.strip() and not line.startswith("#")]
    found = {}
    while todo:
        name = _canonical(todo.pop())
        if name in found:
            continue
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        found[name] = dist
        for spec in dist.requires or []:
            req = Requirement(spec)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return sorted(found.values(), key=lambda d: d.metadata["Name"].lower())


def _license_name(dist) -> str:
    meta = dist.metadata
    name = meta.get("License-Expression") or ""
    if not name and len(meta.get("License") or "") <= 40:  # 有的包把整篇许可证塞在这一栏里
        name = meta.get("License") or ""
    if not name:
        classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License :: ")]
        name = classifiers[0].split(" :: ")[-1] if classifiers else ""
    return name or "见许可文件"


def collect_licenses(app_dir: Path) -> None:
    """把本程序和 exe 里随附的第三方组件的许可证放进 licenses 文件夹，再写一份清单。"""
    import PyInstaller

    target = app_dir / "licenses"
    target.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", target / f"{APP_TITLE}-LICENSE.txt")
    for name in QT_LICENSE_FILES:
        shutil.copyfile(ROOT / "licenses" / name, target / name)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", target / "Python-LICENSE.txt")
    rows = [("Python（含 OpenSSL、SQLite 等）", platform.python_version(), "PSF-2.0 等", "Python-LICENSE.txt")]

    for dist in runtime_distributions():
        name = dist.metadata["Name"]
        if _canonical(name) in QT_PACKAGES:
            rows.append((name, dist.version, "LGPL-3.0-only", "、".join(QT_LICENSE_FILES)))
            continue
        copied = []
        for file in dist.files or []:
            if any(key in file.name.upper() for key in ("LICENSE", "COPYING", "NOTICE")):
                dest = f"{name}-{file.name}"
                shutil.copyfile(file.locate(), target / dest)
                copied.append(dest)
        rows.append((name, dist.version, _license_name(dist), "、".join(copied)))

    pyinstaller_dir = Path(PyInstaller.__file__).parent
    copying = next(pyinstaller_dir.parent.glob("pyinstaller-*.dist-info/licenses/COPYING.txt"), None)
    if copying:
        shutil.copyfile(copying, target / "PyInstaller-COPYING.txt")
    rows.append(("PyInstaller（exe 的启动部分）", PyInstaller.__version__, "GPL-2.0-or-later，附带允许用于任何程序的例外条款", "PyInstaller-COPYING.txt"))

    table = "\n".join(f"- {name} {version}：{license_name}（{files}）" for name, version, license_name, files in rows)
    (target / "许可说明.txt").write_text(
        f"""{APP_TITLE} {APP_VERSION}

本程序以 MIT 许可证发布，见 {APP_TITLE}-LICENSE.txt。

程序里随附了下面这些第三方组件，各自按自己的许可证授权，括号里是许可文件：

{table}

关于 Qt（PySide6、shiboken6）：
按 LGPL-3.0 授权使用，没有做任何修改，许可原文见 LGPL-3.0.txt 和 GPL-3.0.txt。
它们是独立的 DLL / .pyd 文件，放在 _internal\\PySide6 和 _internal\\shiboken6 文件夹里，可以替换成其他兼容版本。
源代码可以从 Qt 官方获取：https://code.qt.io（Qt 和 PySide6）、https://download.qt.io/official_releases/QtForPython/
""",
        encoding="utf-8",
    )


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
    collect_licenses(app_dir)
    archive = make_zip(app_dir)
    print()
    print(f"打包完成：{app_dir / (APP_TITLE + '.exe')}")
    print(f"  文件夹大小 {folder_size(app_dir) / 2**20:.1f} MB（删掉用不到的文件省下 {saved / 2**20:.1f} MB）")
    print(f"  压缩包 {archive.name}：{archive.stat().st_size / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
