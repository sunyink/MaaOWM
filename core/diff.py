"""
core/diff.py — minimal mod 计算

核心算法 (V3):
  对每个 task name k:
    - k 在 W 不在 B   → MOD_ONLY:  整段 canonical 进 mod
    - k 在 W 也在 B:
        delta = {f: W[k][f] for f if W[k][f] != B[k][f]}
        delta 非空 → MODIFIED: 写入 delta
        delta 为空 → IDENTICAL: 不进 mod
    - k 在 B 不在 W   → DELETED:   不进 mod (协议无法表达 task 删除)

设计决策:
  - 不做 baseline 剥离 (MOD_ONLY 全字段保留, 与 snapshot 减数自恰)
  - sub-object (recognition/action/attach 等) 字段级 != 整段比较, 任一字段
    变化整段进 mod。这是字段名歧义的代价, 暂不优化。

异常检测:
  - DELETED: 警告用户用 enabled:false 替代
  - MODIFIED 中 sub-object 全字段重写: 提示可能是手动删字段所致
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

try:
    from . import deep_diff
except ImportError:
    # 直接运行 python core/diff.py 时
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    import deep_diff  # type: ignore


# 已删: _KNOWN_SUB_OBJECTS — 路 D 接入后 sub-object 整段重写已不再发生


@dataclass
class TaskDecision:
    name: str
    kind: str   # "MOD_ONLY" | "MODIFIED" | "IDENTICAL" | "DELETED"
    msg: str
    fields_changed: List[str] = field(default_factory=list)


@dataclass
class DiffResult:
    minimal_mod: Dict[str, dict]
    decisions: List[TaskDecision]

    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for d in self.decisions:
            c[d.kind] = c.get(d.kind, 0) + 1
        return c


def compute_minimal_mod(
    canonical_w: Dict[str, dict],
    canonical_base: Dict[str, dict],
    enable_deep_filter: bool = True,
) -> DiffResult:
    """V3 核心算法 (含路 D 二级过滤)。

    enable_deep_filter:
        True  (默认): 应用 deep_diff 二级过滤, 嵌套 dict 字段递归剥离相同子字段
        False: 仅做一级 diff, 嵌套 dict 字段整段保留 (V3 原始行为, 用于回归对照)
    """
    minimal_mod: Dict[str, dict] = {}
    decisions: List[TaskDecision] = []

    for name, w_def in canonical_w.items():
        base_def = canonical_base.get(name)

        if base_def is None:
            # MOD_ONLY: 整段 canonical 进 mod (含所有默认字段, 自恰原则)
            # 不做二级过滤, 因为没有 base 可对照
            minimal_mod[name] = w_def
            decisions.append(TaskDecision(
                name=name, kind="MOD_ONLY",
                msg=f"独有 task, 整段保留 ({len(w_def)} 字段)",
                fields_changed=sorted(w_def.keys()),
            ))
            continue

        raw_delta = {f: v for f, v in w_def.items() if v != base_def.get(f)}

        if not raw_delta:
            decisions.append(TaskDecision(
                name=name, kind="IDENTICAL", msg="与 base 完全一致, 剥离",
            ))
            continue

        # 路 D 二级过滤
        if enable_deep_filter:
            delta = deep_diff.deep_filter_raw_delta(raw_delta, base_def)
        else:
            delta = raw_delta

        if not delta:
            # 二级过滤后变空, 等价于 IDENTICAL
            decisions.append(TaskDecision(
                name=name, kind="IDENTICAL",
                msg="二级过滤后与 base 等效, 剥离",
            ))
            continue

        minimal_mod[name] = delta
        decisions.append(TaskDecision(
            name=name, kind="MODIFIED",
            msg=f"差异字段 {len(delta)}/{len(w_def)}: {sorted(delta.keys())}",
            fields_changed=sorted(delta.keys()),
        ))

    for name in canonical_base:
        if name not in canonical_w:
            decisions.append(TaskDecision(
                name=name, kind="DELETED",
                msg="工作区已删, 但 mod 协议无法表达 task 删除",
            ))

    return DiffResult(minimal_mod=minimal_mod, decisions=decisions)


# ============================================================
# 异常检测 hint
# ============================================================

@dataclass
class Hint:
    severity: str   # "warn" | "info"
    task: str
    text: str


def detect_hints(result: DiffResult) -> List[Hint]:
    """从 diff 结果里挖出值得提示用户的情况。"""
    hints: List[Hint] = []

    for d in result.decisions:
        if d.kind == "DELETED":
            hints.append(Hint(
                severity="warn", task=d.name,
                text=(
                    f"task 已被工作区删除, 但 mod 协议无法表达 task 删除。\n"
                    f"   该 task 在加载 base+mod 后仍以 base 定义存在 (引用方在 next 里"
                    f"找不到也只是跳过)。\n"
                    f"   如确需让其失效, 在 workspace 中恢复并加 \"enabled\": false"
                ),
            ))
    return hints


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    base = {
        "TaskA": {  # IDENTICAL
            "next": ["X", "Y"], "rate_limit": 1000, "timeout": 20000, "attach": {},
            "recognition": {"type": "OCR", "param": {"expected": "确定"}},
        },
        "TaskB": {  # MODIFIED 单字段
            "next": ["X"], "rate_limit": 1000, "timeout": 20000, "attach": {},
            "recognition": {"type": "OCR", "param": {"expected": "原值"}},
        },
        "TaskF": {  # DELETED
            "next": ["End"], "rate_limit": 1000, "timeout": 20000, "attach": {},
            "recognition": {"type": "OCR", "param": {"expected": "废弃"}},
        },
        "TaskG": {  # MODIFIED 嵌套 dict
            "next": [], "rate_limit": 1000, "timeout": 20000,
            "attach": {"a": 1, "b": 2},
            "recognition": {"type": "OCR", "param": {"expected": "AB", "threshold": 0.7}},
        },
        "TaskH": {  # MODIFIED 还原默认值
            "next": [], "rate_limit": 1000, "timeout": 20000, "attach": {},
            "recognition": {"type": "DirectHit", "param": {}},
            "post_delay": 1000,
        },
    }

    ws = {
        "TaskA": {  # 一字未改
            "next": ["X", "Y"], "rate_limit": 1000, "timeout": 20000, "attach": {},
            "recognition": {"type": "OCR", "param": {"expected": "确定"}},
        },
        "TaskB": {
            "next": ["X"], "rate_limit": 1000, "timeout": 30000, "attach": {},
            "recognition": {"type": "OCR", "param": {"expected": "新值"}},
        },
        # TaskF 删了
        "TaskD": {  # MOD_ONLY
            "next": [], "rate_limit": 1000, "timeout": 20000,
            "attach": {}, "anchor": {}, "post_delay": 200,
            "recognition": {"type": "TemplateMatch", "param": {"template": "new.png"}},
            "action": {"type": "DoNothing", "param": {}},
        },
        "TaskG": {
            "next": [], "rate_limit": 1000, "timeout": 20000,
            "attach": {"a": 99, "b": 2},
            "recognition": {"type": "OCR", "param": {"expected": "AB", "threshold": 0.9}},
        },
        "TaskH": {
            "next": [], "rate_limit": 1000, "timeout": 20000, "attach": {},
            "recognition": {"type": "DirectHit", "param": {}},
            "post_delay": 200,
        },
    }

    expected_mod = {
        "TaskB": {
            "timeout": 30000,
            # 路 D 二级过滤: recognition.param.expected 单字段保留
            "recognition": {"param": {"expected": "新值"}},
        },
        "TaskD": ws["TaskD"],   # MOD_ONLY 整段
        "TaskG": {
            # 路 D: attach.a 单字段; recognition.param.threshold 单字段
            "attach": {"a": 99},
            "recognition": {"param": {"threshold": 0.9}},
        },
        "TaskH": {"post_delay": 200},
    }

    expected_kinds = {
        "TaskA": "IDENTICAL", "TaskB": "MODIFIED", "TaskD": "MOD_ONLY",
        "TaskF": "DELETED", "TaskG": "MODIFIED", "TaskH": "MODIFIED",
    }

    result = compute_minimal_mod(ws, base)
    hints = detect_hints(result)

    print("diff 自检 (含路 D 二级过滤)")
    print("─" * 60)
    for d in result.decisions:
        sym = {"MOD_ONLY": "+", "MODIFIED": "~", "IDENTICAL": "=", "DELETED": "-"}[d.kind]
        print(f"  {sym} {d.kind:10s} {d.name:8s} {d.msg}")

    print()
    print("hints:")
    for h in hints:
        sym = "⚠" if h.severity == "warn" else "ℹ"
        print(f"  {sym} [{h.task}] {h.text}")

    print()
    print(f"  最小 mod 包含 task: {sorted(result.minimal_mod.keys())}")

    # 校验
    actual_kinds = {d.name: d.kind for d in result.decisions}
    ok_kinds = (actual_kinds == expected_kinds)
    ok_mod = (result.minimal_mod == expected_mod)
    # hints 现在只有 DELETED 一种
    ok_hints = any(h.task == "TaskF" and h.severity == "warn" for h in hints)

    print()
    print(f"  kinds 正确:    {'✓' if ok_kinds else '✗'}")
    print(f"  mod 内容正确:  {'✓' if ok_mod else '✗'}")
    print(f"  DELETED hint:  {'✓' if ok_hints else '✗'}")

    if not ok_mod:
        for k in sorted(set(result.minimal_mod) | set(expected_mod)):
            if result.minimal_mod.get(k) != expected_mod.get(k):
                print(f"  ▸ 差异 task {k}:")
                print(f"      期望: {json.dumps(expected_mod.get(k), ensure_ascii=False)}")
                print(f"      实际: {json.dumps(result.minimal_mod.get(k), ensure_ascii=False)}")

    return ok_kinds and ok_mod and ok_hints


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
