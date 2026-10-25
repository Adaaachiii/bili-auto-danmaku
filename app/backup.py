"""数据备份和恢复。

导出：把数据文件夹里的直播间和设置、收藏夹、历史记录、说书文字和进度、屏蔽词整理表打成一个 zip。
数据库用 SQLite 自己的备份接口复制，程序正在写也能得到完整的一份。运行日志和登录信息不在里面
（登录存在 Windows 凭据管理器里，换电脑要重新扫码）。

恢复：程序开着的时候数据库在用，不能直接替换。所以先把选中的备份复制成数据文件夹里的 _restore.zip
（stage_restore），然后重新打开程序；新打开的程序在打开数据库之前把它换上（apply_pending_restore）。
换之前先把现在的数据另存成数据文件夹里的“恢复前的数据-时间.zip”，换的过程中出错就恢复原样。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import APP_ID, APP_TITLE, APP_VERSION, data_dir

MANIFEST = "backup.json"
PENDING = "_restore.zip"  # 等程序重新打开时恢复的备份
PRE_RESTORE_PREFIX = "恢复前的数据-"
_STAGING, _OLD = "_restore_new", "_restore_old"  # 恢复过程中用的临时文件夹
SKIP_DIRS = {"logs", "cache", _STAGING, _OLD}  # cache 里是下载下来的表情图片，随时能重新下载
SKIP_SUFFIXES = (".tmp", ".zip", "-wal", "-shm", "-journal")
# 恢复时一定换掉的东西：备份里没有的话（比如那时还没有说书文字），现在的也要清掉，恢复出来才和备份一模一样
CORE_ITEMS = ("config.json", "progress.json", "favorites.db", "history.db", "stories", "被过滤的弹幕.csv")


class BackupError(Exception):
    """说明是给人看的。"""


@dataclass
class BackupInfo:
    created: str  # 导出的时间，比如“2026-10-25 21:00:00”
    version: str  # 导出时的程序版本
    files: list[str]  # zip 里的数据文件（不含 backup.json）


@dataclass
class RestoreResult:
    ok: bool
    message: str  # 给人看的结果
    previous: Path | None = None  # 恢复前的数据另存在哪


def default_name() -> str:
    return f"{APP_TITLE}备份-{time.strftime('%Y%m%d-%H%M%S')}.zip"


# ---------------------------------------------------------------- 导出


def _data_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or relative.parts[0] in SKIP_DIRS:
            continue
        if path.name.endswith(SKIP_SUFFIXES) or path.name.startswith(".write-test-"):
            continue
        files.append(path)
    return files


def _sqlite_snapshot(path: Path) -> bytes:
    """数据库的一份完整拷贝（包括还在 WAL 文件里、没写回主文件的内容）。"""
    source = sqlite3.connect(str(path))
    memory = sqlite3.connect(":memory:")
    try:
        source.backup(memory)
        return memory.serialize()
    finally:
        memory.close()
        source.close()


def export_backup(dest: Path, root: Path | None = None) -> BackupInfo:
    """把数据文件夹打包成 dest。先写临时文件，写完再改名，中途出错不会留下半个备份。"""
    root = root or data_dir()
    dest = Path(dest)
    temp = dest.with_name(dest.name + ".tmp")
    names: list[str] = []
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in _data_files(root):
                name = path.relative_to(root).as_posix()
                if path.suffix == ".db":
                    archive.writestr(name, _sqlite_snapshot(path))
                else:
                    archive.write(path, name)
                names.append(name)
            info = BackupInfo(time.strftime("%Y-%m-%d %H:%M:%S"), APP_VERSION, names)
            manifest = {"app": APP_ID, "version": info.version, "created": info.created, "files": names}
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temp, dest)
    except (OSError, sqlite3.Error) as e:
        temp.unlink(missing_ok=True)
        raise BackupError(f"导出备份失败：{e}") from e
    return info


# ---------------------------------------------------------------- 检查备份


def _safe(name: str) -> bool:
    """zip 里的路径只能落在数据文件夹里面。"""
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and ":" not in name and "\\" not in name


def read_backup(path: Path) -> BackupInfo:
    """检查是不是本程序导出的备份，返回备份信息。不是的话抛 BackupError。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if MANIFEST not in names:
                raise BackupError("这不是本程序导出的备份（压缩包里没有 backup.json）。")
            manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BackupError(f"打不开这个备份：{e}") from e
    if not isinstance(manifest, dict) or manifest.get("app") != APP_ID:
        raise BackupError("这不是本程序导出的备份。")
    files = [name for name in names if name != MANIFEST and not name.endswith("/")]
    if not all(_safe(name) for name in files):
        raise BackupError("备份里有不正常的文件路径，已停止。")
    if "config.json" not in files:
        raise BackupError("备份不完整（没有 config.json）。")
    return BackupInfo(str(manifest.get("created", "")), str(manifest.get("version", "")), files)


