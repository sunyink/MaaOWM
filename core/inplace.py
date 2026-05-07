"""
core/inplace.py — 挂载 / 卸载主流程

挂载 (mount):
  1. 检测 mod 包当前状态, 备份原 mod  (.maaowm/mod_og_<ts>/)
  2. canonicalize(base_layers) → canonical_base
  3. 写快照 .maaowm/snapshot.json
  4. canonicalize_overlay(base_layers + mod) → canonical_merged (mount 内容)
  5. 建 origin 索引 .maaowm/origin.json
  6. 清空 mod 包的 pipeline 目录
  7. 按 origin 分组写入 canonical_merged → workspace
  8. 写 __OWM_README__.md 提醒纪律

卸载 (unmount):
  1. 检测挂载状态 (.maaowm/snapshot.json 存在)
  2. 备份当前 workspace  (.maaowm/work_<ts>/)
  3. canonicalize(workspace) → canonical_w
  4. 读 snapshot → canonical_base
  5. compute_minimal_mod(canonical_w, canonical_base) → minimal_mod
  6. 读 origin 索引
  7. 清空 mod 包 pipeline 目录
  8. 按 origin 分组写 minimal_mod → mod
  9. 删除 snapshot 和 origin 索引 (挂载状态结束)
  10. 留下日志, 备份保留供恢复
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import shutil
import sys
from typing import Callable, Dict, List, Optional

from . import config as config_mod
from . import diff
from . import oracle
from . import routing
from . import snapshot

ProgressCb = Optional[Callable[[str], None]]


OWM_README_TEXT = """\
# MaaOWM 工作区

此目录由 MaaOWM 挂载生成。每个 task 都已展开为 canonical 全字段 (V2) 形态。

## 编辑准则

  ✓ 修改字段值              (post_delay: 3000 → 200)
  ✓ 给 task 添加新字段
  ✓ 新建 task

  ✗ 不要从 task 里删除字段
     原因: 工作区独立加载时, 缺失字段会退到框架默认。
           你的"删除"实际效果可能是"改成默认值", 与你的预期不符。
     做法: 如需还原 base 的某字段, 请直接把值改成你期望的形态。

  ✗ 不要随意删除被引用的 task
     原因: next/on_error 引用不存在的 task 名时, MaaFW 会拒绝加载。
     做法: 如需让 task 失效, 在原位加 "enabled": false 而非删除。

## 文件归属

  挂载时记录的"task → 文件"归属在 .maaowm/origin.json。
  卸载时按此索引把每个 task 写回原位置, 保持 mod 包的目录结构。

## 备份

  挂载/卸载前的状态都备份在 .maaowm/ 下, 时间戳目录形式。
  误操作可从备份恢复。
