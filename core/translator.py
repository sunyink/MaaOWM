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


def _sub_v2_to_v1(sub: Any) -> Any:
    """把 And/Or 内的 sub-node V1 化拍平。

    输入可能是:
      - string (引用其他 task 名) → 不动, 直接返回
      - dict (内联 sub):
          {"sub_name": "Main_OCR", "recognition": {"type":"OCR", "param":{...}}}
        转为 V1 形态:
          {"sub_name": "Main_OCR", "recognition": "OCR", "expected": [...], ...}
    """
    if not isinstance(sub, dict):
        return sub

    out: Dict[str, Any] = {}
    for k, v in sub.items():
        if k == "recognition":
            continue
        out[k] = v

    reco = sub.get("recognition")
    if isinstance(reco, dict):
        r_type = reco.get("type")
        r_param = reco.get("param", {}) or {}
        # sub 内的 DirectHit + 空 param 也省略 (与顶层 task 一致)
        is_default = (r_type == DEFAULT_RECO_TYPE) and not r_param
        if not is_default:
            if r_type:
                out["recognition"] = r_type
            for pk, pv in r_param.items():
                # 防御性: sub 理论上不应再嵌 And/Or, 但语法上可能,
                # 递归处理保持一致
                if pk in ("all_of", "any_of") and isinstance(pv, list):
                    pv = [_sub_v2_to_v1(item) for item in pv]
                out[pk] = pv
    elif isinstance(reco, str):
        out["recognition"] = reco

    return out


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
                # ★ V0.6.2: And/Or 的 sub-node 数组递归 V1 化
                # 每个内联 sub-node 形如 {sub_name, recognition:{type,param}}
                # 递归调用 sub_v2_to_v1 拍平为 {sub_name, recognition:"OCR", expected:[...]}
                if pk in ("all_of", "any_of") and isinstance(pv, list):
                    pv = [_sub_v2_to_v1(item) for item in pv]
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
# next / on_error 紧凑写法 (独立于 V1/V2)
# ============================================================
# MaaFW parser 同时支持两种写法:
#   紧凑: ["TaskName", "[JumpBack]TaskName", "[Anchor]TaskName", "[Anchor][JumpBack]TaskName"]
#   完整: [{"name": "TaskName", "anchor": false, "jump_back": false}, ...]
# 本模块默认输出紧凑形态 (开发者可读性极佳, 与 base 写法风格一致)。

NODE_REF_FIELDS = ("next", "on_error", "interrupt")


def simplify_node_ref(item: Any) -> Any:
    """单个节点引用 (next/on_error 数组的一个元素): object → 紧凑字符串。

    输入可能形态:
      "TaskA"                              → 已是紧凑, 透传
      "[JumpBack]TaskA"                    → 已是紧凑, 透传
      {name, anchor, jump_back}            → 转紧凑
      其他 (异常)                          → 透传
    """
    if isinstance(item, str):
        return item

    if not isinstance(item, dict):
        return item

    name = item.get("name")
    if not isinstance(name, str):
        return item   # 不规范, 不动

    anchor = bool(item.get("anchor", False))
    jump_back = bool(item.get("jump_back", False))

    # 普通跳转 → 纯字符串
    if not anchor and not jump_back:
        return name

    # 加前缀
    prefix = ""
    if anchor:
        prefix += "[Anchor]"
    if jump_back:
        prefix += "[JumpBack]"
    return prefix + name


def simplify_node_refs_in_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """对一个 task 的 next/on_error/interrupt 字段做紧凑转换 (in-place)。

    返回的是同一个 dict, 方便链式调用。
    """
    if not isinstance(task, dict):
        return task

    for field in NODE_REF_FIELDS:
        refs = task.get(field)
        if not isinstance(refs, list):
            continue
        task[field] = [simplify_node_ref(r) for r in refs]

    return task