# ---------------------------------------------------------------- 恢复


def stage_restore(src: Path, root: Path | None = None) -> BackupInfo:
    """检查备份，复制成数据文件夹里的 _restore.zip，等程序重新打开时恢复。"""
    root = root or data_dir()
    info = read_backup(src)
    temp = root / (PENDING + ".tmp")
    try:
        shutil.copyfile(src, temp)
        os.replace(temp, root / PENDING)
    except OSError as e:
        temp.unlink(missing_ok=True)
        raise BackupError(f"准备恢复失败：{e}") from e
    return info


def has_pending_restore(root: Path | None = None) -> bool:
    return ((root or data_dir()) / PENDING).exists()


def apply_pending_restore(root: Path | None = None) -> RestoreResult | None:
    """程序启动、打开数据库之前调用：有等着恢复的备份就换上。没有返回 None。"""
    root = root or data_dir()
    pending = root / PENDING
    if not pending.exists():
        return None
    try:
        info = read_backup(pending)
    except BackupError as e:
        pending.unlink(missing_ok=True)
        return RestoreResult(False, f"没有恢复：{e}")

    previous = root / f"{PRE_RESTORE_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.zip"
    staging, old = root / _STAGING, root / _OLD
    try:
        export_backup(previous, root)  # 现在的数据先另存一份，恢复错了还能找回来
        for folder in (staging, old):
            shutil.rmtree(folder, ignore_errors=True)
        with zipfile.ZipFile(pending) as archive:  # 先全部解压到一边，解压失败不会动到现在的数据
            for name in info.files:
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except (OSError, zipfile.BadZipFile, BackupError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        pending.unlink(missing_ok=True)
        return RestoreResult(False, f"没有恢复：{e}", previous if previous.exists() else None)

    incoming = sorted({PurePosixPath(name).parts[0] for name in info.files})
    replaced = sorted(set(incoming) | set(CORE_ITEMS))
    moved: list[str] = []
    placed: list[str] = []
    try:
        old.mkdir()
        for name in replaced:  # 现在的数据挪开（数据库连同 -wal / -shm 一起）
            for variant in (name, name + "-wal", name + "-shm"):
                if (root / variant).exists():
                    os.replace(root / variant, old / variant)
                    moved.append(variant)
        for name in incoming:
            os.replace(staging / name, root / name)
            placed.append(name)
    except OSError as e:
        for name in placed:  # 换到一半出错：新的拿走，旧的放回去
            path = root / name
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
        stuck = []
        for name in moved:
            try:
                os.replace(old / name, root / name)
            except OSError:
                stuck.append(name)
        shutil.rmtree(staging, ignore_errors=True)
        pending.unlink(missing_ok=True)
        if stuck:  # 没能放回去的留在 _restore_old 里，不删，免得丢数据
            return RestoreResult(False, f"恢复失败：{e}。有些文件没能放回原处，在 {old} 里：{'、'.join(stuck)}", previous)
        shutil.rmtree(old, ignore_errors=True)
        return RestoreResult(False, f"恢复失败，数据保持原样：{e}", previous)

    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    pending.unlink(missing_ok=True)
    when = f" {info.created} 导出的" if info.created else ""
    return RestoreResult(True, f"已恢复{when}备份（{len(info.files)} 个文件）。", previous)
