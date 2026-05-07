"""
core/translator.py — V2 task → V1 task 转换器

设计参考 MaaPipelineEditor (MPE) 的 nodeParser.ts 实现, 由 sunyink 调研引入。
仅作用于"输出"边界, 不影响 V3 算法本体 (算法始终 V2 in / V2 out)。

V1 输出规则:
  1. recognition.type → 顶层 "recognition" 字符串
     recognition.param.* → 拍平到 task 顶层
  2. action.type → 顶层 "action" 字符串
     action.param.* → 拍平到 task 顶层
  3. type 是默认值且 param 为空 → 整段省略 (DirectHit / DoNothing)
  4. 其他 task 顶层字段 (next/timeout/post_delay/...) 不变

可逆性:
  V1 → V2: parser 自动还原 (V1/V2 信息容量等价, 因为 reco/action 字段名空间不冲突)
  V2 → V1: 本函数实现, 拍平到 task 顶层, 字段顺序由调用方 (或编辑器) 处理
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict


# 默认 type — V1 模式下若 type 为这些且 param 为空, 整段省略
DEFAULT_RECO_TYPE = "DirectHit"
DEFAULT_ACTION_TYPE = "DoNothing"


def task_v2_to_v1(task_v2: Dict[str, Any]) -> Dict[str, Any]:
    """把单个 V2 task 转换为 V1 形态。

    输入: V2 task dict (可能是 minimal mod 的一个 task, 也可能是工作区某个 task)
    输出: V1 task dict (字段拍平到顶层)

    转换不应损失信息 — 因为 reco/action 字段名空间不冲突。
    若调用方传入了不规范的 V2 dict (含同名字段)、本函数仍以 task 原顶层字段优先,
    然后是 reco param, 最后 action param 覆盖 (与 MPE 一致)。
    """
    if not isinstance(task_v2, dict):
        return task_v2

    out: Dict[str, Any] = {}

    # 第一遍: 收集非 recognition/action 的顶层字段
    for k, v in task_v2.items():
        if k in ("recognition", "action"):
            continue
        out[k] = v

    # 第二遍: 处理 recognition
    reco = task_v2.get("recognition")
    if isinstance(reco, dict):
        r_type = reco.get("type")
        r_param = reco.get("param", {}) or {}

        is_default = (r_type == DEFAULT_RECO_TYPE) and not r_param
        if not is_default:
            if r_type:
                out["recognition"] = r_type
            for pk, pv in r_param.items():
                out[pk] = pv
    elif isinstance(reco, str):
        # 输入已经是 V1 形态 (recognition 是字符串), 透传
        out["recognition"] = reco
    # reco 不存在或不是 dict/str → 不写 recognition

    # 第三遍: 处理 action
    act = task_v2.get("action")
    if isinstance(act, dict):
        a_type = act.get("type")
        a_param = act.get("param", {}) or {}

        is_default = (a_type == DEFAULT_ACTION_TYPE) and not a_param
        if not is_default:
            if a_type:
                out["action"] = a_type
            for pk, pv in a_param.items():
                out[pk] = pv
    elif isinstance(act, str):
        out["action"] = act

    return out


def pipeline_v2_to_v1(
    pipeline_v2: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """整个 pipeline 文件 (含多个 task) 的 V2 → V1 转换。
    用于 mount 写工作区或 unmount 写 mod 时的批量转换。
    """
    return {name: task_v2_to_v1(td) for name, td in pipeline_v2.items()}


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    print("translator 自检 (V2 → V1)")
    print("─" * 60)

    cases = []

    # case 1: 标准 OCR + Click → V1 拍平
    cases.append((
        "OCR + Click 标准转换",
        {
            "TaskA": {
                "recognition": {"type": "OCR", "param": {"expected": ["确定"], "threshold": 0.7}},
                "action":      {"type": "Click", "param": {"target": [10, 20, 30, 40]}},
                "next": ["TaskB"],
                "post_delay": 1000,
            }
        },
        {
            "TaskA": {
                "next": ["TaskB"],
                "post_delay": 1000,
                "recognition": "OCR",
                "expected": ["确定"],
                "threshold": 0.7,
                "action": "Click",
                "target": [10, 20, 30, 40],
            }
        },
    ))

    # case 2: 默认 reco (DirectHit + 空 param) → 完全省略 recognition
    cases.append((
        "默认 DirectHit + 空 param → 省略",
        {
            "TaskB": {
                "recognition": {"type": "DirectHit", "param": {}},
                "action": {"type": "Click", "param": {"target": [1, 2, 3, 4]}},
                "next": ["End"],
            }
        },
        {
            "TaskB": {
                "next": ["End"],
                "action": "Click",
                "target": [1, 2, 3, 4],
            }
        },
    ))

    # case 3: 默认 action (DoNothing + 空 param) → 完全省略 action
    cases.append((
        "默认 DoNothing + 空 param → 省略",
        {
            "TaskC": {
                "recognition": {"type": "OCR", "param": {"expected": ["确定"]}},
                "action": {"type": "DoNothing", "param": {}},
                "next": ["X"],
            }
        },
        {
            "TaskC": {
                "next": ["X"],
                "recognition": "OCR",
                "expected": ["确定"],
            }
        },
    ))

    # case 4: 双默认 → 只剩 task 顶层字段
    cases.append((
        "双默认 → 只剩 next/post_delay 等",
        {
            "TaskD": {
                "recognition": {"type": "DirectHit", "param": {}},
                "action": {"type": "DoNothing", "param": {}},
                "next": ["X"],
                "post_delay": 500,
            }
        },
        {
            "TaskD": {
                "next": ["X"],
                "post_delay": 500,
            }
        },
    ))

    # case 5: DirectHit 但 param 非空 → 仍写出 + 拍平
    cases.append((
        "DirectHit + 非空 param → 写出",
        {
            "TaskE": {
                "recognition": {"type": "DirectHit", "param": {"roi": [10, 20, 30, 40]}},
                "next": ["X"],
            }
        },
        {
            "TaskE": {
                "next": ["X"],
                "recognition": "DirectHit",
                "roi": [10, 20, 30, 40],
            }
        },
    ))

    # case 6: minimal mod 形态 (recognition 没 type, 仅 param)
    cases.append((
        "minimal mod 形态: recognition 仅 param 子字段",
        {
            "TaskF": {
                "recognition": {"param": {"expected": ["新值"]}},
            }
        },
        {
            "TaskF": {
                "expected": ["新值"],
            }
        },
    ))
    # 解释: recognition 没 type 则 V1 不写 recognition 字符串,
    # 只把 param 内容拍到顶层。这与 V2 原意保持一致 — V2 没写 type
    # 表示 type 沿用 base, V1 同样不写 recognition 表示沿用 base。

    # case 7: 输入已经是 V1 (recognition 是字符串) — 透传
    cases.append((
        "输入已是 V1 形态 (recognition 是字符串)",
        {
            "TaskG": {
                "recognition": "OCR",
                "expected": ["x"],
                "next": ["Y"],
            }
        },
        {
            "TaskG": {
                "next": ["Y"],
                "recognition": "OCR",
                "expected": ["x"],
            }
        },
    ))

    # case 8: 不写 recognition 也不写 action 的 task (例如只改 timeout)
    cases.append((
        "只有顶层标量字段",
        {"TaskH": {"timeout": 30000}},
        {"TaskH": {"timeout": 30000}},
    ))

    # case 9: pipeline_v2_to_v1 多 task 批量
    pipeline_in = {
        "T1": {"recognition": {"type": "OCR", "param": {"expected": ["a"]}}, "next": ["End"]},
        "T2": {"recognition": {"type": "DirectHit", "param": {}}, "next": ["End"]},
    }
    pipeline_expected = {
        "T1": {"next": ["End"], "recognition": "OCR", "expected": ["a"]},
        "T2": {"next": ["End"]},
    }
    cases.append(("pipeline 批量转换", pipeline_in, pipeline_expected))

    all_ok = True
    for i, (name, input_data, expected) in enumerate(cases, 1):
        if i == 9:
            actual = pipeline_v2_to_v1(input_data)
        else:
            actual = {k: task_v2_to_v1(v) for k, v in input_data.items()}
        ok = (actual == expected)
        if ok:
            print(f"  ✓ case {i}: {name}")
        else:
            all_ok = False
            print(f"  ✗ case {i}: {name}")
            print(f"      期望: {json.dumps(expected, ensure_ascii=False)}")
            print(f"      实际: {json.dumps(actual, ensure_ascii=False)}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