def simplify_node_refs_in_pipeline(
    pipeline: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """对整个 pipeline 字典批量做紧凑转换 (in-place)。"""
    for task_def in pipeline.values():
        simplify_node_refs_in_task(task_def)
    return pipeline


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

    # case 10: ★ V0.6.2 And 子嵌套递归 V1 化
    cases.append((
        "And 子嵌套 V1 递归拍平",
        {
            "TaskAnd": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {
                                "sub_name": "Main_OCR",
                                "recognition": {
                                    "type": "OCR",
                                    "param": {"expected": ["抽抽乐"], "roi": [98, 656, 66, 32]},
                                },
                            },
                            "Global_Main_Clr",
                        ],
                    },
                },
            }
        },
        {
            "TaskAnd": {
                "recognition": "And",
                "all_of": [
                    {
                        "sub_name": "Main_OCR",
                        "recognition": "OCR",
                        "expected": ["抽抽乐"],
                        "roi": [98, 656, 66, 32],
                    },
                    "Global_Main_Clr",
                ],
            }
        },
    ))

    # case 11: Or 子嵌套递归 V1
    cases.append((
        "Or.any_of V1 递归拍平",
        {
            "TaskOr": {
                "recognition": {
                    "type": "Or",
                    "param": {
                        "any_of": [
                            {
                                "recognition": {
                                    "type": "ColorMatch",
                                    "param": {"lower": [[10]], "upper": [[200]]},
                                },
                            },
                        ],
                    },
                },
            }
        },
        {
            "TaskOr": {
                "recognition": "Or",
                "any_of": [
                    {
                        "recognition": "ColorMatch",
                        "lower": [[10]],
                        "upper": [[200]],
                    },
                ],
            }
        },
    ))

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

    # ============================================================
    # next/on_error 紧凑写法测试
    # ============================================================
    print()
    print("translator 自检 (next 紧凑写法)")
    print("─" * 60)

    ref_cases = [
        # (name, input_item, expected)
        ("纯字符串透传",
            "TaskA",
            "TaskA"),
        ("已带前缀字符串透传",
            "[JumpBack]TaskA",
            "[JumpBack]TaskA"),
        ("普通跳转 dict → 字符串",
            {"name": "TaskB", "anchor": False, "jump_back": False},
            "TaskB"),
        ("仅 jump_back → [JumpBack] 前缀",
            {"name": "TaskC", "anchor": False, "jump_back": True},
            "[JumpBack]TaskC"),
        ("仅 anchor → [Anchor] 前缀",
            {"name": "TaskD", "anchor": True, "jump_back": False},
            "[Anchor]TaskD"),
        ("anchor + jump_back → 双前缀",
            {"name": "TaskE", "anchor": True, "jump_back": True},
            "[Anchor][JumpBack]TaskE"),
        ("dict 缺字段也能转",
            {"name": "TaskF"},
            "TaskF"),
        ("非法元素透传",
            42,
            42),
    ]
    for name, inp, exp in ref_cases:
        actual = simplify_node_ref(inp)
        ok = (actual == exp)
        all_ok = all_ok and ok
        print(f"  {'✓' if ok else '✗'} {name}: {actual!r}")

    # task 级测试
    task_in = {
        "Foo": {
            "recognition": {"type": "OCR", "param": {}},
            "next": [
                {"name": "A", "anchor": False, "jump_back": False},
                {"name": "B", "anchor": False, "jump_back": True},
            ],
            "on_error": [
                {"name": "Err", "anchor": False, "jump_back": False},
            ],
        }
    }
    task_expected = {
        "Foo": {
            "recognition": {"type": "OCR", "param": {}},
            "next": ["A", "[JumpBack]B"],
            "on_error": ["Err"],
        }
    }
    actual = {k: simplify_node_refs_in_task(dict(v)) for k, v in task_in.items()}
    # simplify_node_refs_in_task 是 in-place, 但 dict(v) 是浅拷贝, 不影响 task_in
    # 注意 next 列表的元素是 dict, 浅拷贝 v 后 v["next"] 还是同一个 list, 会被改
    # 重新写一个 deepcopy 避免污染 task_in
    import copy
    task_in_copy = copy.deepcopy(task_in)
    actual = {k: simplify_node_refs_in_task(v) for k, v in task_in_copy.items()}
    ok = (actual == task_expected)
    all_ok = all_ok and ok
    print(f"  {'✓' if ok else '✗'} task 级 next + on_error 同时简化")
    if not ok:
        print(f"      期望: {json.dumps(task_expected, ensure_ascii=False)}")
        print(f"      实际: {json.dumps(actual, ensure_ascii=False)}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
