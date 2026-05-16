#!/usr/bin/env python3
"""
verify_workspace_minimal.py — 想法 3 安全性验证

目标:
  V3 当前 mount 写工作区是 canonical 全字段 (1364 task / 19 顶层字段 / 全展开),
  显示效果像把屏幕填满. 想法 3 提议: 写工作区前跑一次 def 剥离 + 紧凑 next,
  让工作区文件接近 base 简洁形态.

风险:
  workspace 是要被独立加载的. 如果剥离后写的字段集合, oracle 重新加载时
  得到的 canonical != 原始全字段 canonical, 那 unmount 时 diff 会出错.

验证流程:
  Step 1: 加载 base → canonical_base (oracle 给的全字段)
  Step 2: 按 type 探 def 表
  Step 3: 应用 def 剥离 → canonical_minimal
  Step 4: + 紧凑 next 简化
  Step 5: 把 canonical_minimal 写到临时目录
  Step 6: oracle 重新加载临时目录 → canonical_loaded
  Step 7: 对比 canonical_base 和 canonical_loaded
          一致 → 想法 3 实施安全
          不一致 → 列出漂移字段, 评估能否接受 / 如何修补

用法:
  python verify_workspace_minimal.py BASE_PIPELINE_DIR
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import sys
import tempfile
from typing import Dict

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for cand in [SCRIPT_DIR, SCRIPT_DIR / "maaowm-v3", SCRIPT_DIR.parent / "maaowm-v3"]:
    if (cand / "core" / "oracle.py").exists():
        sys.path.insert(0, str(cand))
        break

from core import oracle  # type: ignore
from core import def_table  # type: ignore
from core import translator  # type: ignore


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

    # ─── Step 1: 加载 base 拿全字段 canonical ───
    print("\n" + "=" * 70)
    print("Step 1: 加载 base → canonical_base (oracle 给的全字段)")
    print("=" * 70)
    canonical_base = oracle.canonicalize(base_dir)
    print(f"  ✓ {len(canonical_base)} task")

    # ─── Step 2: 探 def 表 ───
    print("\n" + "=" * 70)
    print("Step 2: 按 type 探 def 表")
    print("=" * 70)
    tables = def_table.build_def_tables(base_dir, verbose=True)
    print(f"  reco 白名单 ({len(tables.reco_param)}): {sorted(tables.reco_param.keys())}")
    print(f"  action 白名单 ({len(tables.action_param)}): {sorted(tables.action_param.keys())}")
    if tables.failed_types:
        print(f"  探针失败 ({len(tables.failed_types)}): {tables.failed_types}")

    # ─── Step 3: 应用 def 剥离 (workspace 形态模拟) ───
    print("\n" + "=" * 70)
    print("Step 3: 应用 def 剥离 (workspace 写出形态模拟)")
    print("=" * 70)
    canonical_minimal = copy.deepcopy(canonical_base)
    stripped = def_table.strip_mod_with_def(
        canonical_minimal, tables, canonical_w=canonical_base,
    )
    print(f"  ✓ 剥离 {stripped} 个 def 字段")

    # ─── Step 4: 紧凑 next + on_error ───
    print("\n" + "=" * 70)
    print("Step 4: 紧凑 next/on_error 简化")
    print("=" * 70)
    translator.simplify_node_refs_in_pipeline(canonical_minimal)
    print(f"  ✓ 完成")

    # ─── Step 5: 写临时目录 ───
    print("\n" + "=" * 70)
    print("Step 5: 写 minimal 形态到临时目录")
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
        print(f"  全字段写法: {full_size:,} bytes")
        print(f"  minimal:    {minimal_size:,} bytes")
        print(f"  ✓ 体积缩减 {savings:.1f}%")

        # ─── Step 6: 重新加载 ───
        print("\n" + "=" * 70)
        print("Step 6: 重新 oracle.canonicalize(临时目录)")
        print("=" * 70)
        try:
            canonical_loaded = oracle.canonicalize(ws_dir)
        except oracle.OracleError as e:
            print(f"  ✗ 加载失败: {e}")
            print("\n  → minimal 形态某处 parser 不接受, 想法 3 不能直接做")
            print("  → 需要查 stderr 找出哪些字段省略后导致 parser 报错")
            print("  → 通常是协议要求显式存在的字段 (例: 某些字段不允许缺省)")
            sys.exit(2)
        print(f"  ✓ 加载成功: {len(canonical_loaded)} task")

        # ─── Step 7: 对比 ───
        print("\n" + "=" * 70)
        print("Step 7: 对比 canonical_base 和 canonical_loaded")
        print("=" * 70)
        if canonical_base == canonical_loaded:
            print(f"\n  ✓✓✓ 完全一致 — 想法 3 实施安全 ✓✓✓\n")
            print(f"  含义: V3 在 mount 写工作区时可以应用 def 剥离 + 紧凑 next,")
            print(f"        oracle 重新加载仍能还原全字段 canonical, diff 不受影响.")
            print(f"        体积缩减 {savings:.1f}%, 工作区显著清爽.")
            return

        only_in_base = set(canonical_base) - set(canonical_loaded)
        only_in_loaded = set(canonical_loaded) - set(canonical_base)
        common_diff = sorted([
            k for k in (set(canonical_base) & set(canonical_loaded))
            if canonical_base[k] != canonical_loaded[k]
        ])

        print(f"\n  ✗ 不一致")
        print(f"    task 总数:        {len(canonical_base)} → {len(canonical_loaded)}")
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

        # 详情前 3 个
        print(f"\n  ── 详情: 前 3 个漂移 task ──")
        for name in common_diff[:3]:
            print(f"\n  [{name}]")
            for line in deep_diff_summary(canonical_base[name], canonical_loaded[name])[:10]:
                print(f"  {line}")

        print(f"\n  → 想法 3 不能直接 100% 做, 漂移字段需要保留")
        print(f"  → 选项: ① 把这些字段从 def 表移除 (保留即可) ② 接受小漂移做局部 minimal")


if __name__ == "__main__":
    main()
