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
     ★ 仅当 base 同字段的 type 也是默认时才省略 — 见下方"默认省略的边界"
  4. 其他 task 顶层字段 (next/timeout/post_delay/...) 不变

可逆性:
  V1 → V2: parser 自动还原 (V1/V2 信息容量等价, 因为 reco/action 字段名空间不冲突)
  V2 → V1: 本函数实现, 拍平到 task 顶层, 字段顺序由调用方 (或编辑器) 处理

默认省略的边界 (V0.7.13):
  规则 3 的等价关系"V1 省略 recognition/action ≡ 该字段取框架默认值"只对
  **独立完整节点**成立。本工具的两个产物都不是独立节点, 而是 overlay 的一层:
    - 卸载产物 = mod delta        (运行时 [base, mod] 字段级合并)
    - 挂载产物 = 挂载态工作区      (运行时 [base, 工作区] 字段级合并, in-place)
  在这两层里省略的语义是"不覆盖", 不是"取默认值"。若 base 该字段是非默认
  type、而用户恰恰把它改回默认 type (Click→DoNothing / And→DirectHit),
  裸省略会让 base 的旧 type 透过合并盖回来, 改动被静默吞掉。
  故调用方须传 base 对照 (task_v2_to_v1(base_task=) / pipeline_v2_to_v1
  (canonical_base=)), 仅当 base 同字段 type 也是默认 (或 base 无此 task /
  此字段) 时才省略。这是 def 剥离"双重判定"(见 ARCHITECTURE 5.2) 在 type
  层的同款判据 — 早期只在 param 层做了, type 层漏了。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional


# 默认 type — V1 模式下若 type 为这些且 param 为空, 整段省略
DEFAULT_RECO_TYPE = "DirectHit"
DEFAULT_ACTION_TYPE = "DoNothing"


def _base_field_type(base_task: Any, field: str) -> Any:
    """取 base 同 task 的 recognition/action type。

    base_task 为 None、无该字段、字段形态异常 → None, 语义是"base 没在这个
    字段上表态", 与 base 用默认 type 等价 (对齐 strip_mod_with_def 的
    MOD_ONLY 退化, 见 ARCHITECTURE 7.4)。
    """
    if not isinstance(base_task, dict):
        return None
    v = base_task.get(field)
    if isinstance(v, dict):
        return v.get("type")
    if isinstance(v, str):
        return v            # base 已是 V1 形态时的兜底
    return None


def _may_omit_default_type(
    cur_type: Any,
    cur_param: Any,
    default_type: str,
    base_task: Any,
    field: str,
    has_base_ref: bool,
) -> bool:
    """判定「默认 type + 空 param」能否整段省略 (V0.7.13 type 层双重判定)。

    两条都成立才省略:
      1. 当前 type == 默认 type 且 param 为空
      2. base 同字段 type 也是默认 (或 base 无此 task / 此字段)

    has_base_ref=False (调用方未传 base 对照) → 退化为旧行为, 只看条件 1。
    产物是独立完整节点时才该走这条路; 产物是 overlay 的一层时必须传 base,
    否则"改回默认 type"的 override 会被 base 值盖回 (见模块 docstring)。
    """
    if cur_type != default_type or cur_param:
        return False
    if not has_base_ref:
        return True
    b_type = _base_field_type(base_task, field)
    return b_type is None or b_type == default_type


def _unwrap_single_target_array(v: Any) -> Any:
    """★ V0.7.11: 解包 dumper 的单元素目标数组包裹。

    当前 MaaFW (5.10.0b2) dumper 把 Swipe 的 end 规范成目标数组
    [[x,y,w,h]] (begin 仍是单目标 [x,y,w,h]) — V1 输出端拍回
    开发者手写的平坦形态。真·多段 (len>1) / 已平坦 → 原样。
    解包后再次 canonicalize 会被 dumper 重新包裹, diff 恒在
    canonical 层进行, 不产生幻影 diff。"""
    if isinstance(v, list) and len(v) == 1 and isinstance(v[0], list):
        return v[0]
    return v


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


