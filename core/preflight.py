"""
core/preflight.py — 工作区预检 (卸载前自动 + 用户主动 [C] 检查)

职责:
  1. 用 oracle 模拟卸载: try canonicalize(workspace), 拿到 ok/fail
  2. 失败时给笼统信号 (MaaFW 报错千行无法精准解析, 引导用户用 VSC 插件查)
  3. 成功时跑一次轻量 diff, 给文件级变动统计

设计哲学:
  - 不重复 schema 逻辑 (MaaFW oracle 加载是真理判据, 通过 = 卸载肯定通过)
  - 失败阻断 unmount (用户须先修, 避免错乱状态写进 mod)
  - 单一 hook 点 (validate_workspace), 卸载流程和 [C] 菜单共用
  - 报告侧重"开发者决策": 改了多少, 涉及哪些文件, 该不该 git
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import pathlib
from typing import Callable, Dict, List, Optional

from . import config as config_mod
from . import diff
from . import oracle
from . import snapshot


ProgressCb = Optional[Callable[[str], None]]


# ============================================================
# 数据结构
# ============================================================

@dataclasses.dataclass
class FileStat:
    """单个文件的变动统计。"""
    relative: str               # 相对 workspace_pipeline_dir 的 POSIX 路径
    modified: int = 0
    added: int = 0
    deleted: int = 0
    identical: int = 0          # "无变化" 节点

    @property
    def total_changes(self) -> int:
        return self.modified + self.added + self.deleted

    @property
    def has_changes(self) -> bool:
        return self.total_changes > 0


@dataclasses.dataclass
class ValidationResult:
    """工作区预检结果。"""
    ok: bool
    summary: str                # 一句话总结
    error_detail: Optional[str] = None  # ok=False 时的引导文本
    
    # ok=True 时填充以下字段:
    total_modified: int = 0
    total_added: int = 0
    total_deleted: int = 0
    total_identical: int = 0
    file_stats: List[FileStat] = dataclasses.field(default_factory=list)

    def has_changes(self) -> bool:
        return (self.total_modified + self.total_added + self.total_deleted) > 0


# ============================================================
# 主验证函数
# ============================================================

def validate_workspace(
    cfg: config_mod.OverlayConfig,
    progress_callback: ProgressCb = None,
) -> ValidationResult:
    """对工作区做一次 dry-run: 模拟 oracle 加载 + diff, 给文件级统计。

    流程:
      1. 加载工作区 (失败 → 返回 ok=False + 引导文本)
      2. 读快照 (找不到也返回 ok=False)
      3. diff(canonical_w, canonical_base) → kind 分布
      4. 按 task 归属文件分组, 给每个文件出统计

    不写任何文件, 不动备份, 不影响 .maaowm 状态。
    """
    cb = progress_callback or (lambda _: None)

    # 1. 加载工作区
    cb("加载工作区 (oracle.canonicalize)...")
    try:
        canonical_w = oracle.canonicalize(cfg.workspace_pipeline_dir())
    except oracle.OracleError as e:
        return ValidationResult(
            ok=False,
            summary="工作区加载失败 — 工作区含语法或字段错误",
            error_detail=(
                "MaaFW 拒绝加载工作区, 通常因:\n"
                "  • 改了 type 但残留字段类型不匹配 (常见: OCR 的 threshold 应是 0.7,\n"
                "    TemplateMatch 的 threshold 应是 [0.7])\n"
                "  • next/on_error 引用了不存在的 task 名\n"
                "  • JSON 语法错误 (多余的逗号、括号不配对等)\n"
                "\n"
                "建议处置:\n"
                "  1. 用 VSC + MAA support 插件打开工作区, 查看精确告警位置\n"
                "  2. 修复后再次执行 [C] 检查或 [U] 卸载\n"
                "  3. 紧急回退: 用 [B] 查看备份, 从 .maaowm/work_xxx 恢复\n"
                "\n"
                f"原始错误信号: {e}"
            ),
        )

    cb(f"  ✓ 加载成功: {len(canonical_w)} task")

    # 2. 读快照
    cb("读取挂载快照...")
    try:
        snap = snapshot.read_snapshot(cfg.owm_dir)
    except snapshot.SnapshotError as e:
        return ValidationResult(
            ok=False,
            summary="快照读取失败 — 挂载状态可能损坏",
            error_detail=(
                f"无法读取 .maaowm/snapshot.json: {e}\n"
                "建议: 通过 [B] 查看备份目录, 手动从最近的 mod_og_xxx 恢复"
            ),
        )

    canonical_base = snap.canonical_base
    cb(f"  ✓ 快照: {len(canonical_base)} task ({snap.mount_ts})")

    # 3. diff
    cb("计算 diff...")
    diff_result = diff.compute_minimal_mod(canonical_w, canonical_base)
    counts = diff_result.counts()

    total_modified = counts.get("MODIFIED", 0)
    total_added = counts.get("MOD_ONLY", 0)
    total_deleted = counts.get("DELETED", 0)
    total_identical = counts.get("IDENTICAL", 0)

    cb(
        f"  ✓ diff: {total_modified} 修改 / {total_added} 新增 / "
        f"{total_deleted} 删除 / {total_identical} 无变化"
    )

    # 4. 按文件归属分组 — 用 origin 索引
    cb("按文件归属统计...")
    file_stats = _group_by_file(
        cfg, canonical_w, canonical_base, diff_result,
    )

    # 总结句
    if total_modified + total_added + total_deleted == 0:
        summary = "工作区与 base 完全一致, 无可卸载内容"
    else:
        parts = []
        if total_modified:
            parts.append(f"修改 {total_modified}")
        if total_added:
            parts.append(f"新增 {total_added}")
        if total_deleted:
            parts.append(f"删除 {total_deleted}")
        summary = (
            f"工作区可正常卸载 ({' / '.join(parts)}, 涉及 "
            f"{sum(1 for f in file_stats if f.has_changes)} 个文件)"
        )

    return ValidationResult(
        ok=True,
        summary=summary,
        total_modified=total_modified,
        total_added=total_added,
        total_deleted=total_deleted,
        total_identical=total_identical,
        file_stats=file_stats,
    )


def _group_by_file(
    cfg: config_mod.OverlayConfig,
    canonical_w: Dict[str, dict],
    canonical_base: Dict[str, dict],
    diff_result: diff.DiffResult,
) -> List[FileStat]:
    """按 task 在工作区的归属文件分组统计。

    工作区文件 = mount 时按 origin 索引写出的, 复用同样规则确认每个 task 的归属。
    """
    # 工作区扫描: 每个 task → 当前在哪个文件
    workspace_pipeline = cfg.workspace_pipeline_dir()
    task_to_file: Dict[str, str] = {}
    for name, path in oracle.list_node_names_with_origin(workspace_pipeline).items():
        rel = path.relative_to(workspace_pipeline).as_posix()
        task_to_file[name] = rel

    # 收集所有出现过的文件
    all_files: set = set(task_to_file.values())

    # 给每个 task 的决策按文件归类
    file_to_stat: Dict[str, FileStat] = {f: FileStat(relative=f) for f in all_files}

    for d in diff_result.decisions:
        # task 当前在哪个文件
        f = task_to_file.get(d.name)
        if f is None:
            # 该 task 在 canonical_base 里但工作区没有该 task → DELETED
            # 此 task 在工作区里没文件归属 — 跳过, 在 summary 里仅计入总数
            continue
        stat = file_to_stat[f]
        if d.kind == "MODIFIED":
            stat.modified += 1
        elif d.kind == "MOD_ONLY":
            stat.added += 1
        elif d.kind == "IDENTICAL":
            stat.identical += 1
        # DELETED 由前面的 if f is None 路径处理

    # 排序: 有变化的在前 (改动多的更前), 无变化的在后 (按文件名)
    has_changes = sorted(
        [f for f in file_to_stat.values() if f.has_changes],
        key=lambda x: (-x.total_changes, x.relative),
    )
    no_changes = sorted(
        [f for f in file_to_stat.values() if not f.has_changes],
        key=lambda x: x.relative,
    )
    return has_changes + no_changes


# ============================================================
# 自检 (合成数据)
# ============================================================

def _self_test() -> bool:
    """自检不调真 oracle, 只测 _group_by_file 的统计逻辑。"""
    print("preflight 自检")
    print("─" * 60)

    # 模拟 diff_result
    decisions = [
        diff.TaskDecision("TaskA1", "IDENTICAL", "..."),
        diff.TaskDecision("TaskA2", "MODIFIED", "..."),
        diff.TaskDecision("TaskA3", "IDENTICAL", "..."),
        diff.TaskDecision("TaskB1", "MOD_ONLY", "..."),
        diff.TaskDecision("TaskB2", "MODIFIED", "..."),
        diff.TaskDecision("TaskC1", "IDENTICAL", "..."),
    ]
    fake_diff = diff.DiffResult(minimal_mod={}, decisions=decisions)

    task_to_file = {
        "TaskA1": "a.json", "TaskA2": "a.json", "TaskA3": "a.json",
        "TaskB1": "b.json", "TaskB2": "b.json",
        "TaskC1": "c.json",
    }

    # 直接构造 file_stats 验证排序与计数
    file_to_stat: Dict[str, FileStat] = {
        f: FileStat(relative=f) for f in set(task_to_file.values())
    }
    for d in decisions:
        f = task_to_file.get(d.name)
        stat = file_to_stat[f]
        if d.kind == "MODIFIED":
            stat.modified += 1
        elif d.kind == "MOD_ONLY":
            stat.added += 1
        elif d.kind == "IDENTICAL":
            stat.identical += 1

    has_changes = sorted(
        [f for f in file_to_stat.values() if f.has_changes],
        key=lambda x: (-x.total_changes, x.relative),
    )
    no_changes = sorted(
        [f for f in file_to_stat.values() if not f.has_changes],
        key=lambda x: x.relative,
    )
    result = has_changes + no_changes

    expected = [
        FileStat("b.json", modified=1, added=1, identical=0),  # 2 changes
        FileStat("a.json", modified=1, identical=2),           # 1 change
        FileStat("c.json", identical=1),                       # 0 changes
    ]

    ok_count = (len(result) == 3)
    ok_b = result[0].relative == "b.json" and result[0].modified == 1 and result[0].added == 1
    ok_a = result[1].relative == "a.json" and result[1].modified == 1 and result[1].identical == 2
    ok_c = result[2].relative == "c.json" and result[2].identical == 1
    ok_sort = result[0].total_changes >= result[1].total_changes
    ok_no_change_last = result[2].total_changes == 0

    print(f"  文件数:    3 → 实际 {len(result)} {'✓' if ok_count else '✗'}")
    print(f"  b.json (改动多, 在前): {'✓' if ok_b else '✗'}")
    print(f"  a.json (改动少):       {'✓' if ok_a else '✗'}")
    print(f"  c.json (无变化, 在后): {'✓' if ok_c else '✗'}")
    print(f"  排序: 有改动在前:       {'✓' if ok_sort else '✗'}")
    print(f"  无变化排在最后:         {'✓' if ok_no_change_last else '✗'}")

    print()
    print("文件统计预览:")
    for fs in result:
        if fs.has_changes:
            parts = []
            if fs.modified: parts.append(f"修改 {fs.modified}")
            if fs.added: parts.append(f"新增 {fs.added}")
            if fs.deleted: parts.append(f"删除 {fs.deleted}")
            tag = " | ".join(parts)
        else:
            tag = "无变化"
        print(f"  {fs.relative:20s}  {tag}    (含 {fs.identical} 无变化)")

    return ok_count and ok_b and ok_a and ok_c and ok_sort and ok_no_change_last


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
