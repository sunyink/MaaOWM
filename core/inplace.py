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
from . import def_table
from . import diff
from . import extras as extras_mod
from . import oracle
from . import preflight
from . import routing
from . import snapshot
from . import translator

ProgressCb = Optional[Callable[[str], None]]


DEF_TABLES_FILENAME = "def_tables.json"
EXTRAS_FILENAME = "extras.json"


OWM_README_TEXT = """\
# MaaOWM 工作区

此目录由 MaaOWM 挂载生成。每个 task 已展开为 base+mod 合并后的 canonical
形态, 并按"省略默认值"原则做了精简:
  - 字段值等于框架默认值的, 不写出 (省屏占, 接近 base 简洁形态)
  - next/on_error 用紧凑字符串写法 (默认; 可在配置切换)
  - 输出格式默认 V2 (嵌套); 可切换 V1 (拍平), 看个人偏好
  - 保留 base/mod 中的注释字段 (doc, desc 等非 MaaFW 字段, V0.7.0+)
  - 节点顺序参考 base 原始顺序 (新建 task 排在文件末尾)

## 编辑准则

  ✓ 修改字段值              (post_delay: 3000 → 200)
  ✓ 给 task 添加新字段
  ✓ 新建 task
  ✓ 修改/新增 doc/desc 等注释字段 (会被持久化到 mod)

  ✗ 不要从 task 里删除字段, 妄图"恢复"成 base 的值
     原因: 工作区独立加载时, 缺失字段会退到框架默认值 (不是 base 的值)。
           你的"删除"实际效果是"改成框架默认", 与你的预期不符。
     做法: 如需还原 base 的某字段, 请直接把值改成你期望的形态。
     旁注: 你看到的工作区里没写的字段, 也不代表是 base 的值, 可能是
          框架默认 — 加载时 parser 会自动还原。

  ✗ 不要随意删除被引用的 task
     原因: next/on_error 引用不存在的 task 名时, MaaFW 会拒绝加载。
     做法: 如需让 task 失效, 在原位加 "enabled": false 而非删除。

## doc/desc 等注释字段

  这些字段不参与 MaaFW 运行, 但保留在工作区方便阅读和编辑.
  
  • 修改 doc 内容       → 卸载时持久化到 mod
  • 删除整行 doc 字段   → mod 也不写, 重新挂载时从 base 恢复 (相当于撤回修改)
  • 想真正"清空"注释   → 显式写 doc: ""

## 卸载时的安全检查

  按 [U] 卸载前, OWM 会自动跑一次预检 (oracle.canonicalize 模拟加载)。
  如果工作区有语法错误或字段类型错误 (例: TM 改 OCR 没改 threshold 写法),
  会阻断卸载并提示, 你可用 VSC + MAA support 插件查具体错误位置。

  随时可按 [C] 主动检查工作区状态, 看变动总览和文件分布。

## 文件归属

  挂载时记录的"task → 文件"归属在 .maaowm/origin.json。
  卸载时按此索引把每个 task 写回原位置, 保持 mod 包的目录结构。

## 备份

  挂载/卸载前的状态都备份在 .maaowm/ 下, 时间戳目录形式。
  误操作可从备份恢复 (按 [B] 查看)。
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
    if og_count == 0:
        cb(f"  备份: {og_dir.name} (0 文件, mod 目录为空 — 首次挂载场景)")
    else:
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

    # 探 def 表 (按 type 一一探针, 失败的静默跳过)
    cb("探 def 表 (按 type)...")
    base_for_probe = cfg.base_pipeline_dirs()[0]   # 用第一个 base 层做探针上下文
    def_tables = def_table.build_def_tables(base_for_probe)
    cb(
        f"  reco 白名单 ({len(def_tables.reco_param)}): "
        f"{sorted(def_tables.reco_param.keys())}"
    )
    cb(
        f"  action 白名单 ({len(def_tables.action_param)}): "
        f"{sorted(def_tables.action_param.keys())}"
    )
    if def_tables.failed_types:
        cb(
            f"  探针失败 ({len(def_tables.failed_types)}): "
            f"{def_tables.failed_types[:5]}"
            + ("..." if len(def_tables.failed_types) > 5 else "")
        )
    (cfg.owm_dir / DEF_TABLES_FILENAME).write_text(
        def_tables.to_json(), encoding="utf-8"
    )

    # 检测 mod 是否含 JSON (extras 扫描和后面 canonicalize 都用)
    mod_dir = cfg.workspace_pipeline_dir()
    mod_has_json = any(
        p.is_file() and p.suffix.lower() in (".json", ".jsonc")
        and not any(part.startswith(".") for part in p.relative_to(mod_dir).parts)
        for p in mod_dir.rglob("*")
    )

    # 扫 extras + 节点顺序 (V0.7.0): 必须在 mod 清空前完成
    # base 各层 + mod 覆盖式合并, 取得最终 extras 与 node_order
    cb("扫描 extras + 节点顺序 (base+mod 原始 JSON)...")
    extras_snap = extras_mod.collect_layered_extras(
        layers=cfg.base_pipeline_dirs(),
        mod_dir=mod_dir if mod_has_json else None,
        def_tables=def_tables,
    )
    cb(
        f"  extras: {len(extras_snap.extras)} task / "
        f"node_order: {len(extras_snap.node_order)} 文件"
    )
    (cfg.owm_dir / EXTRAS_FILENAME).write_text(
        extras_snap.to_json(), encoding="utf-8"
    )

    # canonicalize base + mod (要的是合并后的工作区内容)
    # 边界: mod 目录可能为空 (首次挂载常见). MaaFW 的 post_pipeline
    # 对空目录会报 load_all_json failed, 此时直接复用 canonical_base 即可。

    if not mod_has_json:
        cb("mod 目录为空, 跳过 mod 加载, 工作区 = base canonical")
        canonical_merged = canonical_base
    else:
        cb("canonicalize base + mod (生成工作区内容)...")
        try:
            canonical_merged = oracle.canonicalize_overlay(
                *cfg.base_pipeline_dirs(), mod_dir
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

    # 流水线: canonical → def 剥离 → V1 转译 → next 紧凑 → 写文件
    # 顺序约束:
    #   - def 剥离必须在 V1 转译之前 (strip 按 V2 type 查表)
    #   - next 紧凑独立, 放最后或最前都行 (放最后省一次遍历)
    pipeline_to_write = canonical_merged

    # def 剥离 (V0.6.0): 让工作区接近 base 简洁形态
    # round-trip 已实证闭合 (verify_workspace_minimal.py 通过)
    cb("  def 剥离 (按 type 白名单, 减少冗余字段)...")
    stripped = def_table.strip_mod_with_def(
        pipeline_to_write, def_tables, canonical_w=canonical_merged,
    )
    cb(f"  剥离 {stripped} 个 def 字段")

    # V1/V2 输出选择
    if cfg.output_format == "v1":
        cb("  转 V1 格式 (按 MPE 风格拍平)...")
        pipeline_to_write = translator.pipeline_v2_to_v1(pipeline_to_write)

    # next/on_error 紧凑写法 (默认开启, 与 V1/V2 正交)
    if cfg.compact_node_refs:
        cb("  next/on_error 紧凑写法...")
        translator.simplify_node_refs_in_pipeline(pipeline_to_write)

    # wait_freezes 紧凑写法 (V0.7.3): 仅 time 字段 → 标量
    cb("  wait_freezes 紧凑写法...")
    wf_simplified = translator.simplify_wait_freezes_in_pipeline(pipeline_to_write)
    if wf_simplified:
        cb(f"  简化 {wf_simplified} 个 wait_freezes 字段")

    # extras 注入 (V0.7.0): 把 doc/desc 等非 MaaFW 字段塞回每个 task
    cb("  注入 extras (doc/desc 等非 MaaFW 字段)...")
    injected = extras_mod.inject_extras_into_pipeline(pipeline_to_write, extras_snap)
    cb(f"  注入 {injected} 个 extras 字段")

    grouped = routing.group_by_target_file(pipeline_to_write, index)

    # 节点顺序重排 (V0.7.0): 每个文件按 base 中记录的顺序重排 task
    cb("  按 node_order 重排各文件...")
    grouped_reordered = {
        rel: extras_mod.reorder_pipeline_by_node_order(p, rel, extras_snap.node_order)
        for rel, p in grouped.items()
    }

    written = routing.write_mod_files(grouped_reordered, cfg.workspace_pipeline_dir())
    fmt_label = cfg.output_format.upper()
    if cfg.compact_node_refs:
        fmt_label += " + 紧凑 next"
    cb(f"  写入: {len(written)} 文件 [{fmt_label}]")

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

    # 预检 (失败阻断, 不动用户工作区, 不写备份)
    cb("预检工作区...")
    pre_result = preflight.validate_workspace(
        cfg, progress_callback=lambda m: cb(f"  {m}"),
    )
    if not pre_result.ok:
        raise UnmountError(
            f"{pre_result.summary}\n\n{pre_result.error_detail}"
        )
    cb(f"  ✓ 预检通过: {pre_result.summary}")

    # 备份当前工作区
    cb("备份当前工作区 (work)...")
    work_backup = _backup_dir(cfg, "work")
    work_count = _copy_pipeline_only(cfg.workspace_pipeline_dir(), work_backup / cfg.pipeline_subdir)
    cb(f"  备份: {work_backup.name} ({work_count} 文件)")

    # 读 def 表 (挂载时存的, 用于扫描 extras 字段判定)
    def_path = cfg.owm_dir / DEF_TABLES_FILENAME
    def_tables_for_extras = None
    if def_path.exists():
        try:
            def_tables_for_extras = def_table.DefTables.from_json(
                def_path.read_text(encoding="utf-8")
            )
        except Exception as e:
            warnings.append(f"读取挂载时 def 表失败 (extras 扫描跳过): {e}")

    # 扫工作区原始 JSON, 抓最新 extras (用户可能改了 doc/desc) (V0.7.0)
    workspace_extras: Optional[extras_mod.ExtrasSnapshot] = None
    workspace_node_order: Dict[str, List[str]] = {}
    if def_tables_for_extras is not None:
        cb("扫描工作区 extras (用户编辑后的最新状态)...")
        ws_extras_dict, ws_node_order = extras_mod.collect_extras_from_dir(
            cfg.workspace_pipeline_dir(), def_tables_for_extras,
        )
        workspace_extras = extras_mod.ExtrasSnapshot(
            extras=ws_extras_dict, node_order=ws_node_order,
        )
        workspace_node_order = ws_node_order
        cb(f"  extras: {len(ws_extras_dict)} task / 文件: {len(ws_node_order)}")

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

    # extras 变更检测 (V0.7.2): 用户单独改 doc/desc 也要写入 mod
    # 即使 oracle diff 看 IDENTICAL, 只要 extras 变了就强制入 mod
    extras_path = cfg.owm_dir / EXTRAS_FILENAME
    if extras_path.exists() and workspace_extras is not None:
        try:
            mount_extras = extras_mod.ExtrasSnapshot.from_json(
                extras_path.read_text(encoding="utf-8")
            )
            extras_changed = extras_mod.diff_extras(workspace_extras, mount_extras)
            # 把 extras 变更但不在 minimal_mod 的 task 强制加进去
            forced = 0
            for name in extras_changed:
                if name not in diff_result.minimal_mod:
                    diff_result.minimal_mod[name] = {}
                    forced += 1
            if extras_changed:
                cb(
                    f"  extras 变更: {len(extras_changed)} task "
                    f"({forced} 个强制入 mod)"
                )
        except Exception as e:
            warnings.append(f"extras 变更检测失败: {e}")

    # 读 def 表 (挂载时存的) 并应用 def 剥离
    def_path = cfg.owm_dir / DEF_TABLES_FILENAME
    if def_path.exists():
        cb("应用 def 剥离 (按 type 查表, 双重判定: def 且 base 也 def)...")
        try:
            def_tables = def_table.DefTables.from_json(
                def_path.read_text(encoding="utf-8")
            )
            stripped = def_table.strip_mod_with_def(
                diff_result.minimal_mod, def_tables,
                canonical_w=canonical_w,
                canonical_base=canonical_base,    # V0.7.3 双重判定
            )
            cb(f"  剥离 {stripped} 个 def 字段")
        except Exception as e:
            warnings.append(f"def 剥离失败 (产物保留全字段): {e}")
    else:
        warnings.append("def 表缺失, 跳过剥离 (产物可能含冗余 def 字段)")

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

    # V1/V2 输出选择
    minimal_to_write = diff_result.minimal_mod
    if cfg.output_format == "v1":
        cb("  转 V1 格式 (按 MPE 风格拍平)...")
        minimal_to_write = translator.pipeline_v2_to_v1(diff_result.minimal_mod)

    # next/on_error 紧凑写法
    if cfg.compact_node_refs:
        cb("  next/on_error 紧凑写法...")
        translator.simplify_node_refs_in_pipeline(minimal_to_write)

    # wait_freezes 紧凑写法 (V0.7.3): 仅 time 字段 → 标量
    cb("  wait_freezes 紧凑写法...")
    wf_simplified = translator.simplify_wait_freezes_in_pipeline(minimal_to_write)
    if wf_simplified:
        cb(f"  简化 {wf_simplified} 个 wait_freezes 字段")

    # 注入 workspace extras 到 minimal_mod (V0.7.0)
    # 仅对 minimal_mod 中存在的 task 注入 (即用户改过的 task)
    # IDENTICAL task 不写 mod, 自然也不写 extras (mod 保持简洁)
    if workspace_extras is not None:
        cb("  注入 workspace extras (用户改过的 task 的 doc/desc)...")
        # 过滤: 只注入 minimal_mod 中存在的 task 的 extras
        filtered_snap = extras_mod.ExtrasSnapshot(
            extras={
                k: v for k, v in workspace_extras.extras.items()
                if k in minimal_to_write
            },
            node_order=workspace_extras.node_order,
        )
        injected = extras_mod.inject_extras_into_pipeline(minimal_to_write, filtered_snap)
        cb(f"  注入 {injected} 个 extras 字段")

    grouped = routing.group_by_target_file(minimal_to_write, index)

    # 节点顺序重排 (V0.7.0): 用 workspace 的 node_order
    # mod 中 task 的顺序参照工作区, 避免 hash 序意外暴露
    if workspace_node_order:
        cb("  按 workspace node_order 重排 mod 文件...")
        grouped = {
            rel: extras_mod.reorder_pipeline_by_node_order(p, rel, workspace_node_order)
            for rel, p in grouped.items()
        }

    written = routing.write_mod_files(grouped, cfg.workspace_pipeline_dir())
    fmt_label = cfg.output_format.upper()
    if cfg.compact_node_refs:
        fmt_label += " + 紧凑 next"
    cb(f"  写入: {len(written)} mod 文件 [{fmt_label}]")

    # 删除 README
    readme = cfg.workspace_dir / "__OWM_README__.md"
    if readme.exists():
        readme.unlink()

    # 删除 snapshot 和 origin 索引 (挂载状态结束)
    (cfg.owm_dir / snapshot.SNAPSHOT_FILENAME).unlink(missing_ok=True)
    origin_path.unlink(missing_ok=True)
    (cfg.owm_dir / DEF_TABLES_FILENAME).unlink(missing_ok=True)
    (cfg.owm_dir / EXTRAS_FILENAME).unlink(missing_ok=True)

    _log(cfg, f"[UNMOUNT-OK] mod_files={len(written)} counts={counts} backup={work_backup.name}")

    return UnmountResult(
        work_backup=work_backup.name,
        minimal_mod_files=len(written),
        counts=counts,
        hints=hints,
        warnings=warnings,
    )