def task_v2_to_v1(
    task_v2: Dict[str, Any],
    base_task: Optional[Dict[str, Any]] = None,
    has_base_ref: Optional[bool] = None,
) -> Dict[str, Any]:
    """把单个 V2 task 转换为 V1 形态。

    输入: V2 task dict (可能是 minimal mod 的一个 task, 也可能是工作区某个 task)
    输出: V1 task dict (字段拍平到顶层)

    base_task (V0.7.13): 该 task 在 canonical_base 中的对应定义 (V2 形态),
      用于「默认 type 整段省略」的双重判定。产物是 overlay 的一层 (mod delta /
      挂载态工作区) 时必须提供对照, 否则"把非默认 type 改回默认"的改动会因省略
      被 base 盖回 (见模块 docstring「默认省略的边界」)。
      base 里没有该 task (MOD_ONLY) 时传 None + has_base_ref=True。
    has_base_ref: 显式声明"调用方有 base 可对照"。默认 None = 由 base_task
      是否为 None 推断, 满足单 task 直调的直觉; 批量转换由 pipeline_v2_to_v1
      显式传 True, 好让 MOD_ONLY task 走"允许省略"而不是"无对照"。

    转换不应损失信息 — 因为 reco/action 字段名空间不冲突。
    若调用方传入了不规范的 V2 dict (含同名字段)、本函数仍以 task 原顶层字段优先,
    然后是 reco param, 最后 action param 覆盖 (与 MPE 一致)。
    """
    if not isinstance(task_v2, dict):
        return task_v2

    if has_base_ref is None:
        has_base_ref = base_task is not None

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

        is_default = _may_omit_default_type(
            r_type, r_param, DEFAULT_RECO_TYPE, base_task, "recognition", has_base_ref,
        )
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

        is_default = _may_omit_default_type(
            a_type, a_param, DEFAULT_ACTION_TYPE, base_task, "action", has_base_ref,
        )
        if not is_default:
            if a_type:
                out["action"] = a_type
            for pk, pv in a_param.items():
                # ★ V0.7.11: V1 输出端解包 dumper 的单元素目标数组包裹。
                # a_type is None = minimal mod 未写 type (type 沿用 base) 的形态;
                # action.param 层 "end" 仅 Swipe 拥有 / "swipes" 仅 MultiSwipe
                # 拥有 (探针类型表核实), reco param / task 顶层无同名字段。
                if pk == "end" and a_type in ("Swipe", None):
                    pv = _unwrap_single_target_array(pv)
                elif pk == "swipes" and a_type in ("MultiSwipe", None) and isinstance(pv, list):
                    pv = [
                        {**s, "end": _unwrap_single_target_array(s["end"])}
                        if isinstance(s, dict) and "end" in s else s
                        for s in pv
                    ]
                out[pk] = pv
    elif isinstance(act, str):
        out["action"] = act

    return out


