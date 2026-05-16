#!/usr/bin/env python3
"""
verify_def_stripping.py v2 — 按 type 探 def + round-trip 验证

V1 教训:
  - 用空 task 探到的 def 是 DirectHit, ColorMatch 字段不在表里 → 剥不掉
  - dumper 输出 ColorMatch.lower:[] 这种"伪空 def", parser 自己读不回来
    → 不剥离时 mod 重新加载会失败 (round-trip 断)
    → 剥离后字段不在 → parser 走默认分支 → 加载成功

V2 改进:
  - 对每种 recognition type 探一次空 task, 构建 def 表 (按 type 索引)
  - 对每种 action type 同样探一次
  - wait_freezes 是单一类型, 直接复用 def 探针的结果
  - round-trip 验证: 写 worktime → V3 路 D → def 剥离 → 重新加载, 看是否一致

用法:
  python verify_def_stripping.py BASE_PIPELINE_DIR
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for cand in [SCRIPT_DIR, SCRIPT_DIR / "maaowm-v3", SCRIPT_DIR.parent / "maaowm-v3"]:
    if (cand / "core" / "oracle.py").exists():
        sys.path.insert(0, str(cand))
        break

from core import oracle  # type: ignore


# ============================================================
# 已知 type 列表 (来自 Pipeline Protocol 文档)
# ============================================================

RECO_TYPES = [
    "DirectHit", "OCR", "TemplateMatch", "FeatureMatch", "ColorMatch",
    "NeuralNetworkClassifier", "NeuralNetworkDetector", "Custom",
    # And / Or 跳过 (组合逻辑, 没有自己的 param)
]

ACTION_TYPES = [
    "DoNothing", "Click", "LongPress", "Swipe", "MultiSwipe",
    "Key", "InputText", "StartApp", "StopApp", "StopTask",
    "Custom", "Command",
]


# ============================================================
# 按 type 探 def 表
# ============================================================

def _probe_one_task(base_dir: pathlib.Path, probe_task_def: dict) -> Optional[dict]:
    """加载 base + 一个 probe task, 返回 probe task 的 canonical。

    探针用 V1 写法的最简表达 (例: {"recognition":"OCR"}), 让 parser 走"字段不存在用 default"分支。
    """
    PROBE = "__owm_def_probe__"
    from maa.resource import Resource  # type: ignore

    res = Resource()
    res.post_pipeline(str(base_dir)).wait()
    if not res.loaded:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        probe_file = pathlib.Path(tmp) / "_probe.json"
        probe_file.write_text(
            json.dumps({PROBE: probe_task_def}), encoding="utf-8"
        )
        res.post_pipeline(str(tmp)).wait()
        if not res.loaded:
            return None

    return res.get_node_data(PROBE)


def build_def_tables(base_dir: pathlib.Path) -> Tuple[
    Dict[str, dict], Dict[str, dict], dict, dict
]:
    """构建按 type 索引的 def 表。

    返回:
      reco_def_table:  { reco_type: param dict (def 值) }
      action_def_table: { action_type: param dict (def 值) }
      wait_freezes_def: dict (single type)
      task_top_def:    dict (task 顶层字段 def, 如 max_hit/post_delay 等)
    """
    reco_table: Dict[str, dict] = {}
    action_table: Dict[str, dict] = {}
    wait_freezes_def: dict = {}
    task_top_def: dict = {}

    print("  探 recognition 各 type:", end=" ", flush=True)
    for rt in RECO_TYPES:
        canon = _probe_one_task(base_dir, {"recognition": rt})
        if canon is None:
            print(f"\n    ✗ {rt}: 探针失败")
            continue
        reco = canon.get("recognition", {})
        if isinstance(reco, dict) and reco.get("type") == rt:
            reco_table[rt] = reco.get("param", {})
            print(f"{rt}({len(reco_table[rt])})", end=" ", flush=True)
        else:
            print(f"\n    ⚠ {rt}: parser 把 type 改成了 {reco.get('type')!r}, 跳过")

        # 顺便记下 task 顶层 def (任意一次探针都能拿)
        if not task_top_def:
            task_top_def = {
                k: v for k, v in canon.items()
                if k not in ("recognition", "action", "pre_wait_freezes",
                             "post_wait_freezes", "repeat_wait_freezes")
            }
            wait_freezes_def = canon.get("pre_wait_freezes", {})
    print()

    print("  探 action 各 type:", end=" ", flush=True)
    for at in ACTION_TYPES:
        # action 探针: recognition 用 DirectHit (确保能加载), action 设目标 type
        canon = _probe_one_task(base_dir, {
            "recognition": "DirectHit",
            "action": at,
        })
        if canon is None:
            print(f"\n    ✗ {at}: 探针失败")
            continue
        act = canon.get("action", {})
        if isinstance(act, dict) and act.get("type") == at:
            action_table[at] = act.get("param", {})
            print(f"{at}({len(action_table[at])})", end=" ", flush=True)
        else:
            print(f"\n    ⚠ {at}: parser 把 type 改成了 {act.get('type')!r}, 跳过")
    print()

    return reco_table, action_table, wait_freezes_def, task_top_def


# ============================================================
# 字段唯一性扫描 (沿用 v1)
# ============================================================

def collect_field_usage(canonical: Dict[str, dict]) -> Tuple[
    Dict[str, Set[str]], Dict[str, Set[str]]
]:
    reco_usage: Dict[str, Set[str]] = defaultdict(set)
    act_usage: Dict[str, Set[str]] = defaultdict(set)

    for task_data in canonical.values():
        if not isinstance(task_data, dict):
            continue
        reco = task_data.get("recognition", {})
        if isinstance(reco, dict):
            rtype = reco.get("type", "?")
            param = reco.get("param", {})
            if isinstance(param, dict):
                for f in param:
                    reco_usage[f].add(rtype)
        act = task_data.get("action", {})
        if isinstance(act, dict):
            atype = act.get("type", "?")
            param = act.get("param", {})
            if isinstance(param, dict):
                for f in param:
                    act_usage[f].add(atype)

    return reco_usage, act_usage


def report_def_table_diversity(reco_table, action_table):
    """按 type 探到的 def 表, 扫同名字段值是否一致。"""
    print("\n" + "=" * 70)
    print("def 表的同名字段跨 type 一致性")
    print("=" * 70)

    # recognition.param: 收集每个字段名在不同 type 里的 def 值
    reco_field_values: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for t, param in reco_table.items():
        for f, v in param.items():
            reco_field_values[f][t] = v

    print("\nrecognition.param 同名字段跨 type 出现:")
    cross = sorted([
        (f, types) for f, types in reco_field_values.items() if len(types) > 1
    ], key=lambda x: -len(x[1]))
    same_value: List[Tuple[str, Any]] = []
    diff_value: List[Tuple[str, Dict[str, Any]]] = []
    for f, types in cross:
        unique_vals = set()
        try:
            unique_vals = set(json.dumps(v, sort_keys=True) for v in types.values())
        except TypeError:
            unique_vals = {repr(v) for v in types.values()}
        if len(unique_vals) == 1:
            same_value.append((f, list(types.values())[0]))
        else:
            diff_value.append((f, types))

    print(f"  跨 type 但 def 值一致 ({len(same_value)}):")
    for f, v in same_value[:10]:
        vs = json.dumps(v, ensure_ascii=False)
        if len(vs) > 50: vs = vs[:50] + "…"
        print(f"    {f:25s}  def = {vs}")

    print(f"  跨 type 且 def 值不一致 ({len(diff_value)}):")
    for f, types in diff_value:
        print(f"    {f:25s}")
        for t, v in sorted(types.items()):
            vs = json.dumps(v, ensure_ascii=False)
            if len(vs) > 50: vs = vs[:50] + "…"
            print(f"      [{t}]: {vs}")

    if diff_value:
        print(f"\n  ★ 这就是为什么必须按 type 探 def, 不能用单一全局 def 表 ★")


# ============================================================
# 按 type 剥离 def
# ============================================================

def _strip_in_dict_by_def(target: dict, def_dict: dict) -> int:
    """对 target dict 内每个键, 值等于 def_dict 同名键 → 删除。
    嵌套 dict 也递归。返回删除字段数。
    """
    removed = 0
    for key in list(target.keys()):
        if key not in def_dict:
            continue
        t_val, d_val = target[key], def_dict[key]
        if isinstance(t_val, dict) and isinstance(d_val, dict):
            inner = _strip_in_dict_by_def(t_val, d_val)
            removed += inner
            if not t_val:
                del target[key]
                removed += 1
        elif t_val == d_val:
            del target[key]
            removed += 1
    return removed


def strip_mod_with_typed_def(
    mod: Dict[str, dict],
    reco_table: Dict[str, dict],
    action_table: Dict[str, dict],
    wait_freezes_def: dict,
    task_top_def: dict,
    canonical_w: Optional[Dict[str, dict]] = None,
) -> int:
    """对 mod 产物按 type 剥离 def。

    canonical_w 用于查 task 当前的 type (因为 mod 里若 type 没变是不会写 type 字段的,
    需要回到 worktime canonical 看 type 是什么)。如果不传, 只能从 mod 自身读 type,
    type 在 mod 里没显式写时跳过该字段的剥离。
    """
    total = 0
    for task_name, task_def in mod.items():
        if not isinstance(task_def, dict):
            continue

        # ─── recognition.param 剥离 ───
        reco = task_def.get("recognition")
        if isinstance(reco, dict):
            # 拿 type: 优先 mod 内, 否则去 worktime canonical 找
            r_type = reco.get("type")
            if not r_type and canonical_w:
                w_reco = canonical_w.get(task_name, {}).get("recognition", {})
                if isinstance(w_reco, dict):
                    r_type = w_reco.get("type")
            if r_type and r_type in reco_table:
                param = reco.get("param")
                if isinstance(param, dict):
                    total += _strip_in_dict_by_def(param, reco_table[r_type])
                    if not param:
                        del reco["param"]
                        total += 1
            # 如果 reco 剥到只剩 type, 而 type 又来自 worktime (mod 里没显式), 删
            # 但要小心: 如果 type 是 mod 自己写的 (worktime 改过), 必须保留
            if not reco or set(reco.keys()) <= {"type"} and "type" not in (
                {} if not canonical_w else canonical_w.get(task_name, {}).get("recognition", {})
            ):
                # 这里逻辑复杂, 暂不深究, 保留更保守
                pass

        # ─── action.param 剥离 ───
        act = task_def.get("action")
        if isinstance(act, dict):
            a_type = act.get("type")
            if not a_type and canonical_w:
                w_act = canonical_w.get(task_name, {}).get("action", {})
                if isinstance(w_act, dict):
                    a_type = w_act.get("type")
            if a_type and a_type in action_table:
                param = act.get("param")
                if isinstance(param, dict):
                    total += _strip_in_dict_by_def(param, action_table[a_type])
                    if not param:
                        del act["param"]
                        total += 1

        # ─── wait_freezes 剥离 ───
        for key in ("pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes"):
            wf = task_def.get(key)
            if isinstance(wf, dict) and wait_freezes_def:
                total += _strip_in_dict_by_def(wf, wait_freezes_def)
                if not wf:
                    del task_def[key]
                    total += 1

        # ─── attach / anchor 等 task 顶层嵌套字段, 用 task_top_def 剥 ───
        for key in ("attach", "anchor"):
            d = task_def.get(key)
            d_def = task_top_def.get(key) if task_top_def else None
            if isinstance(d, dict) and isinstance(d_def, dict):
                total += _strip_in_dict_by_def(d, d_def)
                if not d:
                    del task_def[key]
                    total += 1

    return total


# ============================================================
# round-trip 验证
# ============================================================

def round_trip_test(
    base_dir: pathlib.Path,
    worktime_task: Tuple[str, dict],
    reco_table, action_table, wait_freezes_def, task_top_def,
    label: str,
):
    """单个 task 的 round-trip 测试。
    
    worktime_task: (task_name, full_canonical_task_dict)
    模拟流程:
      1. 取 baseline (canonical_base) 的同名 task
      2. 一级 diff: worktime_task vs baseline
      3. (跳过路 D 二级递归, 用最简单"整段写入" 模拟)
         注: 为简化, 这里用一级 diff 整段写, 不跑路 D
      4. 应用 def 剥离
      5. 写 mod 文件, base+mod 加载, 看 task canonical
      6. 对比是否等于 worktime_task
    """
    print(f"\n  ── case: {label} ──")
    name, w_def = worktime_task
    canonical_base = oracle.canonicalize(base_dir)
    base_def = canonical_base.get(name, {})

    # 一级 diff (整段)
    raw_delta = {
        f: w_def[f] for f in w_def
        if w_def[f] != base_def.get(f)
    }
    mod = {name: copy.deepcopy(raw_delta)}
    print(f"    一级 diff 后 mod 字段: {sorted(mod[name].keys())}")

    # def 剥离
    mod_stripped = copy.deepcopy(mod)
    canonical_w_dummy = {name: w_def}
    removed = strip_mod_with_typed_def(
        mod_stripped, reco_table, action_table, wait_freezes_def, task_top_def,
        canonical_w_dummy,
    )
    print(f"    def 剥离后字段: {sorted(mod_stripped[name].keys())} (-{removed} 个子字段)")

    # round-trip
    with tempfile.TemporaryDirectory() as tmp:
        mod_dir = pathlib.Path(tmp) / "mod"
        mod_dir.mkdir()
        (mod_dir / "test.json").write_text(
            json.dumps(mod_stripped, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        try:
            merged = oracle.canonicalize_overlay(base_dir, mod_dir)
        except oracle.OracleError as e:
            print(f"    ✗ round-trip 加载失败: {e}")
            return False

        actual = merged.get(name, {})
        ok = (actual == w_def)
        print(f"    round-trip {'✓' if ok else '✗'}")
        if not ok:
            for k in sorted(set(actual) | set(w_def)):
                if actual.get(k) != w_def.get(k):
                    a = json.dumps(actual.get(k), ensure_ascii=False)
                    w = json.dumps(w_def.get(k), ensure_ascii=False)
                    if len(a) > 70: a = a[:70] + "…"
                    if len(w) > 70: w = w[:70] + "…"
                    print(f"      [{k}] 实际: {a}")
                    print(f"      [{k}] 期望: {w}")
        return ok


# ============================================================
# 主入口
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

    # ─── 探 def 表 ───
    print("\n" + "=" * 70)
    print("Phase 1: 按 type 探 def 表")
    print("=" * 70)
    reco_table, action_table, wait_freezes_def, task_top_def = build_def_tables(base_dir)
    print(f"\n  ✓ recognition def: {len(reco_table)} type")
    print(f"  ✓ action def:      {len(action_table)} type")
    print(f"  ✓ wait_freezes def 字段数: {len(wait_freezes_def)}")
    print(f"  ✓ task 顶层 def 字段: {sorted(task_top_def.keys())}")

    # ─── 字段唯一性 ───
    print("\n" + "=" * 70)
    print("Phase 2: base 中字段实际使用唯一性")
    print("=" * 70)
    canonical_base = oracle.canonicalize(base_dir)
    reco_usage, act_usage = collect_field_usage(canonical_base)
    multi_reco = sorted([(f, sorted(t)) for f, t in reco_usage.items() if len(t) > 1],
                        key=lambda x: -len(x[1]))
    print(f"\n  recognition.param 跨 type 字段 ({len(multi_reco)}):")
    for f, types in multi_reco:
        print(f"    {f:25s}  {types}")

    # def 表跨 type 字段值差异
    report_def_table_diversity(reco_table, action_table)

    # ─── round-trip 测试 ───
    print("\n" + "=" * 70)
    print("Phase 3: round-trip 测试 (按 type 探 def 后剥离)")
    print("=" * 70)

    # case 1: OCR → ColorMatch (你之前的 Activities_Fin 场景)
    target = "Activities_Fin"
    if target not in canonical_base:
        for n, d in canonical_base.items():
            if d.get("recognition", {}).get("type") == "OCR":
                target = n
                break

    sim_w = copy.deepcopy(canonical_base[target])
    cm_def_param = reco_table.get("ColorMatch", {})
    sim_w["recognition"] = {"type": "ColorMatch", "param": copy.deepcopy(cm_def_param)}

    round_trip_test(
        base_dir, (target, sim_w),
        reco_table, action_table, wait_freezes_def, task_top_def,
        label="OCR → ColorMatch (空配)"
    )

    # case 2: OCR → TemplateMatch
    sim_w2 = copy.deepcopy(canonical_base[target])
    tm_def_param = reco_table.get("TemplateMatch", {})
    sim_w2["recognition"] = {"type": "TemplateMatch", "param": copy.deepcopy(tm_def_param)}

    round_trip_test(
        base_dir, (target, sim_w2),
        reco_table, action_table, wait_freezes_def, task_top_def,
        label="OCR → TemplateMatch (空配)"
    )

    # case 3: 改 timeout (顶层标量)
    sim_w3 = copy.deepcopy(canonical_base[target])
    sim_w3["timeout"] = 30000
    round_trip_test(
        base_dir, (target, sim_w3),
        reco_table, action_table, wait_freezes_def, task_top_def,
        label="改 timeout (标量字段)"
    )

    # case 4: 改 recognition.param 内一字段 (type 不变)
    sim_w4 = copy.deepcopy(canonical_base[target])
    sim_w4["recognition"]["param"] = copy.deepcopy(sim_w4["recognition"]["param"])
    sim_w4["recognition"]["param"]["expected"] = ["新值"]
    round_trip_test(
        base_dir, (target, sim_w4),
        reco_table, action_table, wait_freezes_def, task_top_def,
        label="OCR.expected 改值 (type 不变)"
    )


if __name__ == "__main__":
    main()
