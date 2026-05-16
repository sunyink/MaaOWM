#!/usr/bin/env python3
"""
verify_workspace_minimal_v2.py — 真测"全剥光" minimal 形态的 round-trip

V1 教训:
  之前的 verify v1 测的是 "现状 strip 后是否安全". 但 strip_mod_with_def 实际只
  剥了一部分 (顶层标量没动, 子嵌套没动). v1 通过仅说明"少剥也安全", 不能证明
  "全剥光也安全".
  实测结果显示工作区还残留 enabled/inverse/max_hit/post_delay 等 def 字段,
  And 嵌套内的 sub-recognition.param 也没剥.

v2 改进 — 模拟"理想全剥光"形态:
  顶层标量字段全剥 (按 task_top def)
  recognition/action.param 内字段按 type 剥
  wait_freezes 内字段按其 def 剥
  attach/anchor 嵌套字段剥
  And/Or 的 sub-recognition 递归剥 (sub_name 等于 type 字符串时省略)
  And 的 box_index == 0 删

写入临时目录, oracle 重新加载, 看 canonical 是否仍闭合.

如果通过, 这套规则就是 V0.6.1 strip_mod_with_def 的扩展依据.

用法:
  python verify_workspace_minimal_v2.py BASE_PIPELINE_DIR
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import sys
import tempfile
from typing import Dict, Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for cand in [SCRIPT_DIR, SCRIPT_DIR / "maaowm-v3", SCRIPT_DIR.parent / "maaowm-v3"]:
    if (cand / "core" / "oracle.py").exists():
        sys.path.insert(0, str(cand))
        break

from core import oracle  # type: ignore
from core import def_table  # type: ignore
from core import translator  # type: ignore


# ============================================================
# 全剥光剥离器 — 比 V0.6.0 strip_mod_with_def 更激进
# ============================================================

def _strip_dict_by_def(target: dict, def_dict: dict) -> int:
    """通用: target 内字段值等于 def_dict 同字段 → 删. 返回删除数."""
    if not isinstance(target, dict) or not isinstance(def_dict, dict):
        return 0
    removed = 0
    for key in list(target.keys()):
        if key not in def_dict:
            continue
        if target[key] == def_dict[key]:
            del target[key]
            removed += 1
    return removed


def strip_sub_recognition(sub_node: Any, def_tables: def_table.DefTables) -> Any:
    """剥离 sub-recognition (And/Or 数组里的元素).

    输入可能是:
      - string (引用其他 task 名) → 不动, 直接返回
      - dict (内联 sub):
          { "sub_name": "OCR", "recognition": {"type":"OCR", "param":{...}} }
        剥离规则:
          1. 按 recognition.type 剥 recognition.param 内 def 字段
          2. recognition.param 全空 → 删 param
          3. sub_name == recognition.type → 删 sub_name (parser 会自动回填)
    """
    if not isinstance(sub_node, dict):
        return sub_node

    reco = sub_node.get("recognition")
    if isinstance(reco, dict):
        r_type = reco.get("type")
        if r_type and r_type in def_tables.reco_param:
            param = reco.get("param")
            if isinstance(param, dict):
                _strip_dict_by_def(param, def_tables.reco_param[r_type])
                if not param:
                    del reco["param"]

    # sub_name == reco.type → 删 (parser 会自动填回 type 名作为 sub_name)
    if isinstance(reco, dict):
        r_type = reco.get("type")
        if r_type and sub_node.get("sub_name") == r_type:
            del sub_node["sub_name"]

    return sub_node


def aggressive_strip(
    pipeline: Dict[str, dict],
    def_tables: def_table.DefTables,
) -> int:
    """对 pipeline 应用激进剥离 (V0.6.0 + 顶层标量 + 子嵌套).

    返回删除字段总数.
    """
    total = 0
    for task_name, task in pipeline.items():
        if not isinstance(task, dict):
            continue

        # ── 1. recognition.param ──
        reco = task.get("recognition")
        if isinstance(reco, dict):
            r_type = reco.get("type")
            if r_type and r_type in def_tables.reco_param:
                param = reco.get("param")
                if isinstance(param, dict):
                    total += _strip_dict_by_def(param, def_tables.reco_param[r_type])
                    if not param:
                        del reco["param"]
                        total += 1

            # And 特殊处理: box_index 默认 0 删, all_of 数组每个元素递归剥
            if r_type == "And":
                param = reco.get("param", {})
                if isinstance(param, dict):
                    if param.get("box_index") == 0:
                        del param["box_index"]
                        total += 1
                    if "all_of" in param and isinstance(param["all_of"], list):
                        for sub in param["all_of"]:
                            strip_sub_recognition(sub, def_tables)

            # Or 特殊处理: any_of 数组每个元素递归剥
            if r_type == "Or":
                param = reco.get("param", {})
                if isinstance(param, dict):
                    if "any_of" in param and isinstance(param["any_of"], list):
                        for sub in param["any_of"]:
                            strip_sub_recognition(sub, def_tables)

            if not reco:
                del task["recognition"]
                total += 1

        # ── 2. action.param ──
        act = task.get("action")
        if isinstance(act, dict):
            a_type = act.get("type")
            if a_type and a_type in def_tables.action_param:
                param = act.get("param")
                if isinstance(param, dict):
                    total += _strip_dict_by_def(param, def_tables.action_param[a_type])
                    if not param:
                        del act["param"]
                        total += 1
            if not act:
                del task["action"]
                total += 1

        # ── 3. wait_freezes ──
        for key in ("pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes"):
            wf = task.get(key)
            if isinstance(wf, dict) and def_tables.wait_freezes:
                total += _strip_dict_by_def(wf, def_tables.wait_freezes)
                if not wf:
                    del task[key]
                    total += 1

        # ── 4. attach/anchor 嵌套 dict ──
        for key in ("attach", "anchor"):
            d = task.get(key)
            d_def = def_tables.task_top.get(key) if def_tables.task_top else None
            if isinstance(d, dict) and isinstance(d_def, dict):
                total += _strip_dict_by_def(d, d_def)
                if not d:
                    del task[key]
                    total += 1

        # ── 5. ★ 顶层标量字段 (NEW in v2) ──
        # task_top 含: enabled, inverse, max_hit, on_error, post_delay, pre_delay,
        #              rate_limit, repeat, repeat_delay, timeout, focus, ...
        # 排除已专门处理的: recognition/action 由上面剥; wait_freezes 由上面剥;
        #                  attach/anchor 由上面剥
        excluded = {
            "recognition", "action",
            "pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes",
            "attach", "anchor",
        }
        for key in list(task.keys()):
            if key in excluded:
                continue
            if key not in def_tables.task_top:
                continue
            if task[key] == def_tables.task_top[key]:
                del task[key]
                total += 1

    return total


# ============================================================
# diff 工具
# ============================================================

def deep_diff_summary(a, b, prefix="", max_lines=30):
    diffs = []

    def walk(av, bv, path):
        if len(diffs) >= max_lines:
            return
        if av == bv:
            return
        if isinstance(av, dict) and isinstance(bv, dict):
            for k in sorted(set(av) | set(bv)):
                walk(av.get(k, "<MISSING>"), bv.get(k, "<MISSING>"),
                     f"{path}.{k}" if path else k)
        elif isinstance(av, list) and isinstance(bv, list):
            if len(av) != len(bv):
                diffs.append(f"  {path}: 列表长度 {len(av)} vs {len(bv)}")
                return
            for i, (a_, b_) in enumerate(zip(av, bv)):
                walk(a_, b_, f"{path}[{i}]")
        else:
            ar = json.dumps(av, ensure_ascii=False)
            br = json.dumps(bv, ensure_ascii=False)
            if len(ar) > 70: ar = ar[:70] + "…"
            if len(br) > 70: br = br[:70] + "…"
            diffs.append(f"  {path}: {ar}  !=  {br}")

    walk(a, b, prefix)
    return diffs


# ============================================================
# 主流程
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    base_dir = pathlib.Path(sys.argv[1])
    if not base_dir.is_dir():
        sys.exit(f"✗ {base_dir} 不是目录")

    print(f"base: {base_dir}")
    print("\n初始化 oracle...")
    oracle.init()

    # ─── Step 1 ───
    print("\n" + "=" * 70)
    print("Step 1: 加载 base → canonical_base (oracle 给的全字段)")
    print("=" * 70)
    canonical_base = oracle.canonicalize(base_dir)
    print(f"  ✓ {len(canonical_base)} task")

    # ─── Step 2 ───
    print("\n" + "=" * 70)
    print("Step 2: 按 type 探 def 表")
    print("=" * 70)
    tables = def_table.build_def_tables(base_dir, verbose=True)
    print(f"  reco 白名单 ({len(tables.reco_param)}): {sorted(tables.reco_param.keys())}")
    print(f"  action 白名单 ({len(tables.action_param)}): {sorted(tables.action_param.keys())}")
    print(f"  task_top def 字段: {sorted(tables.task_top.keys())}")
    if tables.failed_types:
        print(f"  探针失败 ({len(tables.failed_types)}): {tables.failed_types[:3]}...")

    # ─── Step 3 ───
    print("\n" + "=" * 70)
    print("Step 3: 应用激进剥离 (顶层标量 + 子嵌套)")
    print("=" * 70)
    canonical_minimal = copy.deepcopy(canonical_base)
    stripped = aggressive_strip(canonical_minimal, tables)
    print(f"  ✓ 剥离 {stripped} 个 def 字段")

    # ─── Step 4 ───
    print("\n" + "=" * 70)
    print("Step 4: 紧凑 next/on_error 简化")
    print("=" * 70)
    translator.simplify_node_refs_in_pipeline(canonical_minimal)
    print(f"  ✓ 完成")

    # ─── Step 5 ───
    print("\n" + "=" * 70)
    print("Step 5: 写到临时目录, 看体积")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ws_dir = pathlib.Path(tmp) / "minimal"
        ws_dir.mkdir()
        out_file = ws_dir / "all.json"

        ordered = {k: canonical_minimal[k] for k in sorted(canonical_minimal)}
        out_file.write_text(
            json.dumps(ordered, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        minimal_size = out_file.stat().st_size

        full_text = json.dumps(
            {k: canonical_base[k] for k in sorted(canonical_base)},
            ensure_ascii=False, indent=4,
        )
        full_size = len(full_text)
        savings = (1 - minimal_size / full_size) * 100
        print(f"  全字段写法: {full_size:>12,} bytes")
        print(f"  minimal:    {minimal_size:>12,} bytes")
        print(f"  ✓ 体积缩减 {savings:.1f}%")

        # ─── Step 6 ───
        print("\n" + "=" * 70)
        print("Step 6: 重新 oracle.canonicalize(临时目录)")
        print("=" * 70)
        try:
            canonical_loaded = oracle.canonicalize(ws_dir)
        except oracle.OracleError as e:
            print(f"  ✗ 加载失败: {e}")
            print("\n  → 激进剥离触发 parser 拒绝, 需查 stderr 找哪个字段不能省")
            sys.exit(2)
        print(f"  ✓ 加载成功: {len(canonical_loaded)} task")

        # ─── Step 7 ───
        print("\n" + "=" * 70)
        print("Step 7: 对比 canonical_base 和 canonical_loaded")
        print("=" * 70)
        if canonical_base == canonical_loaded:
            print(f"\n  ✓✓✓ 完全一致 — 激进剥离方案安全 ✓✓✓\n")
            print(f"  含义: 顶层标量 + 子嵌套 + box_index 全剥, round-trip 仍闭合.")
            print(f"  可以把这套规则集成到 V0.6.1 strip_mod_with_def. 体积 -{savings:.1f}%.")
            return

        only_in_base = set(canonical_base) - set(canonical_loaded)
        only_in_loaded = set(canonical_loaded) - set(canonical_base)
        common_diff = sorted([
            k for k in (set(canonical_base) & set(canonical_loaded))
            if canonical_base[k] != canonical_loaded[k]
        ])

        print(f"\n  ✗ 不一致 — 激进剥离漂移")
        print(f"    仅 base 有:       {len(only_in_base)}")
        if only_in_base:
            print(f"      前 5: {sorted(only_in_base)[:5]}")
        print(f"    仅 loaded 有:     {len(only_in_loaded)}")
        if only_in_loaded:
            print(f"      前 5: {sorted(only_in_loaded)[:5]}")
        print(f"    内容不同的 task:  {len(common_diff)}")

        if not common_diff:
            return

        # 字段路径频次
        path_freq: Dict[str, int] = {}
        for name in common_diff:
            for line in deep_diff_summary(
                canonical_base[name], canonical_loaded[name], max_lines=200
            ):
                path = line.strip().split(":")[0]
                path = re.sub(r'\[\d+\]', '[*]', path)
                path_freq[path] = path_freq.get(path, 0) + 1

        print(f"\n  ── 漂移字段路径频次 (前 15) ──")
        for path, freq in sorted(path_freq.items(), key=lambda x: -x[1])[:15]:
            print(f"    {freq:5d}  {path}")

        print(f"\n  ── 详情: 前 3 个漂移 task ──")
        for name in common_diff[:3]:
            print(f"\n  [{name}]")
            for line in deep_diff_summary(canonical_base[name], canonical_loaded[name])[:10]:
                print(f"  {line}")

        print(f"\n  → 这些字段是 \"伪 def\" 或 parser 兼容性问题, 应保留")
        print(f"  → 把漂移路径里的字段从激进剥离名单移除即可")


if __name__ == "__main__":
    main()
