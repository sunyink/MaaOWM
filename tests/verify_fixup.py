#!/usr/bin/env python3
"""
verify_fixup.py — 验证 sub_recognition fixup 能让 oracle round-trip 闭合

诊断已确认: dumper 输出的 sub_recognition 形如
  { "type": "OCR", "param": {...}, "sub_name": "..." }
parser 不认这个形态 (期望外层 "recognition" 字段), 重新加载会退到 DirectHit 默认。

fixup 思路: 把 dumper 输出转换成 parser 认识的 V2 嵌套形态:
  { "recognition": { "type": "OCR", "param": {...} }, "sub_name": "..." }

这个脚本:
  A. 加载 base 得到 c1
  B. 应用 fixup 修正 sub_recognition 形态
  C. 写入临时目录
  D. 重新 canonicalize 得到 c2
  E. 对比 c1 和 c2

如果 fixup 正确, c1 == c2 round-trip 闭合, 我们的猜想成立。

用法:
  python verify_fixup.py BASE_PIPELINE_DIR
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
from typing import Any, Dict, List

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for cand in [SCRIPT_DIR, SCRIPT_DIR / "maaowm-v3", SCRIPT_DIR.parent / "maaowm-v3"]:
    if (cand / "core" / "oracle.py").exists():
        sys.path.insert(0, str(cand))
        break

from core import oracle  # type: ignore


# ============================================================
# Fixup 函数 — 这就是补丁的全部
# ============================================================

def fixup_sub_recognition_in_place(canonical: Dict[str, dict]) -> int:
    """把每个 task 的 recognition 内嵌 sub list (any_of / all_of) 中的 sub object,
    从 dumper 输出形态转换为 parser 认识的形态。

    转换前 (dumper 输出, parser 读不回来):
        {"type": "OCR", "param": {...}, "sub_name": "X"}

    转换后 (parser 认识的 V2 嵌套):
        {"recognition": {"type": "OCR", "param": {...}}, "sub_name": "X"}

    返回修改的 sub 数量, 用于诊断。
    字符串引用 (如 "Global_Main_Clr") 不动。
    """
    fixed = 0
    for task_name, task_data in canonical.items():
        reco = task_data.get("recognition")
        if not isinstance(reco, dict):
            continue
        if reco.get("type") not in ("And", "Or"):
            continue
        param = reco.get("param")
        if not isinstance(param, dict):
            continue

        for key in ("all_of", "any_of"):
            sub_list = param.get(key)
            if not isinstance(sub_list, list):
                continue
            for i, sub in enumerate(sub_list):
                if not isinstance(sub, dict):
                    continue   # 字符串引用, 跳过
                if "recognition" in sub:
                    continue   # 已经是 parser 形态, 跳过
                if "type" not in sub:
                    continue   # 不是 dumper 形态, 跳过
                # 关键转换
                sub_type = sub.pop("type")
                sub_param = sub.pop("param", {})
                sub["recognition"] = {"type": sub_type, "param": sub_param}
                fixed += 1
    return fixed


# ============================================================
# 验证主流程
# ============================================================

def deep_diff_summary(a, b, prefix: str = "", max_lines: int = 20):
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

    # A. 加载
    print("\nA. 加载 base, 第一次 canonicalize...")
    c1 = oracle.canonicalize(base_dir)
    print(f"   → {len(c1)} task")

    # B. 应用 fixup (深拷贝避免污染 c1, 我们对比时还要用原版)
    import copy
    c1_fixed = copy.deepcopy(c1)
    print("\nB. 应用 fixup (转换 sub_recognition 形态)...")
    fixed_count = fixup_sub_recognition_in_place(c1_fixed)
    print(f"   → 修改了 {fixed_count} 个 sub object")

    # 打印一个示例确认 fixup 形态
    sample_task = None
    for name, data in c1_fixed.items():
        reco = data.get("recognition", {})
        if reco.get("type") in ("And", "Or"):
            param = reco.get("param", {})
            sub_list = param.get("all_of") or param.get("any_of") or []
            if any(isinstance(s, dict) for s in sub_list):
                sample_task = name
                break
    if sample_task:
        print(f"\n   示例 [{sample_task}] fixup 后的第一个 inline sub:")
        reco = c1_fixed[sample_task]["recognition"]
        param = reco["param"]
        sub_list = param.get("all_of") or param.get("any_of") or []
        for s in sub_list:
            if isinstance(s, dict):
                print("   " + json.dumps(s, ensure_ascii=False, indent=4).replace("\n", "\n   "))
                break

    # C. 写入临时目录
    with tempfile.TemporaryDirectory() as tmp:
        rt_dir = pathlib.Path(tmp) / "fixed"
        rt_dir.mkdir()
        out_file = rt_dir / "all.json"

        print(f"\nC. 写入临时文件 {out_file.name} ...")
        ordered = {k: c1_fixed[k] for k in sorted(c1_fixed)}
        out_file.write_text(
            json.dumps(ordered, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        print(f"   → 文件大小 {out_file.stat().st_size:,} bytes")

        # D. 重新 canonicalize
        print(f"\nD. 重新 canonicalize 临时目录...")
        try:
            c2 = oracle.canonicalize(rt_dir)
        except oracle.OracleError as e:
            print(f"   ✗ 加载失败: {e}")
            print("   fixup 后的格式 oracle 也不认, 需要换个 fixup 形态")
            sys.exit(2)
        print(f"   → {len(c2)} task")

        # E. 对比 c1 (原版, 未 fixup) vs c2 (fixup 后再 canonical)
        print(f"\nE. 对比 c1 (原始 base canonical) vs c2 (fixup → 写入 → 重新 canonical):")

        if c1 == c2:
            print(f"   ✓✓✓ 完全一致 — fixup 让 round-trip 闭合, 修复成功 ✓✓✓")
            print(f"\n   含义: V3 在写入工作区前应用此 fixup, 卸载就能正确 diff")
            return

        only_in_1 = set(c1) - set(c2)
        only_in_2 = set(c2) - set(c1)
        common_diff = sorted([k for k in (set(c1) & set(c2)) if c1[k] != c2[k]])

        print(f"   ✗ 仍不一致")
        print(f"     仅 c1 有: {len(only_in_1)}, 仅 c2 有: {len(only_in_2)}, 内容不同: {len(common_diff)}")

        if common_diff:
            # 看是不是还有 sub_recognition 残余, 还是变成另一类问题
            print(f"\n   ── 前 3 个差异 ──")
            for name in common_diff[:3]:
                print(f"\n   [{name}]")
                for line in deep_diff_summary(c1[name], c2[name])[:15]:
                    print(f"   {line}")

            # 字段路径频次
            path_freq: Dict[str, int] = {}
            import re
            for name in common_diff:
                for line in deep_diff_summary(c1[name], c2[name], max_lines=100):
                    path = line.strip().split(":")[0]
                    path = re.sub(r'\[\d+\]', '[*]', path)
                    path_freq[path] = path_freq.get(path, 0) + 1

            print(f"\n   ── 残余差异路径频次 (前 10) ──")
            for path, freq in sorted(path_freq.items(), key=lambda x: -x[1])[:10]:
                print(f"   {freq:5d}  {path}")


if __name__ == "__main__":
    main()