def pipeline_v2_to_v1(
    pipeline_v2: Dict[str, Dict[str, Any]],
    canonical_base: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """整个 pipeline 文件 (含多个 task) 的 V2 → V1 转换。
    用于 mount 写工作区或 unmount 写 mod 时的批量转换。

    canonical_base (V0.7.13): base 的 canonical。两个真实调用点 (挂载写工作区 /
    卸载写 mod) 的产物都是 overlay 的一层, **都必须传** — 不传则「默认 type 整段
    省略」会吞掉"改回默认 type"的 override (见模块 docstring)。
    参数保持可选只为兼容纯格式转换的直调 (如自检里的独立节点用例)。
    """
    has_base_ref = canonical_base is not None
    base_map = canonical_base or {}
    return {
        name: task_v2_to_v1(td, base_map.get(name), has_base_ref=has_base_ref)
        for name, td in pipeline_v2.items()
    }


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
# wait_freezes 紧凑写法 (V0.7.3)
# ============================================================

_WAIT_FREEZES_KEYS = ("pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes")


def simplify_wait_freezes_in_task(task: Dict[str, Any]) -> int:
    """对单个 task 应用 wait_freezes 紧凑写法 (in-place)。返回简化数。

    parser 接受标量数字作为 wait_freezes (PipelineParser.cpp 第 1534-1538 行):
      pre_wait_freezes: 3000 等价于 pre_wait_freezes: {time: 3000}

    仅当 wait_freezes dict 仅含 time 一个字段时, 退化为标量 time 值。
    其他字段存在时保留 dict 形态.
    """
    if not isinstance(task, dict):
        return 0
    simplified = 0
    for key in _WAIT_FREEZES_KEYS:
        v = task.get(key)
        if isinstance(v, dict) and len(v) == 1 and "time" in v:
            time_val = v["time"]
            if isinstance(time_val, (int, float)) and not isinstance(time_val, bool):
                task[key] = time_val
                simplified += 1
    return simplified


def simplify_wait_freezes_in_pipeline(
    pipeline: Dict[str, Dict[str, Any]],
) -> int:
    """对整个 pipeline 应用 wait_freezes 紧凑写法 (in-place). 返回简化数."""
    total = 0
    for task in pipeline.values():
        if isinstance(task, dict):
            total += simplify_wait_freezes_in_task(task)
    return total


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

    # case 12: ★ V0.7.11 Swipe end 单元素目标数组解包
    cases.append((
        "Swipe end [[x,y,w,h]] 解包, begin 不动",
        {
            "TaskSw": {
                "action": {
                    "type": "Swipe",
                    "param": {
                        "begin": [150, 300, 1, 1],
                        "end": [[150, 700, 1, 1]],
                        "duration": 600,
                    },
                },
            }
        },
        {
            "TaskSw": {
                "action": "Swipe",
                "begin": [150, 300, 1, 1],
                "end": [150, 700, 1, 1],
                "duration": 600,
            }
        },
    ))

    # case 13: 真·多段 end (len>1) 原样保留
    cases.append((
        "Swipe end 多段 (len>1) 不解包",
        {
            "TaskSw2": {
                "action": {
                    "type": "Swipe",
                    "param": {
                        "begin": [117, 640],
                        "end": [[117, 640, 1, 1], [1200, 500, 1, 1]],
                    },
                },
            }
        },
        {
            "TaskSw2": {
                "action": "Swipe",
                "begin": [117, 640],
                "end": [[117, 640, 1, 1], [1200, 500, 1, 1]],
            }
        },
    ))

    # case 14: minimal mod 形态 (action 无 type, type 沿用 base) 也解包
    cases.append((
        "minimal mod 无 type 的 end 解包",
        {
            "TaskSw3": {
                "action": {
                    "param": {
                        "end": [[820, 90, 1, 1]],
                    },
                },
            }
        },
        {
            "TaskSw3": {
                "end": [820, 90, 1, 1],
            }
        },
    ))

    # case 15: MultiSwipe swipes[i].end 解包, 兄弟字段不动
    cases.append((
        "MultiSwipe swipes[i].end 解包",
        {
            "TaskMs": {
                "action": {
                    "type": "MultiSwipe",
                    "param": {
                        "swipes": [
                            {"starting": 0, "begin": [100, 200], "end": [[300, 400, 1, 1]]},
                            {"starting": 50, "begin": [500, 600], "end": [700, 800]},
                        ],
                    },
                },
            }
        },
        {
            "TaskMs": {
                "action": "MultiSwipe",
                "swipes": [
                    {"starting": 0, "begin": [100, 200], "end": [300, 400, 1, 1]},
                    {"starting": 50, "begin": [500, 600], "end": [700, 800]},
                ],
            }
        },
    ))

    # case 16: end 已平坦 → 透传 (幂等)
    cases.append((
        "Swipe end 已平坦透传",
        {
            "TaskSw4": {
                "action": {
                    "type": "Swipe",
                    "param": {
                        "begin": [1, 2],
                        "end": [3, 4, 1, 1],
                    },
                },
            }
        },
        {
            "TaskSw4": {
                "action": "Swipe",
                "begin": [1, 2],
                "end": [3, 4, 1, 1],
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
    # 默认 type 省略的双重判定 (V0.7.13) — overlay 层不能裸省略
    # ============================================================
    print()
    print("translator 自检 (默认 type 省略 · 双重判定)")
    print("─" * 60)

    # 实例原型: MFABD2 Event_GoinEvent — base 是 And + Click[1118,387],
    # 挂载态改成 DoNothing 后卸载, action 整个从 mod 消失 (加载后沿用 base 的 Click)
    _BASE_AND_CLICK = {
        "Event_GoinEvent": {
            "recognition": {"type": "And", "param": {
                "all_of": ["Rec_HomePage_GA_Ocr", "Rec_HomePage_GA_Clr"],
            }},
            "action": {"type": "Click", "param": {"target": [1118, 387]}},
            "post_delay": 8000,
        }
    }

    # (name, pipeline_in, canonical_base, expected)
    dual_cases = [
        (
            "base=Click, delta=DoNothing → 必须写出 action",
            {"Event_GoinEvent": {
                "action": {"type": "DoNothing"},   # 空 param 已被 def 剥离
                "post_delay": 200,
                "next": ["Event_GoinEvent_OcrCk"],
            }},
            _BASE_AND_CLICK,
            {"Event_GoinEvent": {
                "post_delay": 200,
                "next": ["Event_GoinEvent_OcrCk"],
                "action": "DoNothing",
            }},
        ),
        (
            "base=And, delta=DirectHit → 必须写出 recognition",
            {"Event_GoinEvent": {"recognition": {"type": "DirectHit", "param": {}}}},
            _BASE_AND_CLICK,
            {"Event_GoinEvent": {"recognition": "DirectHit"}},
        ),
        (
            "base 同字段也是默认 → 照旧省略 (不矫枉过正)",
            {"T": {"action": {"type": "DoNothing", "param": {}}, "next": ["X"]}},
            {"T": {"action": {"type": "DoNothing", "param": {}}, "next": []}},
            {"T": {"next": ["X"]}},
        ),
        (
            "base 无此字段 → 视作 base 用默认, 省略",
            {"T": {"action": {"type": "DoNothing", "param": {}}, "next": ["X"]}},
            {"T": {"next": []}},
            {"T": {"next": ["X"]}},
        ),
        (
            "MOD_ONLY (base 无此 task) → 整段是完整节点, 省略",
            {"NewTask": {
                "recognition": {"type": "DirectHit", "param": {}},
                "action": {"type": "DoNothing", "param": {}},
                "next": ["X"],
            }},
            _BASE_AND_CLICK,          # 不含 NewTask
            {"NewTask": {"next": ["X"]}},
        ),
        (
            "挂载端: 工作区完整节点 (base=Click, merged=DoNothing) → 写出",
            {"Event_GoinEvent": {
                "recognition": {"type": "And", "param": {
                    "all_of": ["Rec_HomePage_GA_Ocr", "Rec_HomePage_GA_Clr"],
                }},
                "action": {"type": "DoNothing", "param": {}},
                "post_delay": 200,
            }},
            _BASE_AND_CLICK,
            {"Event_GoinEvent": {
                "post_delay": 200,
                "recognition": "And",
                "all_of": ["Rec_HomePage_GA_Ocr", "Rec_HomePage_GA_Clr"],
                "action": "DoNothing",
            }},
        ),
        (
            "delta 只有 param 无 type (type 沿用 base) → 不写 action 字符串",
            {"Event_GoinEvent": {"action": {"param": {"target": [100, 200]}}}},
            _BASE_AND_CLICK,
            {"Event_GoinEvent": {"target": [100, 200]}},
        ),
    ]

    for name, pin, cbase, expected in dual_cases:
        actual = pipeline_v2_to_v1(pin, canonical_base=cbase)
        if actual == expected:
            print(f"  ✓ {name}")
        else:
            all_ok = False
            print(f"  ✗ {name}")
            print(f"      期望: {json.dumps(expected, ensure_ascii=False)}")
            print(f"      实际: {json.dumps(actual, ensure_ascii=False)}")

    # 不传 base 对照 → 退化旧行为 (向后兼容, 纯格式转换直调走这条)
    _no_base_in = {"T": {"action": {"type": "DoNothing", "param": {}}, "next": ["X"]}}
    for label, actual in (
        ("pipeline_v2_to_v1 不传 canonical_base", pipeline_v2_to_v1(_no_base_in)),
        ("task_v2_to_v1 不传 base_task",
         {"T": task_v2_to_v1(_no_base_in["T"])}),
    ):
        if actual == {"T": {"next": ["X"]}}:
            print(f"  ✓ {label} → 旧行为省略")
        else:
            all_ok = False
            print(f"  ✗ {label}: {json.dumps(actual, ensure_ascii=False)}")

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

    # ─── wait_freezes 紧凑写法自检 (V0.7.3) ───
    print()
    print("translator 自检 (wait_freezes 紧凑写法)")
    print("─" * 60)

    wf_cases = [
        # (label, 输入 task, 期望 task)
        ("仅 time 字段 → 标量",
         {"pre_wait_freezes": {"time": 3000}},
         {"pre_wait_freezes": 3000}),
        ("time + 其他字段 → 不简化",
         {"pre_wait_freezes": {"time": 3000, "threshold": 0.8}},
         {"pre_wait_freezes": {"time": 3000, "threshold": 0.8}}),
        ("time=0 也简化",
         {"pre_wait_freezes": {"time": 0}},
         {"pre_wait_freezes": 0}),
        ("post_wait_freezes 同样处理",
         {"post_wait_freezes": {"time": 500}},
         {"post_wait_freezes": 500}),
        ("repeat_wait_freezes 同样处理",
         {"repeat_wait_freezes": {"time": 100}},
         {"repeat_wait_freezes": 100}),
        ("已是标量, 不动",
         {"pre_wait_freezes": 3000},
         {"pre_wait_freezes": 3000}),
        ("空 dict 不动 (理论不该出现)",
         {"pre_wait_freezes": {}},
         {"pre_wait_freezes": {}}),
        ("3 个字段都简化",
         {
             "pre_wait_freezes": {"time": 1000},
             "post_wait_freezes": {"time": 2000},
             "repeat_wait_freezes": {"time": 3000},
         },
         {
             "pre_wait_freezes": 1000,
             "post_wait_freezes": 2000,
             "repeat_wait_freezes": 3000,
         }),
    ]
    for label, t_in, t_expected in wf_cases:
        t_actual = copy.deepcopy(t_in)
        simplify_wait_freezes_in_task(t_actual)
        ok = (t_actual == t_expected)
        all_ok = all_ok and ok
        if ok:
            print(f"  ✓ {label}: {json.dumps(t_actual, ensure_ascii=False)}")
        else:
            print(f"  ✗ {label}")
            print(f"      期望: {json.dumps(t_expected, ensure_ascii=False)}")
            print(f"      实际: {json.dumps(t_actual, ensure_ascii=False)}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