"""


# ============================================================
# 挂载状态检测
# ============================================================

def is_mounted(cfg: config_mod.OverlayConfig) -> bool:
    """通过 .maaowm/snapshot.json 是否存在来判断挂载状态。"""
    return (cfg.owm_dir / snapshot.SNAPSHOT_FILENAME).exists()


def get_mount_info(cfg: config_mod.OverlayConfig) -> Optional[Dict[str, str]]:
    """读取挂载元数据 (用于 TUI 显示)。未挂载返回 None。"""
    if not is_mounted(cfg):
        return None
    try:
        snap = snapshot.read_snapshot(cfg.owm_dir)
    except snapshot.SnapshotError:
        return None
    ts = snap.mount_ts
    try:
        readable = datetime.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        readable = ts
    return {
        "mount_ts": ts,
        "mount_ts_readable": readable,
        "task_count": str(len(snap.canonical_base)),
    }


# ============================================================
# 备份
# ============================================================

def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _backup_dir(cfg: config_mod.OverlayConfig, kind: str) -> pathlib.Path:
    """返回 .maaowm/<kind>_<ts>/ 路径。kind in {'mod_og', 'work'}。"""
    return cfg.owm_dir / f"{kind}_{_timestamp()}"


def _copy_pipeline_only(src: pathlib.Path, dst: pathlib.Path) -> int:
    """只拷 pipeline 子目录的 JSON。返回拷贝的文件数。"""
    if not src.is_dir():
        return 0
    count = 0
    for p in src.rglob('*'):
        if not p.is_file():
            continue
        if any(part.startswith('.') for part in p.relative_to(src).parts):
            continue
        if p.suffix.lower() not in ('.json', '.jsonc'):
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        count += 1
    return count


def _clean_pipeline_dir(pipeline_dir: pathlib.Path) -> int:
    """清空 pipeline 目录下的 .json/.jsonc (保留其他文件如 .gitignore)。返回清理的文件数。"""
    if not pipeline_dir.is_dir():
        return 0
    count = 0
    for p in pipeline_dir.rglob('*'):
        if not p.is_file():
            continue
        if any(part.startswith('.') for part in p.relative_to(pipeline_dir).parts):
            continue
        if p.suffix.lower() in ('.json', '.jsonc'):
            p.unlink()
            count += 1
    # 清空空目录 (但保留 pipeline_dir 本身)
    for p in sorted(pipeline_dir.rglob('*'), reverse=True):
        if p.is_dir() and not any(p.iterdir()):
            try:
                p.rmdir()
            except OSError:
                pass
    return count


# ============================================================
# 操作日志
# ============================================================

def _log(cfg: config_mod.OverlayConfig, line: str) -> None:
    log = cfg.owm_dir / "operations.log"
    cfg.owm_dir.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {line}\n")


def get_log_lines(cfg: config_mod.OverlayConfig, max_lines: int = 50) -> List[str]:
    log = cfg.owm_dir / "operations.log"
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return lines[-max_lines:]


def get_backup_list(cfg: config_mod.OverlayConfig) -> List[Dict[str, str]]:
    """列出 .maaowm/ 下的备份目录, 给 TUI 用。"""
    if not cfg.owm_dir.is_dir():
        return []
    out = []
    for p in cfg.owm_dir.iterdir():
        if not p.is_dir():
            continue
        if not (p.name.startswith("mod_og_") or p.name.startswith("work_")):
            continue
        kind = "mod_og" if p.name.startswith("mod_og_") else "work"
        mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        out.append({
            "name": p.name,
            "kind": kind,
            "mtime_str": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            "path": str(p),
        })
    out.sort(key=lambda x: x["mtime_str"], reverse=True)
    return out


# ============================================================
# 结果数据结构
# ============================================================

@dataclasses.dataclass
class MountResult:
    og_backup: str            # mod_og 备份目录名
    snapshot_path: str        # snapshot.json 路径
    task_count: int           # canonical_merged 总 task 数
    workspace_files: int      # 写入工作区的文件数
    warnings: List[str]

    def summary(self) -> str:
        return (
            f"workspace: {self.workspace_files} 文件 / {self.task_count} task | "
            f"备份: {self.og_backup}"
        )


@dataclasses.dataclass
class UnmountResult:
    work_backup: str          # work 备份目录名
    minimal_mod_files: int    # 写回 mod 的文件数
    counts: Dict[str, int]    # diff decisions 统计
    hints: List[diff.Hint]
    warnings: List[str]

    def summary(self) -> str:
        return (
            f"mod: {self.minimal_mod_files} 文件 | "
            f"diff: {self.counts.get('MODIFIED',0)} mod / "
            f"{self.counts.get('MOD_ONLY',0)} new / "
            f"{self.counts.get('IDENTICAL',0)} stripped"
        )


class MountError(Exception):
    pass


class UnmountError(Exception):
    pass


# ============================================================
# 挂载主流程
# ============================================================

ORIGIN_INDEX_FILENAME = "origin.json"


def mount(
    cfg: config_mod.OverlayConfig,
    progress_callback: ProgressCb = None,
) -> MountResult:
    """挂载: 备份 mod, 把 base+mod 合并写回 mod 作为工作区。"""
    cb = progress_callback or (lambda s: None)
    warnings: List[str] = []

    if is_mounted(cfg):
        raise MountError("当前已处于挂载状态。请先卸载或删除 .maaowm/snapshot.json")

    # 初始化 oracle (DLL 加载)
    cb("初始化 MaaFW oracle...")
    try:
        pkg = oracle.init(cfg.maa_pkg_dir)
    except oracle.OracleError as e:
        raise MountError(str(e)) from None
    cb(f"  使用 maa 包: {pkg}")

    # 备份原 mod 包
    cb("备份原 mod 包 (mod_og)...")
    og_dir = _backup_dir(cfg, "mod_og")
    og_count = _copy_pipeline_only(cfg.workspace_pipeline_dir(), og_dir / cfg.pipeline_subdir)
    cb(f"  备份: {og_dir.name} ({og_count} 文件)")

    # canonicalize base
    cb("canonicalize base 层...")
    try:
        canonical_base = oracle.canonicalize_overlay(*cfg.base_pipeline_dirs())
    except oracle.OracleError as e:
        raise MountError(f"加载 base 失败: {e}") from None
    cb(f"  base canonical: {len(canonical_base)} task")

    # 写快照
    cb("写挂载快照 snapshot.json...")
    snap = snapshot.make_snapshot(canonical_base, cfg.base_pipeline_dirs())
    snap_path = snapshot.write_snapshot(snap, cfg.owm_dir)
    cb(f"  快照: {snap_path.name}")

    # canonicalize base + mod (要的是合并后的工作区内容)
    cb("canonicalize base + mod (生成工作区内容)...")
    try:
        canonical_merged = oracle.canonicalize_overlay(
            *cfg.base_pipeline_dirs(), cfg.workspace_pipeline_dir()
        )
    except oracle.OracleError as e:
        raise MountError(f"加载 base+mod 合并失败: {e}") from None
    cb(f"  合并 canonical: {len(canonical_merged)} task")

    # 建 origin 索引 — 注意此时 mod 还是原状 (清空前)
    cb("建立 origin 索引...")
    # 用各层中实际的 pipeline 子目录建索引
    base_origin: Dict[str, str] = {}
    for bp in cfg.base_pipeline_dirs():
        for name, path in oracle.list_node_names_with_origin(bp).items():
            if name not in base_origin:
                base_origin[name] = path.relative_to(bp).as_posix()
    mod_origin: Dict[str, str] = {}
    for name, path in oracle.list_node_names_with_origin(cfg.workspace_pipeline_dir()).items():
        mod_origin[name] = path.relative_to(cfg.workspace_pipeline_dir()).as_posix()
    index = routing.OriginIndex(mod_origin=mod_origin, base_origin=base_origin)
    (cfg.owm_dir / ORIGIN_INDEX_FILENAME).write_text(index.to_json(), encoding="utf-8")
    cb(f"  origin 索引: base {len(base_origin)} / mod {len(mod_origin)}")

    # 清空 mod 的 pipeline 目录, 写入合并 canonical
    cb("清空 mod 包 pipeline 目录, 写入 canonical 工作区...")
    cleaned = _clean_pipeline_dir(cfg.workspace_pipeline_dir())
    cb(f"  清理: {cleaned} 旧文件")

    grouped = routing.group_by_target_file(canonical_merged, index)
    written = routing.write_mod_files(grouped, cfg.workspace_pipeline_dir())
    cb(f"  写入: {len(written)} 文件")

    # 写 README
    readme = cfg.workspace_dir / "__OWM_README__.md"
    readme.write_text(OWM_README_TEXT, encoding="utf-8")

    _log(cfg, f"[MOUNT-OK] tasks={len(canonical_merged)} files={len(written)} backup={og_dir.name}")

    return MountResult(
        og_backup=og_dir.name,
        snapshot_path=str(snap_path),
        task_count=len(canonical_merged),
        workspace_files=len(written),
        warnings=warnings,
    )


# ============================================================
# 卸载主流程
# ============================================================

def unmount(
    cfg: config_mod.OverlayConfig,
    progress_callback: ProgressCb = None,
) -> UnmountResult:
    """卸载: 备份 work, diff 出 minimal mod, 写回 mod 包。"""
    cb = progress_callback or (lambda s: None)
    warnings: List[str] = []

    if not is_mounted(cfg):
        raise UnmountError("当前未处于挂载状态 (.maaowm/snapshot.json 不存在)")

    # 初始化 oracle
    cb("初始化 MaaFW oracle...")
    try:
        oracle.init(cfg.maa_pkg_dir)
    except oracle.OracleError as e:
        raise UnmountError(str(e)) from None

    # 备份当前工作区
    cb("备份当前工作区 (work)...")
    work_backup = _backup_dir(cfg, "work")
    work_count = _copy_pipeline_only(cfg.workspace_pipeline_dir(), work_backup / cfg.pipeline_subdir)
    cb(f"  备份: {work_backup.name} ({work_count} 文件)")

    # canonicalize 工作区
    cb("canonicalize 工作区...")
    try:
        canonical_w = oracle.canonicalize(cfg.workspace_pipeline_dir())
    except oracle.OracleError as e:
        raise UnmountError(f"加载工作区失败: {e}") from None
    cb(f"  workspace canonical: {len(canonical_w)} task")

    # 读快照
    cb("读取挂载快照...")
    try:
        snap = snapshot.read_snapshot(cfg.owm_dir)
    except snapshot.SnapshotError as e:
        raise UnmountError(str(e)) from None
    canonical_base = snap.canonical_base
    cb(f"  快照 canonical_base: {len(canonical_base)} task ({snap.mount_ts})")

    # diff
    cb("计算 minimal mod...")
    diff_result = diff.compute_minimal_mod(canonical_w, canonical_base)
    counts = diff_result.counts()
    cb(f"  diff: {counts}")

    hints = diff.detect_hints(diff_result)
    if hints:
        cb(f"  hints: {len(hints)} 条")

    # 读 origin 索引
    cb("读取 origin 索引...")
    origin_path = cfg.owm_dir / ORIGIN_INDEX_FILENAME
    if origin_path.exists():
        index = routing.OriginIndex.from_json(origin_path.read_text(encoding="utf-8"))
    else:
        warnings.append("origin 索引缺失, 用当前 base 重建 (可能与挂载时不一致)")
        # 临时重建
        base_origin: Dict[str, str] = {}
        for bp in cfg.base_pipeline_dirs():
            for name, path in oracle.list_node_names_with_origin(bp).items():
                if name not in base_origin:
                    base_origin[name] = path.relative_to(bp).as_posix()
        index = routing.OriginIndex(mod_origin={}, base_origin=base_origin)

    # 清空 mod, 写 minimal_mod
    cb("清空 mod 包 pipeline 目录, 写入 minimal mod...")
    cleaned = _clean_pipeline_dir(cfg.workspace_pipeline_dir())
    cb(f"  清理: {cleaned} 文件")

    grouped = routing.group_by_target_file(diff_result.minimal_mod, index)
    written = routing.write_mod_files(grouped, cfg.workspace_pipeline_dir())
    cb(f"  写入: {len(written)} mod 文件")

    # 删除 README
    readme = cfg.workspace_dir / "__OWM_README__.md"
    if readme.exists():
        readme.unlink()

    # 删除 snapshot 和 origin 索引 (挂载状态结束)
    (cfg.owm_dir / snapshot.SNAPSHOT_FILENAME).unlink(missing_ok=True)
    origin_path.unlink(missing_ok=True)

    _log(cfg, f"[UNMOUNT-OK] mod_files={len(written)} counts={counts} backup={work_backup.name}")

    return UnmountResult(
        work_backup=work_backup.name,
        minimal_mod_files=len(written),
        counts=counts,
        hints=hints,
        warnings=warnings,
    )
