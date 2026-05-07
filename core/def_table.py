"""
core/def_table.py — 按 recognition/action type 探 def 表 + 剥离工具

设计原则:
  1. 动态白名单: 探针成功的 type 进白名单, 失败的静默跳过 (不影响其他 type)
  2. 异常容错: 任何探针抛异常, 当作"探针失败"处理, 不让 V3 流程崩
  3. 保守剥离: 白名单外的 type, 该字段整段保留 (绝不漏剥)

为什么这样:
  - 探针失败的 type (如 NeuralNetworkClassifier 没装模型) 不能瞎猜 def
  - 用户 base 里若有未列出的新 type, V3 仍能跑通, 只是不剥离那部分
  - 优雅地处理"MaaFW 升级带新 type / 拿掉旧 type"的演进

数据流:
  build_def_tables(base_dir) → DefTables (含 reco/action/wait_freezes/task_top)
  → 存在 .maaowm/def_tables.json
  → unmount 时读出
  → strip_mod_with_def(mod, def_tables, canonical_w) → 按 type 查表剥离

剥离规则:
  对每个 mod 中 task 的 嵌套 dict 字段 (recognition/action/wait_freezes/attach/anchor):
    - 嵌套 dict 内每个子字段值 == def 表对应字段值 → 删除该子字段
    - 嵌套 dict 全部子字段被删后 → 删除整个嵌套字段
  
  type 来源 (用于查 def 表):
    1. mod 自己写了 type → 用 mod 的 type
    2. mod 没写 type     → 用 worktime canonical 同 task 的 type
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# stderr 静默 — 屏蔽 MaaFW C 层探针时的预期报错
# ============================================================

@contextlib.contextmanager
def _silence_stderr():
    """临时把 fd 2 (C 层 stderr) 重定向到 null。

    Python 的 sys.stderr 重定向不影响 C 库 (MaaFW.dll), 必须重定向 fd 2。
    此函数在 Windows / Unix 都兼容。
    探针失败的 ERR log 是预期行为, 用户不需要看到。
    """
    saved = None
    try:
        sys.stderr.flush()
    except Exception:
        pass
    try:
        saved = os.dup(2)
        with open(os.devnull, "wb") as devnull:
            os.dup2(devnull.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)
    except (OSError, AttributeError):
        # 极少数环境 (Jupyter / 嵌入式) 拿不到 fd 2, 退化为不静默
        if saved is not None:
            try:
                os.close(saved)
            except OSError:
                pass
        yield


# ============================================================
# 已知 type 清单 (来自 Pipeline Protocol 文档)
# 不在白名单里的 type 探针时静默跳过, 该 task 的 param 整段保留
# ============================================================

RECO_TYPES_TO_PROBE = [
    "DirectHit",
    "OCR",
    "TemplateMatch",
    "FeatureMatch",
    "ColorMatch",
    "NeuralNetworkClassifier",
    "NeuralNetworkDetector",
    "Custom",
    # And / Or 不探 — param 是列表, 路 D 已整段保留, 不进剥离
]

ACTION_TYPES_TO_PROBE = [
    "DoNothing",
    "Click",
    "LongPress",
    "Swipe",
    "MultiSwipe",
    "ClickKey",      # 注: 用户文档里也叫 "Key", 但 parser 标准化为 ClickKey
    "LongPressKey",
    "InputText",
    "StartApp",
    "StopApp",
    "StopTask",
    "Touch",
    "TouchUp",
    "Scroll",
    "Shell",
    "Custom",
    "Command",
]


# ============================================================
# 数据结构
# ============================================================

@dataclasses.dataclass
class DefTables:
    """探针成功的 def 表集合。

    白名单 = reco_param.keys() / action_param.keys()
    没在白名单的 type, 处理时整段保留。
    """
    reco_param: Dict[str, dict]            # { reco_type: param def }
    action_param: Dict[str, dict]          # { action_type: param def }
    wait_freezes: dict                     # 单一类型, 直接是 param def
    task_top: dict                         # task 顶层嵌套字段 (attach/anchor)
    failed_types: List[str]                # 探针失败的 type 名 (供日志/诊断)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "DefTables":
        d = json.loads(text)
        return cls(
            reco_param=d["reco_param"],
            action_param=d["action_param"],
            wait_freezes=d["wait_freezes"],
            task_top=d["task_top"],
            failed_types=d.get("failed_types", []),
        )

    @property
    def reco_whitelist(self) -> set:
        return set(self.reco_param.keys())

    @property
    def action_whitelist(self) -> set:
        return set(self.action_param.keys())


# ============================================================
# 探针 (容错)
# ============================================================

def _probe_one_task(
    base_dir: pathlib.Path,
    probe_task_def: dict,
) -> Optional[dict]:
    """加载 base + 一个最简 probe task, 返回 probe 的 canonical。

    任何异常 (oracle 报错, parser 拒收, 字段缺失) 一律返回 None。
    永远不让本函数把异常向上抛。
    """
    PROBE = "__owm_def_probe__"
    try:
        from maa.resource import Resource  # type: ignore
    except ImportError:
        return None

    try:
        res = Resource()
        res.post_pipeline(str(base_dir)).wait()
        if not res.loaded:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            probe_file = pathlib.Path(tmp) / "_probe.json"
            probe_file.write_text(
                json.dumps({PROBE: probe_task_def}), encoding="utf-8"
            )
            # 静默 MaaFW 探针失败时的 [ERR] 输出 (预期行为, 不让用户看到)
            with _silence_stderr():
                res.post_pipeline(str(tmp)).wait()
            if not res.loaded:
                return None

        return res.get_node_data(PROBE)
    except Exception:
        return None


# 进程级缓存: 同一进程内对同 base_dir 的探针结果只跑一次
# (探针会触发 MaaFW 的 [ERR] stderr 输出, 缓存后避免反复挂卸载时刷屏)
_def_tables_cache: Dict[str, DefTables] = {}


def clear_def_tables_cache() -> None:
    """清空进程级缓存 (一般不需要; 测试 / base 路径变化时可手动清)。"""
    _def_tables_cache.clear()


def build_def_tables(base_dir: pathlib.Path, verbose: bool = False) -> DefTables:
    """构建 def 表。探针失败的 type 静默跳过, 写日志。

    base_dir: pipeline 目录的绝对路径
    verbose:  True 时打印探针进度 (用于 verify 脚本)

    进程级缓存: 同一 base_dir 在本进程内已探过, 直接返回缓存。
    """
    cache_key = str(base_dir.resolve())
    cached = _def_tables_cache.get(cache_key)
    if cached is not None:
        if verbose:
            print(f"  (复用进程缓存: {len(cached.reco_param)} reco / {len(cached.action_param)} action)")
        return cached

    reco_param: Dict[str, dict] = {}
    action_param: Dict[str, dict] = {}
    wait_freezes: dict = {}
    task_top: dict = {}
    failed: List[str] = []

    if verbose:
        print("  探 recognition: ", end="", flush=True)
    for rt in RECO_TYPES_TO_PROBE:
        canon = _probe_one_task(base_dir, {"recognition": rt})
        if canon is None:
            failed.append(f"recognition:{rt}")
            if verbose:
                print(f"✗{rt} ", end="", flush=True)
            continue
        reco = canon.get("recognition") if isinstance(canon, dict) else None
        if not isinstance(reco, dict) or reco.get("type") != rt:
            actual = reco.get("type") if isinstance(reco, dict) else None
            failed.append(f"recognition:{rt}(parser→{actual})")
            if verbose:
                print(f"⚠{rt}({actual}) ", end="", flush=True)
            continue

        reco_param[rt] = reco.get("param", {})
        if verbose:
            print(f"{rt}({len(reco_param[rt])}) ", end="", flush=True)

        # 顺便记 task 顶层 def + wait_freezes def (任意一次成功探针的副产物即可)
        if not task_top:
            task_top = {
                k: v for k, v in canon.items()
                if k not in ("recognition", "action",
                             "pre_wait_freezes", "post_wait_freezes",
                             "repeat_wait_freezes")
            }
        if not wait_freezes:
            wf = canon.get("pre_wait_freezes")
            if isinstance(wf, dict):
                wait_freezes = wf
    if verbose:
        print()

    if verbose:
        print("  探 action: ", end="", flush=True)
    for at in ACTION_TYPES_TO_PROBE:
        # 用 DirectHit 当 recognition (确保 task 能加载), 设目标 action type
        canon = _probe_one_task(base_dir, {
            "recognition": "DirectHit",
            "action": at,
        })
        if canon is None:
            failed.append(f"action:{at}")
            if verbose:
                print(f"✗{at} ", end="", flush=True)
            continue
        act = canon.get("action") if isinstance(canon, dict) else None
        if not isinstance(act, dict) or act.get("type") != at:
            actual = act.get("type") if isinstance(act, dict) else None
            failed.append(f"action:{at}(parser→{actual})")
            if verbose:
                print(f"⚠{at}({actual}) ", end="", flush=True)
            continue

        action_param[at] = act.get("param", {})
        if verbose:
            print(f"{at}({len(action_param[at])}) ", end="", flush=True)
    if verbose:
        print()

    result = DefTables(
        reco_param=reco_param,
        action_param=action_param,
        wait_freezes=wait_freezes,
        task_top=task_top,
        failed_types=failed,
    )
    _def_tables_cache[cache_key] = result
    return result


# ============================================================
# 剥离 (按白名单, 黑名单整段保留)
# ============================================================

def _strip_dict_by_def(target: dict, def_dict: dict) -> int:
    """对 target dict 内每个键, 值等于 def_dict 同名键 → 删除。
    嵌套 dict 也递归。返回删除字段数。
    """
    if not isinstance(target, dict) or not isinstance(def_dict, dict):
        return 0
    removed = 0
    for key in list(target.keys()):
        if key not in def_dict:
            continue
        t_val, d_val = target[key], def_dict[key]
        if isinstance(t_val, dict) and isinstance(d_val, dict):
            inner = _strip_dict_by_def(t_val, d_val)
            removed += inner
            if not t_val:
                del target[key]
                removed += 1
        elif t_val == d_val:
            del target[key]
            removed += 1
    return removed


def _resolve_type(
    mod_field_value: dict,
    canonical_w: Optional[Dict[str, dict]],
    task_name: str,
    field: str,
) -> Optional[str]:
    """决定查 def 表用的 type:
      1. mod 字段里写了 type → 用它
      2. 否则去 worktime canonical 找 → 用它
      3. 都没有 → None (该字段不剥离)
    """
    t = mod_field_value.get("type") if isinstance(mod_field_value, dict) else None
    if t:
        return t
    if canonical_w and task_name in canonical_w:
        w_field = canonical_w[task_name].get(field, {})
        if isinstance(w_field, dict):
            return w_field.get("type")
    return None


def _strip_sub_recognition(sub_node: Any, def_tables: "DefTables") -> int:
    """剥离 And/Or 内的 sub-recognition (在 all_of/any_of 数组里).

    输入可能是:
      - string (引用其他 task 名) → 不动, 返回 0
      - dict (内联 sub):
          {"sub_name": "OCR", "recognition": {"type":"OCR", "param":{...}}}
        剥离规则:
          1. 按 recognition.type 剥 recognition.param 内 def 字段
          2. recognition.param 全空 → 删 param
          3. sub_name == recognition.type → 删 sub_name (parser 会自动回填)
    返回删除字段数。
    """
    if not isinstance(sub_node, dict):
        return 0
    removed = 0

    reco = sub_node.get("recognition")
    if isinstance(reco, dict):
        r_type = reco.get("type")
        if r_type and r_type in def_tables.reco_param:
            param = reco.get("param")
            if isinstance(param, dict):
                removed += _strip_dict_by_def(param, def_tables.reco_param[r_type])
                if not param:
                    del reco["param"]
                    removed += 1

        # sub_name == reco.type 时, parser 会自动用 type 名作为 sub_name
        # (parse_sub_recognition 第 1980-1982 行)
        if r_type and sub_node.get("sub_name") == r_type:
            del sub_node["sub_name"]
            removed += 1

    return removed


def strip_mod_with_def(
    mod: Dict[str, dict],
    def_tables: DefTables,
    canonical_w: Optional[Dict[str, dict]] = None,
) -> int:
    """对 mod (in-place) 应用 def 剥离, 返回删除的字段总数。

    canonical_w: worktime 的 canonical, 用于查 task 当前 type
                 (mod 自己若没写 type 字段, 我们仍要知道用哪个 def 表)

    剥离规则 (V0.6.1, verify_workspace_minimal_v2 验证 round-trip 闭合):
      1. recognition.param 内字段按 type 剥
      2. action.param 内字段按 type 剥
      3. wait_freezes 内字段按其 def 剥 (单一类型)
      4. attach/anchor 嵌套 dict 内字段按 task_top def 剥
      5. task 顶层标量/列表字段按 task_top def 剥 (★ V0.6.1 加)
      6. And 的 box_index == 0 删 (★ V0.6.1 加)
      7. And/Or 的 sub-recognition 数组递归剥 (★ V0.6.1 加)
    """
    total = 0
    # 顶层"非 def 字段域"白名单 — 这些字段由专用逻辑处理, 不参与通用顶层剥离
    SPECIAL_TOP_KEYS = {
        "recognition", "action",
        "pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes",
        "attach", "anchor",
    }

    for task_name, task_def in mod.items():
        if not isinstance(task_def, dict):
            continue

        # ── 1. recognition.param ──
        reco = task_def.get("recognition")
        if isinstance(reco, dict):
            r_type = _resolve_type(reco, canonical_w, task_name, "recognition")
            if r_type and r_type in def_tables.reco_param:
                param = reco.get("param")
                if isinstance(param, dict):
                    total += _strip_dict_by_def(param, def_tables.reco_param[r_type])
                    if not param:
                        del reco["param"]
                        total += 1
                if not reco:
                    del task_def["recognition"]
                    total += 1
                    reco = None  # 后续 6/7 规则的引用一致

            # ── 6. And box_index == 0 删 ──
            if r_type == "And" and isinstance(reco, dict):
                param = reco.get("param", {})
                if isinstance(param, dict) and param.get("box_index") == 0:
                    del param["box_index"]
                    total += 1

            # ── 7. And/Or 子嵌套递归剥 ──
            if r_type in ("And", "Or") and isinstance(reco, dict):
                param = reco.get("param", {})
                if isinstance(param, dict):
                    arr_key = "all_of" if r_type == "And" else "any_of"
                    arr = param.get(arr_key)
                    if isinstance(arr, list):
                        for sub in arr:
                            total += _strip_sub_recognition(sub, def_tables)

        # ── 2. action.param ──
        act = task_def.get("action")
        if isinstance(act, dict):
            a_type = _resolve_type(act, canonical_w, task_name, "action")
            if a_type and a_type in def_tables.action_param:
                param = act.get("param")
                if isinstance(param, dict):
                    total += _strip_dict_by_def(param, def_tables.action_param[a_type])
                    if not param:
                        del act["param"]
                        total += 1
                if not act:
                    del task_def["action"]
                    total += 1

        # ── 3. wait_freezes (单一 type) ──
        for key in ("pre_wait_freezes", "post_wait_freezes", "repeat_wait_freezes"):
            wf = task_def.get(key)
            if isinstance(wf, dict) and def_tables.wait_freezes:
                total += _strip_dict_by_def(wf, def_tables.wait_freezes)
                if not wf:
                    del task_def[key]
                    total += 1

        # ── 4. task 顶层嵌套 (attach/anchor) ──
        for key in ("attach", "anchor"):
            d = task_def.get(key)
            d_def = def_tables.task_top.get(key) if def_tables.task_top else None
            if isinstance(d, dict) and isinstance(d_def, dict):
                total += _strip_dict_by_def(d, d_def)
                if not d:
                    del task_def[key]
                    total += 1

        # ── 5. ★ task 顶层标量/列表字段按 task_top def 剥 ──
        # 例如: enabled:true, inverse:false, max_hit:4294967295, on_error:[],
        #       post_delay:200, pre_delay:200, rate_limit:1000, repeat:1,
        #       repeat_delay:0, timeout:20000, focus:None
        # 排除 SPECIAL_TOP_KEYS (它们由 1-4 规则单独处理)
        for key in list(task_def.keys()):
            if key in SPECIAL_TOP_KEYS:
                continue
            if key not in def_tables.task_top:
                continue   # 不在 def 表里, 是 extras 或非 def 字段, 保留
            if task_def[key] == def_tables.task_top[key]:
                del task_def[key]
                total += 1

    return total


# ============================================================
# 自检 — 合成数据测剥离逻辑
# ============================================================

def _self_test() -> bool:
    print("def_table 自检")
    print("─" * 60)

    # 合成 def 表
    def_tables = DefTables(
        reco_param={
            "OCR": {
                "expected": [],
                "threshold": 0.3,
                "roi": [0, 0, 0, 0],
                "color_filter": "",
                "model": "",
                "only_rec": False,
                "replace": [],
                "index": 0,
                "order_by": "Horizontal",
                "roi_offset": [0, 0, 0, 0],
            },
            "ColorMatch": {
                "lower": [],
                "upper": [],
                "method": 4,
                "count": 1,
                "connected": False,
                "roi": [0, 0, 0, 0],
                "roi_offset": [0, 0, 0, 0],
                "index": 0,
                "order_by": "Horizontal",
            },
            "DirectHit": {
                "roi": [0, 0, 0, 0],
                "roi_offset": [0, 0, 0, 0],
            },
        },
        action_param={
            "Click": {
                "target": True,
                "target_offset": [0, 0, 0, 0],
                "contact": 0,
                "pressure": 1,
            },
            "DoNothing": {},
        },
        wait_freezes={
            "method": 5,
            "rate_limit": 1000,
            "target": True,
            "target_offset": [0, 0, 0, 0],
            "threshold": 0.95,
            "time": 0,
            "timeout": 20000,
        },
        task_top={
            "attach": {},
            "anchor": {},
            "enabled": True,
            "inverse": False,
            "max_hit": 4294967295,
            "on_error": [],
            "post_delay": 200,
            "pre_delay": 200,
            "rate_limit": 1000,
            "repeat": 1,
            "repeat_delay": 0,
            "timeout": 20000,
            "focus": None,
            "next": [],
        },
        failed_types=["recognition:NeuralNetworkClassifier"],
    )

    cases = []

    # case 1: OCR.param 部分字段是 def, 部分用户改了 → 剥离 def 字段
    cases.append((
        "OCR.param def 字段剥离",
        {
            "TaskA": {
                "recognition": {
                    "type": "OCR",
                    "param": {
                        "expected": ["新值"],         # 用户值, 保留
                        "threshold": 0.3,             # def, 剥
                        "roi": [10, 20, 30, 40],      # 用户值, 保留
                        "color_filter": "",           # def, 剥
                        "model": "",                  # def, 剥
                        "only_rec": False,            # def, 剥
                        "replace": [],                # def, 剥
                        "index": 0,                   # def, 剥
                        "order_by": "Horizontal",     # def, 剥
                        "roi_offset": [0, 0, 0, 0],   # def, 剥
                    },
                }
            }
        },
        {
            "TaskA": {
                "recognition": {
                    "type": "OCR",
                    "param": {
                        "expected": ["新值"],
                        "roi": [10, 20, 30, 40],
                    },
                }
            }
        },
    ))

    # case 2: ColorMatch.param 全是 def → 整 param 删除, 仅留 type
    cases.append((
        "ColorMatch.param 全 def 剥光",
        {
            "TaskB": {
                "recognition": {
                    "type": "ColorMatch",
                    "param": {
                        "lower": [], "upper": [], "method": 4, "count": 1,
                        "connected": False, "roi": [0, 0, 0, 0],
                        "roi_offset": [0, 0, 0, 0], "index": 0,
                        "order_by": "Horizontal",
                    },
                }
            }
        },
        {
            "TaskB": {"recognition": {"type": "ColorMatch"}}
        },
    ))

    # case 3: 黑名单 type (NN) → 整段保留, 不剥
    cases.append((
        "黑名单 type 不剥离",
        {
            "TaskC": {
                "recognition": {
                    "type": "NeuralNetworkClassifier",
                    "param": {
                        "labels": ["a", "b"],
                        "model": "x.onnx",
                        "roi": [0, 0, 0, 0],
                    },
                }
            }
        },
        {
            "TaskC": {
                "recognition": {
                    "type": "NeuralNetworkClassifier",
                    "param": {
                        "labels": ["a", "b"],
                        "model": "x.onnx",
                        "roi": [0, 0, 0, 0],
                    },
                }
            }
        },
    ))

    # case 4: action.param def 剥离
    cases.append((
        "Click.param def 剥离",
        {
            "TaskD": {
                "action": {
                    "type": "Click",
                    "param": {
                        "target": [100, 100, 50, 50],   # 用户值
                        "target_offset": [0, 0, 0, 0],   # def
                        "contact": 0,                    # def
                        "pressure": 1,                   # def
                    },
                }
            }
        },
        {
            "TaskD": {
                "action": {
                    "type": "Click",
                    "param": {"target": [100, 100, 50, 50]},
                }
            }
        },
    ))

    # case 5: wait_freezes 部分剥离
    cases.append((
        "wait_freezes def 剥离",
        {
            "TaskE": {
                "post_wait_freezes": {
                    "method": 5,                     # def
                    "rate_limit": 1000,              # def
                    "target": True,                  # def
                    "target_offset": [0, 0, 0, 0],   # def
                    "threshold": 0.95,               # def
                    "time": 1000,                    # 用户改了!
                    "timeout": 20000,                # def
                },
            }
        },
        {
            "TaskE": {"post_wait_freezes": {"time": 1000}}
        },
    ))

    # case 6: attach 部分剥离 (你的 dict-merge 修正版语义)
    cases.append((
        "attach 含用户键, 不剥",
        {
            "TaskF": {"attach": {"custom_note": "调试中"}},
        },
        {
            "TaskF": {"attach": {"custom_note": "调试中"}},
        },
    ))

    # case 7: type 来自 worktime canonical (mod 自己没写 type)
    cases.append((
        "type 从 worktime canonical 推断",
        {
            "TaskG": {
                "recognition": {
                    "param": {"expected": ["新"]},   # 没 type, 但用户改了 expected
                }
            }
        },
        {
            "TaskG": {"recognition": {"param": {"expected": ["新"]}}},
        },
    ))
    # 上面 case 7 的 canonical_w 在外面的 ok 标注里传

    # case 8: 嵌套 dict 剥光 → 整字段删
    cases.append((
        "wait_freezes 全 def → 整字段删",
        {
            "TaskH": {
                "pre_wait_freezes": {
                    "method": 5, "rate_limit": 1000, "target": True,
                    "target_offset": [0, 0, 0, 0], "threshold": 0.95,
                    "time": 0, "timeout": 20000,
                }
            }
        },
        {"TaskH": {}},
    ))

    # case 9: ★ V0.6.1 顶层标量字段按 task_top def 剥
    cases.append((
        "顶层标量字段 def 剥 (V0.6.1)",
        {
            "TaskI": {
                "enabled": True,           # def → 删
                "inverse": False,          # def → 删
                "max_hit": 4294967295,     # def → 删
                "post_delay": 200,         # def → 删
                "pre_delay": 1000,         # 用户值, 保留
                "rate_limit": 1000,        # def → 删
                "repeat": 1,               # def → 删
                "timeout": 30000,          # 用户值, 保留
                "next": ["X"],             # 用户值, 保留 (next 是列表, 通常不进 def)
            }
        },
        {
            "TaskI": {
                "pre_delay": 1000,
                "timeout": 30000,
                "next": ["X"],
            }
        },
    ))

    # case 10: ★ V0.6.1 And box_index == 0 删 + 子嵌套递归剥
    cases.append((
        "And.box_index 默认 0 删 + 子嵌套剥离",
        {
            "TaskJ": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "box_index": 0,
                        "all_of": [
                            {
                                "sub_name": "OCR",          # == reco.type → 删
                                "recognition": {
                                    "type": "OCR",
                                    "param": {
                                        "expected": ["确定"],
                                        "threshold": 0.3,    # def → 删
                                        "roi": [0, 0, 0, 0], # def → 删
                                    },
                                },
                            },
                            "Global_External",   # 字符串引用, 不动
                            {
                                "sub_name": "我的注释别名",   # != type, 保留
                                "recognition": {
                                    "type": "ColorMatch",
                                    "param": {
                                        "lower": [[10, 20, 30]],
                                        "upper": [[200, 200, 200]],
                                    },
                                },
                            },
                        ],
                    },
                },
            }
        },
        {
            "TaskJ": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {
                                "recognition": {
                                    "type": "OCR",
                                    "param": {"expected": ["确定"]},
                                },
                            },
                            "Global_External",
                            {
                                "sub_name": "我的注释别名",
                                "recognition": {
                                    "type": "ColorMatch",
                                    "param": {
                                        "lower": [[10, 20, 30]],
                                        "upper": [[200, 200, 200]],
                                    },
                                },
                            },
                        ],
                    },
                },
            }
        },
    ))

    # case 11: ★ V0.6.1 Or 子嵌套剥离 (任一)
    cases.append((
        "Or.any_of 子嵌套剥离",
        {
            "TaskK": {
                "recognition": {
                    "type": "Or",
                    "param": {
                        "any_of": [
                            {
                                "sub_name": "OCR",
                                "recognition": {
                                    "type": "OCR",
                                    "param": {
                                        "expected": ["X"],
                                        "threshold": 0.3,    # def
                                    },
                                },
                            },
                        ],
                    },
                },
            }
        },
        {
            "TaskK": {
                "recognition": {
                    "type": "Or",
                    "param": {
                        "any_of": [
                            {
                                "recognition": {
                                    "type": "OCR",
                                    "param": {"expected": ["X"]},
                                },
                            },
                        ],
                    },
                },
            }
        },
    ))

    # case 12: ★ V0.6.1 黑名单 type 在子嵌套也不剥
    cases.append((
        "And 子嵌套含黑名单 type → 不剥",
        {
            "TaskL": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {
                                "sub_name": "NeuralNetworkClassifier",
                                "recognition": {
                                    "type": "NeuralNetworkClassifier",
                                    "param": {
                                        "labels": ["a", "b"],
                                        "model": "x.onnx",
                                        "roi": [0, 0, 0, 0],
                                    },
                                },
                            },
                        ],
                    },
                },
            }
        },
        # 期望: 整段保留 (NN 不在白名单, sub_name 也保留, 因为 sub_name == type 但 type 不在白名单 — 谨慎不动)
        # 实际行为: sub_name == reco.type 仍然会触发删除 (无关白名单 — parser 总会回填)
        # 所以 sub_name 会被删, 但 param 不动
        {
            "TaskL": {
                "recognition": {
                    "type": "And",
                    "param": {
                        "all_of": [
                            {
                                "recognition": {
                                    "type": "NeuralNetworkClassifier",
                                    "param": {
                                        "labels": ["a", "b"],
                                        "model": "x.onnx",
                                        "roi": [0, 0, 0, 0],
                                    },
                                },
                            },
                        ],
                    },
                },
            }
        },
    ))

    import copy
    all_ok = True
    for i, (name, mod_in, mod_expected) in enumerate(cases, 1):
        mod_actual = copy.deepcopy(mod_in)
        canonical_w_dummy = None
        if i == 7:
            # case 7: 提供 worktime canonical 让 type 推断生效
            canonical_w_dummy = {
                "TaskG": {"recognition": {"type": "OCR", "param": {}}}
            }
        strip_mod_with_def(mod_actual, def_tables, canonical_w_dummy)
        ok = (mod_actual == mod_expected)
        if ok:
            print(f"  ✓ case {i}: {name}")
        else:
            all_ok = False
            print(f"  ✗ case {i}: {name}")
            print(f"      期望: {json.dumps(mod_expected, ensure_ascii=False)}")
            print(f"      实际: {json.dumps(mod_actual, ensure_ascii=False)}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
