"""
core/fixup.py — MaaFW 5.10.0b2 兼容补丁

问题:
  PipelineDumper 输出 sub_recognition (And/Or 内的 inline sub) 形态:
    { "type": "OCR", "param": {...}, "sub_name": "X" }
  但 PipelineParser 期望:
    { "recognition": "OCR", ..., "sub_name": "X" }      (V1)
    { "recognition": { "type": "OCR", "param": {...} }, "sub_name": "X" }   (V2)
  Dumper 输出的形态 parser 读不回来, type 退到 DirectHit, 真实字段全丢。

修复:
  在 oracle.canonicalize 输出后、写入工作区前, 把 sub object 转成 V2 嵌套形态,
  parser 能正确读回, round-trip 闭合。

未来:
  待 MaaFW 上游修复 (上报中) 后, 此模块整体删除即可。

测试:
  - 经 verify_fixup.py 在 1364 task / 223 sub 的真实 base 上验证, round-trip 闭合
"""

from __future__ import annotations

from typing import Any, Dict


def fixup_sub_recognition(canonical: Dict[str, dict]) -> int:
    """In-place 修复 canonical 字典里的所有 sub_recognition。

    把每个 inline sub object 从 dumper 形态:
        {"type": "OCR", "param": {...}, "sub_name": "X"}
    转换为 parser 认识的 V2 嵌套形态:
        {"recognition": {"type": "OCR", "param": {...}}, "sub_name": "X"}

    字符串引用 (节点名) 不动。已经是 parser 形态的也不动 (幂等)。
    返回修改的 sub object 数量, 用于诊断/日志。
    """
    fixed = 0
    for task_data in canonical.values():
        if not isinstance(task_data, dict):
            continue
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
            for sub in sub_list:
                if not isinstance(sub, dict):
                    continue              # 字符串引用, 跳过
                if "recognition" in sub:
                    continue              # 已是 parser 形态 (幂等)
                if "type" not in sub:
                    continue              # 不是 dumper 形态

                sub_type = sub.pop("type")
                sub_param = sub.pop("param", {})
                sub["recognition"] = {"type": sub_type, "param": sub_param}
                fixed += 1
    return fixed


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    import json
    print("fixup 自检")
    print("─" * 60)

    test_cases = []

    # case 1: 普通 inline sub 转换
    test_cases.append((
        "普通 And inline sub",
        {
            "TaskA": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {"type": "OCR", "param": {"expected": ["X"]}, "sub_name": "ocr1"}
                        ]
                    }
                }
            }
        },
        {
            "TaskA": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {"recognition": {"type": "OCR", "param": {"expected": ["X"]}}, "sub_name": "ocr1"}
                        ]
                    }
                }
            }
        },
        1,  # 期望修改数
    ))

    # case 2: 字符串引用不动
    test_cases.append((
        "字符串引用 sub 跳过",
        {
            "TaskB": {
                "recognition": {
                    "type": "Or",
                    "param": {"any_of": ["Global_Foo", "Global_Bar"]}
                }
            }
        },
        {
            "TaskB": {
                "recognition": {
                    "type": "Or",
                    "param": {"any_of": ["Global_Foo", "Global_Bar"]}
                }
            }
        },
        0,
    ))

    # case 3: 混合 (inline + 字符串引用)
    test_cases.append((
        "混合 sub: inline 转, 字符串引用不转",
        {
            "TaskC": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {"type": "OCR", "param": {"expected": ["A"]}, "sub_name": "x"},
                            "Global_Y"
                        ]
                    }
                }
            }
        },
        {
            "TaskC": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {"recognition": {"type": "OCR", "param": {"expected": ["A"]}}, "sub_name": "x"},
                            "Global_Y"
                        ]
                    }
                }
            }
        },
        1,
    ))

    # case 4: 非 And/Or 不处理
    test_cases.append((
        "非 And/Or 的 task 不处理",
        {
            "TaskD": {
                "recognition": {
                    "type": "OCR",
                    "param": {"expected": ["确定"]}
                }
            }
        },
        {
            "TaskD": {
                "recognition": {
                    "type": "OCR",
                    "param": {"expected": ["确定"]}
                }
            }
        },
        0,
    ))

    # case 5: 幂等 — 已经是 parser 形态再调一次不变
    test_cases.append((
        "已 fixup 的再调一次保持不变 (幂等)",
        {
            "TaskE": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {"recognition": {"type": "OCR", "param": {}}, "sub_name": "x"}
                        ]
                    }
                }
            }
        },
        {
            "TaskE": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {"recognition": {"type": "OCR", "param": {}}, "sub_name": "x"}
                        ]
                    }
                }
            }
        },
        0,
    ))

    all_ok = True
    for i, (name, input_data, expected, expected_count) in enumerate(test_cases, 1):
        import copy
        actual = copy.deepcopy(input_data)
        actual_count = fixup_sub_recognition(actual)
        ok_data = actual == expected
        ok_count = actual_count == expected_count
        if ok_data and ok_count:
            print(f"  ✓ case {i}: {name}")
        else:
            all_ok = False
            print(f"  ✗ case {i}: {name}")
            if not ok_data:
                print(f"    期望: {json.dumps(expected, ensure_ascii=False)}")
                print(f"    实际: {json.dumps(actual, ensure_ascii=False)}")
            if not ok_count:
                print(f"    期望修改数: {expected_count}, 实际: {actual_count}")

    return all_ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
