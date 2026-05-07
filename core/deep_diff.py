"""
core/deep_diff.py — 二级过滤: 在顶层 diff 已经产出 raw_delta 后, 递归
剥离嵌套 dict 中与 base 相同的子字段, 让 mod 包更接近"语义最小"形态。

设计要点:
  - raw_delta 中每个 key 已经确定"与 base 不等"
  - 对 dict 类型 value 做递归剥离, 标量/列表保持原样
  - 列表字段不递归 (列表 deep diff 含义不明确, 整段更安全)
  - type 切换不需特殊处理: PipelineData 是固定结构体,
    不同 type 共用同一套字段, 仅值不同; round-trip 仍闭合

设计哲学:
  这一步只影响"产物美观度"和"git diff 友好度", 不影响 round-trip 闭合性。
  即使关掉路 D, V3 仍能正确工作 (产物大但语义对)。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Set


# 已知列表字段: 不做递归剥离, 整段保留
_LIST_FIELDS: Set[str] = {
    "next", "on_error", "interrupt",
    "template", "expected",
    # 任意其他列表型字段都默认不递归 (见 deep_diff_value 实现)
}


def deep_diff_value(w_val: Any, b_val: Any, key_hint: str = "") -> Any:
    """对单个 (w, b) 值对做递归剥离, 返回应写入 mod 的值。

    返回 None 表示"无需写入 mod" (说明完全相等, 调用方应剥离这个 key)。

    注: dict 递归后若结果为空 dict {}, 视情况:
      - 顶层调用 (key_hint=""): 保留空 dict (因为可能用户就是想写空)
      - 嵌套调用: 调用方应判断是否剥离

    本函数不区分上述情况, 由调用方处理。
    """
    if w_val == b_val:
        return None

    # 列表: 整段保留
    if isinstance(w_val, list):
        return w_val

    # 标量或类型不一致: 整段保留
    if not isinstance(w_val, dict) or not isinstance(b_val, dict):
        return w_val

    # dict: 递归剥离
    result: Dict[str, Any] = {}
    for k in w_val:
        sub = deep_diff_value(w_val[k], b_val.get(k), key_hint=k)
        if sub is None:
            continue
        # sub 是 dict 且 deep_diff 后为空 → 也跳过
        if isinstance(sub, dict) and not sub:
            # 但要看一种边缘情况: 如果 b_val 没有这个 key 或这个 key 的值不是空 dict,
            # 而 w_val 这个 key 是空 dict, 那"空 dict"本身可能是用户意图。
            # 这种情况下, deep_diff_value 在外层比较 w_val[k]=={} vs b_val.get(k)
            # 已经判断不等, 才会走到这里。所以保留 {}。
            if w_val[k] == {} and b_val.get(k) != {}:
                result[k] = {}
            continue
        result[k] = sub

    return result


def deep_filter_raw_delta(
    raw_delta: Dict[str, Any],
    base_def: Dict[str, Any],
) -> Dict[str, Any]:
    """对一个 task 的 raw_delta 应用二级过滤。

    raw_delta: V3 一级 diff 的产出, 形如 { field: w_value, ... }
    base_def:  base canonical 的对应 task, 用于递归对照
    返回:     精简后的 minimal delta

    若过滤后某 dict 字段变成空 {}, 该字段从结果剔除 (保持 raw_delta 的"非空"约定)。
    """
    minimal: Dict[str, Any] = {}
    for key, w_val in raw_delta.items():
        # 列表字段直接保留, 不递归
        if key in _LIST_FIELDS or isinstance(w_val, list):
            minimal[key] = w_val
            continue

        # 非 dict 值原样保留
        if not isinstance(w_val, dict):
            minimal[key] = w_val
            continue

        b_val = base_def.get(key)

        # base 没有这个字段, 或者类型不一致: 保留整段
        if not isinstance(b_val, dict):
            minimal[key] = w_val
            continue

        # dict vs dict: 递归剥离
        sub = deep_diff_value(w_val, b_val, key_hint=key)
        if sub is None:
            # w_val == b_val? 但既然进入 raw_delta 应该不等 — 防御性兜底
            continue
        if isinstance(sub, dict) and not sub:
            # 递归后空 dict, 说明所有子字段都和 base 相同 — 这种情况不应出现
            # 在 raw_delta 中 (说明 raw_delta 给的 w_val == b_val), 防御性兜底
            continue
        minimal[key] = sub

    return minimal


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    """覆盖路 D 设计中所有声明的行为。"""

    print("deep_diff 自检")
    print("─" * 60)

    cases: List[tuple] = []

    # ───────────────────────────────────────────────────────
    # case 1: 标量字段不递归
    # ───────────────────────────────────────────────────────
    cases.append((
        "标量字段保持原样",
        {"timeout": 30000},                   # raw_delta
        {"timeout": 20000, "post_delay": 200}, # base
        {"timeout": 30000},                   # expected_minimal
    ))

    # ───────────────────────────────────────────────────────
    # case 2: 列表字段不递归 (整段保留)
    # ───────────────────────────────────────────────────────
    cases.append((
        "列表字段整段保留",
        {"next": ["A", "X", "C"]},            # raw_delta
        {"next": ["A", "B", "C"]},            # base
        {"next": ["A", "X", "C"]},            # expected
    ))

    # ───────────────────────────────────────────────────────
    # case 3: 嵌套 dict 递归剥离
    # ───────────────────────────────────────────────────────
    cases.append((
        "嵌套 dict 单字段变化",
        {"recognition": {
            "type": "OCR",
            "param": {"expected": "新", "threshold": 0.7, "roi": [0,0,100,100]}
        }},
        {"recognition": {
            "type": "OCR",
            "param": {"expected": "原", "threshold": 0.7, "roi": [0,0,100,100]}
        }},
        # 期望: 仅留变化字段
        {"recognition": {"param": {"expected": "新"}}},
    ))

    # ───────────────────────────────────────────────────────
    # case 4: type 切换 + 保留旧字段 (round-trip 闭合验证)
    #         切了 type, 别的字段值碰巧和 base 一样 → 应剥离
    # ───────────────────────────────────────────────────────
    cases.append((
        "type 切换, 共享字段相同 → 剥离",
        {"recognition": {
            "type": "TemplateMatch",
            "param": {
                "expected": "A",            # 和 base 一样 (用户没清理)
                "threshold": 0.7,            # 和 base 一样
                "roi": [0,0,100,100],        # 和 base 一样
                "template": "x.png",         # 新增的字段
            }
        }},
        {"recognition": {
            "type": "OCR",
            "param": {
                "expected": "A",
                "threshold": 0.7,
                "roi": [0,0,100,100],
                "template": "",              # base 这里是 ""
            }
        }},
        # 期望: type 不同, 保留 type; template 不同 ("x.png" vs ""), 保留
        # expected/threshold/roi 都相同, 剥离
        {"recognition": {
            "type": "TemplateMatch",
            "param": {"template": "x.png"},
        }},
    ))

    # ───────────────────────────────────────────────────────
    # case 5: type 切换 + 用户清理 (改默认值)
    # ───────────────────────────────────────────────────────
    cases.append((
        "type 切换 + 用户改 expected 为默认",
        {"recognition": {
            "type": "TemplateMatch",
            "param": {
                "expected": "",              # 用户清理为默认
                "threshold": 0.9,            # 用户改了
                "roi": [10,20,30,40],        # 用户改了
                "template": "x.png",         # 新增
            }
        }},
        {"recognition": {
            "type": "OCR",
            "param": {
                "expected": "A",
                "threshold": 0.7,
                "roi": [0,0,100,100],
                "template": "",
            }
        }},
        {"recognition": {
            "type": "TemplateMatch",
            "param": {
                "expected": "",
                "threshold": 0.9,
                "roi": [10,20,30,40],
                "template": "x.png",
            },
        }},
    ))

    # ───────────────────────────────────────────────────────
    # case 6: attach 递归剥离 (你的修正版语义)
    # ───────────────────────────────────────────────────────
    cases.append((
        "attach 递归: 改一个 key, 其他剥离",
        {"attach": {"a": 99, "b": 2}},
        {"attach": {"a": 1, "b": 2}},
        {"attach": {"a": 99}},
    ))

    # ───────────────────────────────────────────────────────
    # case 7: attach "删 key" 在 dict-merge 语义下不可表达
    #         用户写 attach: {a:1} 试图"删 b" → 实际效果 = base 不变
    #         所以 raw_delta 这里 attach 整段是 {a:1} (因为 != base)
    #         路 D 递归: a 相同 → 剥离, 留 {} → 我们清掉空 dict
    #         结果: mod 不写 attach. MaaFW load 后 attach = base = {a:1, b:2}
    #         用户预期 {a:1} 落空, 但这是 dict-merge 协议层限制, 不是路 D bug
    # ───────────────────────────────────────────────────────
    cases.append((
        "attach 试图删 key (用户语义错误, 但路 D 行为可预测)",
        {"attach": {"a": 1}},                # raw_delta: 整段, 因为整体 != base
        {"attach": {"a": 1, "b": 2}},
        # 期望: a 与 base 相等, 剥离; 整 attach 变空 → 整 attach 不写
        # (这等价于"忽略此次 attach 改动", 用户得到 base 不变, 与 dict-merge 一致)
        {},
    ))

    # ───────────────────────────────────────────────────────
    # case 8: attach 真删 key (用户用空字符串显式表达)
    # ───────────────────────────────────────────────────────
    cases.append((
        "attach 显式改 b 为空字符串",
        {"attach": {"a": 1, "b": ""}},
        {"attach": {"a": 1, "b": 2}},
        {"attach": {"b": ""}},
    ))

    # ───────────────────────────────────────────────────────
    # case 9: 多层嵌套 (recognition.param.子字段)
    # ───────────────────────────────────────────────────────
    cases.append((
        "三层嵌套, 仅最深层变化",
        {"action": {
            "type": "Click",
            "param": {
                "target": [100, 100, 50, 50],
                "target_offset": [0, 0, 0, 0],
            }
        }},
        {"action": {
            "type": "Click",
            "param": {
                "target": [50, 50, 50, 50],
                "target_offset": [0, 0, 0, 0],
            }
        }},
        # 期望: 只剩 param.target
        {"action": {"param": {"target": [100, 100, 50, 50]}}},
    ))

    # ───────────────────────────────────────────────────────
    # case 10: 嵌套 dict 中字段类型不一致 (奇葩 case, 整段保留)
    # ───────────────────────────────────────────────────────
    cases.append((
        "嵌套字段类型不一致, 保留整段",
        {"focus": {"foo": "bar"}},
        {"focus": None},
        {"focus": {"foo": "bar"}},
    ))

    # ───────────────────────────────────────────────────────
    # case 11: 用户写空 attach, base 非空 (dict-merge 下无效操作, 剥离)
    #         attach: {} 在 dict_merge 语义下等于"啥也没说", 不会清空 base
    #         物理结果与 case 7 同构: 整 attach 被剥离, 运行时 attach = base
    # ───────────────────────────────────────────────────────
    cases.append((
        "用户写空 attach (dict-merge 下无效, 剥离)",
        {"attach": {}},
        {"attach": {"a": 1}},
        {},
    ))

    # 跑测试
    all_ok = True
    for i, (name, raw_delta, base_def, expected) in enumerate(cases, 1):
        actual = deep_filter_raw_delta(raw_delta, base_def)
        ok = (actual == expected)
        if ok:
            print(f"  ✓ case {i:2d}: {name}")
        else:
            all_ok = False
            print(f"  ✗ case {i:2d}: {name}")
            print(f"      raw_delta:  {json.dumps(raw_delta, ensure_ascii=False)}")
            print(f"      base_def:   {json.dumps(base_def, ensure_ascii=False)}")
            print(f"      期望:        {json.dumps(expected, ensure_ascii=False)}")
            print(f"      实际:        {json.dumps(actual, ensure_ascii=False)}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
