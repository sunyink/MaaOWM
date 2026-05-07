"""
core/extras.py — 非 MaaFW 字段 (doc/desc) + 节点顺序持久化

设计哲学:
  - oracle.canonicalize 给出的 canonical 不含 doc/desc 等"非 MaaFW 字段",
    这些字段对运行无影响, 但对开发者可读性至关重要.
  - 同样, oracle 给出的 task 顺序由 dumper 内部决定 (按字母序或 hash 序),
    base 的"叙事流"丢失.
  - 本模块在挂载时直接读 base+mod 的原始 JSON 文件, 抓出 extras + node_order
    存到 .maaowm/, 写工作区时注入回每个 task; 卸载时从工作区抓最新 extras
    写回 mod.

数据流:
  mount 端:
    扫 base 各层原始 JSON → 收 extras + 节点顺序
    扫 mod 原始 JSON → 覆盖式合并 extras (mod 优先)
    存 .maaowm/extras.json + .maaowm/node_order.json
    写工作区前: 把 extras 注入回各 task, 按 node_order 重排 task 输出顺序

  unmount 端:
    扫工作区原始 JSON → 抓最新 extras (此时工作区是 minimal 形态)
    写 mod 时: 注入 extras + 按 node_order 重排

extras 判定:
  字段不在 MaaFW 字段全集 → 是 extras
  全集 = task_top def keys ∪ {recognition, action, *_wait_freezes}
       ∪ 各 type reco/action param 字段名 (V1 拍平场景)
       ∪ 已知硬编码补全 (NN/Touch 等探针失败 type)
       ∪ sub-node 字段名 (sub_name)

  对 sub-node (And/Or 内部) 同样递归处理.

策略 (per 用户决定):
  用户在工作区删了 extras 字段 → mod 也不写 (重新挂载时从 base 注入)
  用户改了 extras 值 → mod 写新值
  用户加了新 extras → mod 加
  → diff 写回时只看"工作区当前 extras 是否非空", 不看挂载时的 A.
    挂载时记录的 extras 仅作 mount 注入参考.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import def_table
from . import oracle


# ============================================================
# MaaFW 字段全集 — 用于"什么是 extras"的判定
# ============================================================

# 已知 task 顶层 MaaFW 字段 (硬编码补全)
# 含 task_top def 字段 + 嵌套结构字段
KNOWN_TASK_TOP_KEYS = {
    "recognition", "action",
    "pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes",
    "attach", "anchor",
    # task_top def 表里的标量
    "enabled", "inverse", "max_hit", "next", "on_error",
    "post_delay", "pre_delay", "rate_limit", "repeat", "repeat_delay",
    "timeout", "focus",
    # 不常用但合法
    "interrupt",  # 5.1+ 已废弃, 但仍可能出现于老 mod
    "is_sub",     # 同上
}

# V1 拍平时 reco/action param 字段会散在 task 顶层
# 含探针失败 type 的字段 (硬编码补全)
KNOWN_RECO_PARAM_KEYS = {
    # 通用
    "roi", "roi_offset", "index", "order_by",
    # OCR
    "expected", "threshold", "replace", "only_rec", "model", "color_filter",
    # TemplateMatch
    "template", "method", "green_mask",
    # FeatureMatch
    "detector", "ratio", "count",
    # ColorMatch
    "lower", "upper", "connected",
    # NeuralNetworkClassifier / Detector (探针失败的)
    "labels",
    # Custom
    "custom_recognition", "custom_recognition_param",
    # And / Or
    "all_of", "any_of", "box_index",
}

KNOWN_ACTION_PARAM_KEYS = {
    # Click / LongPress / Touch
    "target", "target_offset", "contact", "pressure", "duration",
    # Swipe / MultiSwipe
    "begin", "end", "end_offset", "end_hold", "only_hover", "starting", "swipes",
    # Key
    "key",
    # InputText
    "input_text",
    # StartApp / StopApp
    "package",
    # Scroll
    "dx", "dy",
    # Command
    "exec", "args", "detach",
    # Custom
    "custom_action", "custom_action_param",
}

# Sub-node 内部 MaaFW 字段
KNOWN_SUBNODE_KEYS = {"sub_name", "recognition"}


def build_maafw_field_sets(def_tables: def_table.DefTables) -> Tuple[Set[str], Set[str]]:
    """合并硬编码已知集 + 动态探针, 得到当前 MaaFW 顶层 + sub 内的字段全集。

    返回:
      task_top_set:  task 顶层 MaaFW 字段名集合
      subnode_set:   sub-node 内 MaaFW 字段名集合 (V1 拍平时也用)
    """
    task_top_set = set(KNOWN_TASK_TOP_KEYS)
    task_top_set.update(def_tables.task_top.keys())

    # V1 拍平时, reco param + action param 散在顶层
    for params in def_tables.reco_param.values():
        task_top_set.update(params.keys())
    for params in def_tables.action_param.values():
        task_top_set.update(params.keys())
    task_top_set.update(KNOWN_RECO_PARAM_KEYS)
    task_top_set.update(KNOWN_ACTION_PARAM_KEYS)

    # sub-node = sub-level MaaFW keys + V1 拍平的 reco param keys
    subnode_set = set(KNOWN_SUBNODE_KEYS)
    for params in def_tables.reco_param.values():
        subnode_set.update(params.keys())
    subnode_set.update(KNOWN_RECO_PARAM_KEYS)

    return task_top_set, subnode_set


# ============================================================
# 数据结构
# ============================================================

@dataclasses.dataclass
class TaskExtras:
    """单个 task 的 extras 信息。

    top_extras: task 顶层非 MaaFW 字段 (如 doc/desc)
    sub_extras: sub-node extras, 按"sub 在数组中的下标"索引
                值是 dict{字段名: 值}
                例: {0: {"doc": "子识别注释"}, 2: {"v3_note": "..."}}
                只有 dict 形式 sub-node (内联) 才有 extras; 字符串引用没有
    """
    top_extras: Dict[str, Any] = dataclasses.field(default_factory=dict)
    sub_extras: Dict[int, Dict[str, Any]] = dataclasses.field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.top_extras and not self.sub_extras


@dataclasses.dataclass
class ExtrasSnapshot:
    """挂载时收集的 extras 总集 + node_order 索引。

    extras: { task_name: TaskExtras }
    node_order: { 文件相对路径(POSIX): [task_name 列表, 按文件原序] }
                文件归属和 .maaowm/origin.json 一致.
    """
    extras: Dict[str, TaskExtras] = dataclasses.field(default_factory=dict)
    node_order: Dict[str, List[str]] = dataclasses.field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "extras": {
                k: {
                    "top_extras": v.top_extras,
                    "sub_extras": {str(idx): val for idx, val in v.sub_extras.items()},
                }
                for k, v in self.extras.items()
            },
            "node_order": self.node_order,
        }, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ExtrasSnapshot":
        d = json.loads(text)
        extras = {}
        for k, v in d.get("extras", {}).items():
            extras[k] = TaskExtras(
                top_extras=v.get("top_extras", {}),
                sub_extras={int(i): val for i, val in v.get("sub_extras", {}).items()},
            )
        return cls(
            extras=extras,
            node_order=d.get("node_order", {}),
        )


# ============================================================
# 抽取 extras (从原始 JSON)
# ============================================================

def _is_maafw_recognition_param(field: str, parent_recognition_dict_form: bool) -> bool:
    """V2 嵌套形态下, recognition 是 dict, recognition.param 内字段都是 MaaFW.
    本函数仅在 task 顶层平铺判断时被引用 — V2 形态下 reco 的 param 内字段
    根本不在 task 顶层, 不会触及."""
    return False


def _extract_subnode_extras(
    sub_node: Any,
    subnode_set: Set[str],
) -> Dict[str, Any]:
    """从 sub-node (dict 形式) 抽取 extras (sub 内非 MaaFW 字段)。

    sub-node 字段:
      MaaFW: sub_name, recognition (V2 dict 形态), 或 V1 拍平后散列各 param 字段
      extras: 任何不在白名单的字段 (如 doc/desc)
    """
    if not isinstance(sub_node, dict):
        return {}
    extras = {}
    for k, v in sub_node.items():
        if k not in subnode_set:
            extras[k] = v
    return extras


def _extract_task_extras(
    task_data: dict,
    task_top_set: Set[str],
    subnode_set: Set[str],
) -> TaskExtras:
    """从单个 task 的原始 JSON dict 抽取 extras (顶层 + sub-node 嵌套)。"""
    top_extras = {}
    for k, v in task_data.items():
        if k in task_top_set:
            continue
        # 跳过特殊以 $ 开头的 key (V3/MPE config 标记)
        if k.startswith("$"):
            continue
        top_extras[k] = v

    # sub-node extras: 按 all_of/any_of 数组下标记录
    sub_extras: Dict[int, Dict[str, Any]] = {}
    for arr_key in ("all_of", "any_of"):
        # V1 拍平形态: all_of/any_of 在 task 顶层
        # V2 嵌套形态: 在 task.recognition.param.all_of
        for container in (task_data, task_data.get("recognition", {}).get("param", {})
                          if isinstance(task_data.get("recognition"), dict) else {}):
            if not isinstance(container, dict):
                continue
            arr = container.get(arr_key)
            if not isinstance(arr, list):
                continue
            for idx, sub in enumerate(arr):
                if not isinstance(sub, dict):
                    continue
                sub_e = _extract_subnode_extras(sub, subnode_set)
                if sub_e:
                    sub_extras[idx] = sub_e
            break  # 找到一处就停, 避免重复

    return TaskExtras(top_extras=top_extras, sub_extras=sub_extras)


def collect_extras_from_dir(
    pipeline_dir: pathlib.Path,
    def_tables: def_table.DefTables,
) -> Tuple[Dict[str, TaskExtras], Dict[str, List[str]]]:
    """扫描一个目录的所有 pipeline JSON, 抽出 extras + 节点顺序。

    返回:
      extras: { task_name: TaskExtras }
      node_order: { 文件相对路径: [task names 顺序] }
    """
    task_top_set, subnode_set = build_maafw_field_sets(def_tables)
    extras: Dict[str, TaskExtras] = {}
    node_order: Dict[str, List[str]] = {}

    for path in sorted(pipeline_dir.rglob("*.json")) + sorted(pipeline_dir.rglob("*.jsonc")):
        if any(part.startswith(".") for part in path.relative_to(pipeline_dir).parts):
            continue
        data = oracle.load_pipeline_json(path)
        if not isinstance(data, dict):
            continue

        rel = path.relative_to(pipeline_dir).as_posix()
        order = []
        for task_name, task_data in data.items():
            if task_name.startswith("$"):
                continue
            order.append(task_name)
            if isinstance(task_data, dict):
                te = _extract_task_extras(task_data, task_top_set, subnode_set)
                if not te.is_empty():
                    extras[task_name] = te
        if order:
            node_order[rel] = order

    return extras, node_order


def collect_layered_extras(
    layers: Iterable[pathlib.Path],
    mod_dir: Optional[pathlib.Path],
    def_tables: def_table.DefTables,
) -> ExtrasSnapshot:
    """按层覆盖式合并 extras。base 多层从底向上, mod 最上 (优先)。

    node_order 取自 base 层 (合并第一个出现的层); mod 自己定义的 task 在末尾.
    """
    final_extras: Dict[str, TaskExtras] = {}
    final_order: Dict[str, List[str]] = {}

    seen_in_base: Set[str] = set()  # 已被 base 层归位的 task

    # base 层依次处理
    for layer in layers:
        ex, order = collect_extras_from_dir(layer, def_tables)
        # extras 覆盖式合并
        for task_name, te in ex.items():
            final_extras[task_name] = te
        # node_order 仅在文件首次出现时记录 (后层不覆盖)
        for rel, names in order.items():
            if rel not in final_order:
                final_order[rel] = list(names)
                seen_in_base.update(names)

    # mod 层
    if mod_dir and mod_dir.exists():
        ex, order = collect_extras_from_dir(mod_dir, def_tables)
        for task_name, te in ex.items():
            final_extras[task_name] = te  # mod 优先覆盖
        for rel, names in order.items():
            if rel in final_order:
                # 同名文件 mod 也在 base, 合并 mod 新增的 task 到末尾
                existing = set(final_order[rel])
                for n in names:
                    if n not in existing:
                        final_order[rel].append(n)
            else:
                final_order[rel] = list(names)

    return ExtrasSnapshot(extras=final_extras, node_order=final_order)


# ============================================================
# 注入 extras (写入工作区前)
# ============================================================

def inject_extras_into_pipeline(
    pipeline: Dict[str, dict],
    snapshot: ExtrasSnapshot,
) -> int:
    """对 pipeline (in-place) 注入 extras。返回注入的字段数 (顶层 + sub)。"""
    injected = 0
    for task_name, task in pipeline.items():
        if not isinstance(task, dict):
            continue
        te = snapshot.extras.get(task_name)
        if te is None:
            continue

        # 注入 top_extras
        for k, v in te.top_extras.items():
            if k not in task:
                task[k] = v
                injected += 1

        # 注入 sub_extras (按 all_of/any_of 找数组)
        for arr_key in ("all_of", "any_of"):
            arr = None
            container = None
            # V1 形态: 顶层
            if arr_key in task and isinstance(task[arr_key], list):
                arr = task[arr_key]
                container = task
            # V2 形态: recognition.param 下
            elif isinstance(task.get("recognition"), dict):
                rp = task["recognition"].get("param", {})
                if isinstance(rp, dict) and isinstance(rp.get(arr_key), list):
                    arr = rp[arr_key]
                    container = rp
            if arr is None:
                continue
            for idx, sub in enumerate(arr):
                if not isinstance(sub, dict):
                    continue
                sub_e = te.sub_extras.get(idx)
                if not sub_e:
                    continue
                for k, v in sub_e.items():
                    if k not in sub:
                        sub[k] = v
                        injected += 1
            break  # 一个 task 只走一次

    return injected


def reorder_pipeline_by_node_order(
    pipeline: Dict[str, dict],
    rel_path: str,
    node_order: Dict[str, List[str]],
) -> Dict[str, dict]:
    """按 node_order 中记录的顺序重排 pipeline。

    rel_path: 当前文件的相对路径 (POSIX). 从 node_order 找该文件的顺序记录.
    顺序中没记录的 task 按字母序丢底部.
    """
    order = node_order.get(rel_path, [])
    seen = set()
    out = {}

    for name in order:
        if name in pipeline and name not in seen:
            out[name] = pipeline[name]
            seen.add(name)

    leftovers = sorted(k for k in pipeline if k not in seen)
    for name in leftovers:
        out[name] = pipeline[name]

    return out


# ============================================================
# extras diff (V0.7.2): 检测用户是否单独改了 doc/desc 等
# ============================================================

def diff_extras(
    workspace: ExtrasSnapshot,
    mount_time: ExtrasSnapshot,
) -> Set[str]:
    """对比工作区和挂载时的 extras, 返回 extras 有变更的 task 集合。

    变更判定:
      - workspace 有非空 extras 且和 mount 不同 → 视为用户改了 doc, 加入返回集合
      - workspace 该 task extras 为空 (用户整字段删了)  → 不返回
        语义: "删 doc 字段" = 撤回修改, base 重新接管 (重新挂载时从 base 注入)
        如果用户想真"清空", 应显式写 doc: "" (这就是非空 extras, 走变更分支)

    返回的 task 集合应被强制加入 minimal_mod (即使 oracle diff 看是 IDENTICAL),
    后续 inject_extras 才能把新 doc 写到产物.
    """
    changed: Set[str] = set()
    all_tasks = set(workspace.extras.keys()) | set(mount_time.extras.keys())

    for name in all_tasks:
        ws_te = workspace.extras.get(name, TaskExtras())
        mt_te = mount_time.extras.get(name, TaskExtras())

        # workspace 没有非空 extras → 用户删了, 不写 mod (撤回修改语义)
        if ws_te.is_empty():
            continue

        # workspace 非空, 和 mount 不同 → 改了或新增了
        if ws_te.top_extras != mt_te.top_extras:
            changed.add(name)
            continue
        if ws_te.sub_extras != mt_te.sub_extras:
            changed.add(name)
            continue

    return changed


# ============================================================
# 自检 — 合成数据
# ============================================================

def _self_test() -> bool:
    print("extras 自检")
    print("─" * 60)

    fake_def_tables = def_table.DefTables(
        reco_param={
            "OCR": {"expected": [], "threshold": 0.3, "roi": [0, 0, 0, 0]},
            "ColorMatch": {"lower": [], "upper": []},
        },
        action_param={
            "Click": {"target": True, "target_offset": [0, 0, 0, 0]},
        },
        wait_freezes={},
        task_top={
            "enabled": True, "inverse": False, "max_hit": 0,
            "post_delay": 200, "pre_delay": 200, "next": [],
            "on_error": [], "rate_limit": 1000, "repeat": 1,
            "repeat_delay": 0, "timeout": 0, "focus": None,
            "attach": {}, "anchor": {},
        },
        failed_types=[],
    )

    task_top_set, subnode_set = build_maafw_field_sets(fake_def_tables)
    print(f"  task_top_set 大小: {len(task_top_set)}")
    print(f"  subnode_set 大小:  {len(subnode_set)}")

    all_ok = True

    # ─── case 1: 顶层 extras 抽取 ───
    task1 = {
        "doc": "这是文档注释",
        "desc": "简短描述",
        "recognition": "OCR",
        "expected": ["按钮"],
        "next": ["X"],
    }
    te = _extract_task_extras(task1, task_top_set, subnode_set)
    ok = te.top_extras == {"doc": "这是文档注释", "desc": "简短描述"} and not te.sub_extras
    print(f"  ✓ case 1: 顶层 extras 抽取" if ok else f"  ✗ case 1: 实际 {te}")
    all_ok = all_ok and ok

    # ─── case 2: V1 形态 sub extras ───
    task2 = {
        "recognition": "And",
        "all_of": [
            {
                "sub_name": "OCR",
                "recognition": "OCR",
                "expected": ["X"],
                "doc": "子识别注释",
            },
            "Global_Ext",
            {
                "recognition": "ColorMatch",
                "lower": [[1]],
                "v3_note": "调试中",
            },
        ],
    }
    te = _extract_task_extras(task2, task_top_set, subnode_set)
    ok = (te.sub_extras == {0: {"doc": "子识别注释"}, 2: {"v3_note": "调试中"}}
          and not te.top_extras)
    print(f"  ✓ case 2: V1 sub extras (按下标)" if ok else f"  ✗ case 2: 实际 {te}")
    all_ok = all_ok and ok

    # ─── case 3: V2 嵌套形态 sub extras ───
    task3 = {
        "recognition": {
            "type": "And",
            "param": {
                "all_of": [
                    {
                        "sub_name": "OCR",
                        "recognition": {"type": "OCR", "param": {}},
                        "doc": "V2 嵌套子注释",
                    },
                ],
            },
        },
    }
    te = _extract_task_extras(task3, task_top_set, subnode_set)
    ok = te.sub_extras == {0: {"doc": "V2 嵌套子注释"}}
    print(f"  ✓ case 3: V2 sub extras" if ok else f"  ✗ case 3: 实际 {te}")
    all_ok = all_ok and ok

    # ─── case 4: 注入 extras ───
    pipeline = {
        "TaskA": {"recognition": "OCR", "expected": ["X"]},
        "TaskB": {"recognition": "DirectHit"},
    }
    snap = ExtrasSnapshot(extras={
        "TaskA": TaskExtras(top_extras={"doc": "注释 A"}),
        "TaskB": TaskExtras(top_extras={"desc": "B 描述"}),
    })
    inject_extras_into_pipeline(pipeline, snap)
    ok = (pipeline["TaskA"].get("doc") == "注释 A"
          and pipeline["TaskB"].get("desc") == "B 描述")
    print(f"  ✓ case 4: 注入 top extras" if ok else f"  ✗ case 4: 实际 {pipeline}")
    all_ok = all_ok and ok

    # ─── case 5: 注入 sub extras ───
    pipeline2 = {
        "TaskAnd": {
            "recognition": "And",
            "all_of": [
                {"sub_name": "OCR", "recognition": "OCR", "expected": ["X"]},
                "Global_Ref",
            ],
        }
    }
    snap2 = ExtrasSnapshot(extras={
        "TaskAnd": TaskExtras(sub_extras={0: {"doc": "子注释"}}),
    })
    inject_extras_into_pipeline(pipeline2, snap2)
    ok = pipeline2["TaskAnd"]["all_of"][0].get("doc") == "子注释"
    print(f"  ✓ case 5: 注入 sub extras" if ok else f"  ✗ case 5: 实际 {pipeline2}")
    all_ok = all_ok and ok

    # ─── case 6: node_order 重排 ───
    pipeline3 = {"TaskC": {}, "TaskA": {}, "TaskB": {}}
    order = {"foo.json": ["TaskA", "TaskB", "TaskC"]}
    out = reorder_pipeline_by_node_order(pipeline3, "foo.json", order)
    ok = list(out.keys()) == ["TaskA", "TaskB", "TaskC"]
    print(f"  ✓ case 6: 按 node_order 重排" if ok else f"  ✗ case 6: 实际 {list(out.keys())}")
    all_ok = all_ok and ok

    # ─── case 7: 重排时多余 task 丢底部 ───
    pipeline4 = {"TaskZ": {}, "TaskA": {}, "TaskExtra": {}}
    order2 = {"foo.json": ["TaskA", "TaskZ"]}
    out = reorder_pipeline_by_node_order(pipeline4, "foo.json", order2)
    ok = list(out.keys()) == ["TaskA", "TaskZ", "TaskExtra"]
    print(f"  ✓ case 7: 多余 task 丢底部" if ok else f"  ✗ case 7: 实际 {list(out.keys())}")
    all_ok = all_ok and ok

    # ─── case 8: 序列化往返 ───
    snap3 = ExtrasSnapshot(
        extras={
            "T": TaskExtras(top_extras={"doc": "x"}, sub_extras={0: {"d": "y"}})
        },
        node_order={"a.json": ["T1", "T2"]},
    )
    text = snap3.to_json()
    snap3_back = ExtrasSnapshot.from_json(text)
    ok = (snap3_back.extras["T"].top_extras == {"doc": "x"}
          and snap3_back.extras["T"].sub_extras == {0: {"d": "y"}}
          and snap3_back.node_order == {"a.json": ["T1", "T2"]})
    print(f"  ✓ case 8: JSON 往返序列化" if ok else f"  ✗ case 8: 失败")
    all_ok = all_ok and ok

    # ─── case 9: diff — 用户改 doc 内容 ───
    ws = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": "新值"}),
    })
    mt = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": "原值"}),
    })
    changed = diff_extras(ws, mt)
    ok = changed == {"Task1"}
    print(f"  ✓ case 9: 改 doc 内容 → 进 mod" if ok else f"  ✗ case 9: 实际 {changed}")
    all_ok = all_ok and ok

    # ─── case 10: diff — 用户没改 ───
    ws = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": "X"}),
    })
    mt = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": "X"}),
    })
    changed = diff_extras(ws, mt)
    ok = changed == set()
    print(f"  ✓ case 10: 未改 doc → 不进 mod" if ok else f"  ✗ case 10: 实际 {changed}")
    all_ok = all_ok and ok

    # ─── case 11: diff — 用户整字段删 doc (撤回修改语义) ───
    ws = ExtrasSnapshot(extras={
        "Task1": TaskExtras(),  # 空, 用户删了 doc
    })
    mt = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": "原 base 文档"}),
    })
    changed = diff_extras(ws, mt)
    ok = changed == set()
    print(f"  ✓ case 11: 删 doc → 不写 mod (base 接管)"
          if ok else f"  ✗ case 11: 实际 {changed}")
    all_ok = all_ok and ok

    # ─── case 12: diff — 显式写 doc:"" 视为修改 ───
    ws = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": ""}),  # 显式空字符串
    })
    mt = ExtrasSnapshot(extras={
        "Task1": TaskExtras(top_extras={"doc": "原 base 文档"}),
    })
    changed = diff_extras(ws, mt)
    ok = changed == {"Task1"}
    print(f"  ✓ case 12: doc:\"\" 显式清空 → 进 mod"
          if ok else f"  ✗ case 12: 实际 {changed}")
    all_ok = all_ok and ok

    # ─── case 13: diff — sub-node 内 extras 变化 ───
    ws = ExtrasSnapshot(extras={
        "Task1": TaskExtras(sub_extras={0: {"doc": "新子注释"}}),
    })
    mt = ExtrasSnapshot(extras={
        "Task1": TaskExtras(sub_extras={0: {"doc": "原子注释"}}),
    })
    changed = diff_extras(ws, mt)
    ok = changed == {"Task1"}
    print(f"  ✓ case 13: sub-node extras 变化 → 进 mod"
          if ok else f"  ✗ case 13: 实际 {changed}")
    all_ok = all_ok and ok

    # ─── case 14: diff — workspace 新增 task 含 doc ───
    ws = ExtrasSnapshot(extras={
        "NewTask": TaskExtras(top_extras={"doc": "新建注释"}),
    })
    mt = ExtrasSnapshot()
    changed = diff_extras(ws, mt)
    ok = changed == {"NewTask"}
    print(f"  ✓ case 14: 新建 task 含 doc → 进 mod"
          if ok else f"  ✗ case 14: 实际 {changed}")
    all_ok = all_ok and ok

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
