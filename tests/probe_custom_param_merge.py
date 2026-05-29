#!/usr/bin/env python3
"""
probe_custom_param_merge.py — 验证 MaaFW 对 custom_action_param /
custom_recognition_param 的合并语义：整体替换还是 dict-merge？

测试方法：
  base pipeline: Custom 任务含完整 custom_action_param (多个字段)
  mod  pipeline: 同名任务只覆盖 custom_action_param 中的一个字段
  → canonicalize_overlay(base, mod) 后看 get_node_data 的 custom_action_param

预期：
  若 MaaFW 整体替换 → mod 只剩改了的字段，其余字段丢失
  若 MaaFW dict-merge → 所有字段保留，只有被改字段变新值

用法：
  python tests/probe_custom_param_merge.py [overlay_config.json]
  (默认自动读取根目录 overlay_config.json 里的 maa_pkg_dir)
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from core import oracle  # type: ignore


# ─────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────

def _write_pipeline(directory: pathlib.Path, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pipeline.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _section(title: str) -> None:
    print("\n" + "─" * 60)
    print(title)
    print("─" * 60)


# ─────────────────────────────────────────────────────────────
# 测试用例
# ─────────────────────────────────────────────────────────────

TASK_NAME = "ProbeTask"

BASE_FULL_PARAM = {
    "filter_regex": "(\\d+)",
    "number_mode": "int",
    "sort_mode": "asc",
    "pick_index": 1,
    "replacement_list": [[230, 120], [230, 175], [230, 230]],
    "target_node": "SomeTargetTask",
    "target_param": "target",
}

MOD_PARTIAL_PARAM = {
    # 只改了 replacement_list，其余字段省略
    "replacement_list": [[190, 100], [190, 145], [190, 190]],
}


def run_test(base_dir: pathlib.Path, mod_dir: pathlib.Path, label: str) -> None:
    _section(f"测试: {label}")

    result = oracle.canonicalize_overlay(base_dir, mod_dir)
    node = result.get(TASK_NAME)
    if node is None:
        print("  ✗ get_node_data 返回 None，任务未找到")
        return

    act = node.get("action", {})
    act_param = act.get("param", {}) if isinstance(act, dict) else {}
    cap = act_param.get("custom_action_param")

    print(f"  action.type        = {act.get('type') if isinstance(act, dict) else act!r}")
    print(f"  custom_action      = {act_param.get('custom_action')!r}")
    print(f"  custom_action_param= {json.dumps(cap, ensure_ascii=False)}")
    print()

    base_keys  = set(BASE_FULL_PARAM.keys())
    got_keys   = set(cap.keys()) if isinstance(cap, dict) else set()
    lost_keys  = base_keys - got_keys
    extra_keys = got_keys - base_keys

    if not isinstance(cap, dict):
        print("  ✗ custom_action_param 不是 dict")
        return

    if lost_keys:
        print(f"  [FAIL] fields LOST (atomic replace): {sorted(lost_keys)}")
    else:
        print(f"  [OK]   all base fields kept (dict-merge)")

    if extra_keys:
        print(f"  [?]    unexpected extra fields: {sorted(extra_keys)}")

    rl = cap.get("replacement_list")
    expected_rl = MOD_PARTIAL_PARAM["replacement_list"]
    if rl == expected_rl:
        print(f"  [OK]   replacement_list updated to mod value")
    else:
        print(f"  [?]    replacement_list: {rl!r} (expected {expected_rl!r})")


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    # 读 maa_pkg_dir
    cfg_path = ROOT_DIR / "overlay_config.json"
    if len(sys.argv) > 1:
        cfg_path = pathlib.Path(sys.argv[1])

    maa_pkg_dir = None
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        mp = cfg.get("maa_pkg_dir")
        if mp:
            maa_pkg_dir = pathlib.Path(mp)

    print("初始化 oracle...")
    pkg = oracle.init(maa_pkg_dir)
    print(f"  maa 包: {pkg}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)

        # ── Case 1: V1 base + V1 mod (partial custom_action_param) ──
        base_v1 = tmp / "base_v1"
        _write_pipeline(base_v1, {
            TASK_NAME: {
                "recognition": "DirectHit",
                "action": "Custom",
                "custom_action": "OCR_RankAndPatch",
                "custom_action_param": BASE_FULL_PARAM,
            }
        })
        mod_v1_partial = tmp / "mod_v1_partial"
        _write_pipeline(mod_v1_partial, {
            TASK_NAME: {
                "custom_action_param": MOD_PARTIAL_PARAM,
            }
        })
        run_test(base_v1, mod_v1_partial, "V1 base + V1 mod (partial custom_action_param)")

        # ── Case 2: V1 base + V1 mod (full custom_action_param，对照组) ──
        mod_v1_full = tmp / "mod_v1_full"
        _write_pipeline(mod_v1_full, {
            TASK_NAME: {
                "custom_action_param": {**BASE_FULL_PARAM,
                                        "replacement_list": MOD_PARTIAL_PARAM["replacement_list"]},
            }
        })
        run_test(base_v1, mod_v1_full, "V1 base + V1 mod (full custom_action_param，对照)")

        # ── Case 3: V2 base + V2 mod (partial，action 整段只含 param.custom_action_param) ──
        full_cap = {**BASE_FULL_PARAM}
        base_v2 = tmp / "base_v2"
        _write_pipeline(base_v2, {
            TASK_NAME: {
                "recognition": {"type": "DirectHit"},
                "action": {
                    "type": "Custom",
                    "param": {
                        "custom_action": "OCR_RankAndPatch",
                        "custom_action_param": full_cap,
                    },
                },
            }
        })
        mod_v2_partial = tmp / "mod_v2_partial"
        _write_pipeline(mod_v2_partial, {
            TASK_NAME: {
                "action": {
                    "type": "Custom",
                    "param": {
                        "custom_action": "OCR_RankAndPatch",
                        "custom_action_param": MOD_PARTIAL_PARAM,
                    },
                },
            }
        })
        run_test(base_v2, mod_v2_partial, "V2 base + V2 mod (partial custom_action_param)")

        # ── Case 0: 探 MaaFW 实际 V2 canonical 里 custom reco/action 的字段名 ──
        _section("Case 0: MaaFW V2 canonical 字段名诊断")
        diag_base = tmp / "diag_base"
        _write_pipeline(diag_base, {
            "DiagCustomAction": {
                "recognition": "DirectHit",
                "action": "Custom",
                "custom_action": "TestAction",
                "custom_action_param": {"key_a": 1, "key_b": "hello"},
            },
            "DiagCustomReco": {
                "recognition": "Custom",
                "custom_recognition": "TestReco",
                "custom_recognition_param": {"key_x": 1, "key_y": "world"},
                "action": "DoNothing",
            },
        })
        diag_canon = oracle.canonicalize(diag_base)

        node_act = diag_canon.get("DiagCustomAction", {})
        act_obj = node_act.get("action", {})
        print("  [DiagCustomAction] action V2 structure:")
        print(f"    action.type  = {act_obj.get('type')!r}")
        act_p = act_obj.get("param", {})
        for k, v in sorted(act_p.items()):
            print(f"    action.param.{k} = {json.dumps(v, ensure_ascii=False)}")

        print()
        node_reco = diag_canon.get("DiagCustomReco", {})
        reco_obj = node_reco.get("recognition", {})
        print("  [DiagCustomReco] recognition V2 structure:")
        print(f"    recognition.type  = {reco_obj.get('type')!r}")
        reco_p = reco_obj.get("param", {})
        for k, v in sorted(reco_p.items()):
            print(f"    recognition.param.{k} = {json.dumps(v, ensure_ascii=False)}")

        # ── Case 4: canonical_base 本身是否保留 custom_action_param 所有字段 ──
        _section("Case 4: canonicalize(base_v1)  -- base 单独加载时的 canonical")
        cb = oracle.canonicalize(base_v1)
        node_cb = cb.get(TASK_NAME, {})
        act_cb = node_cb.get("action", {})
        cap_cb = act_cb.get("param", {}).get("custom_action_param") if isinstance(act_cb, dict) else None
        print(f"  custom_action_param = {json.dumps(cap_cb, ensure_ascii=False)}")
        expected_keys = set(BASE_FULL_PARAM.keys())
        got_keys_cb = set(cap_cb.keys()) if isinstance(cap_cb, dict) else set()
        missing = expected_keys - got_keys_cb
        if missing:
            print(f"  [FAIL] canonical_base itself missing fields: {sorted(missing)}  <-- MaaFW issue")
        else:
            print(f"  [OK]   canonical_base has all {len(got_keys_cb)} fields  --> loss is NOT here")

        # ── Case 5: deep_diff 对 canonical_w(full) vs canonical_base(full) 产出什么 ──
        # 模拟用户只改了 replacement_list，workspace canonical 里其他字段和 base 一样
        import sys as _sys
        _sys.path.insert(0, str(ROOT_DIR))
        from core import diff as owm_diff  # type: ignore
        from core import translator as owm_translator  # type: ignore
        import copy

        canonical_w_sim = copy.deepcopy(cb)  # workspace = base canonical
        # 模拟用户改了 replacement_list
        w_task = canonical_w_sim[TASK_NAME]
        w_act_param = w_task["action"]["param"]
        w_act_param["custom_action_param"] = {
            **BASE_FULL_PARAM,
            "replacement_list": MOD_PARTIAL_PARAM["replacement_list"],
        }

        _section("Case 5: deep_diff(canonical_w, canonical_base) 产出的 mod delta")
        diff_result = owm_diff.compute_minimal_mod(canonical_w_sim, cb)
        mod_delta = diff_result.minimal_mod.get(TASK_NAME, {})
        print(f"  raw V2 delta keys: {sorted(mod_delta.keys())}")
        if "action" in mod_delta:
            act_delta = mod_delta["action"]
            param_delta = act_delta.get("param", {}) if isinstance(act_delta, dict) else {}
            cap_delta = param_delta.get("custom_action_param") if isinstance(param_delta, dict) else None
            print(f"  action.param.custom_action_param in delta = {json.dumps(cap_delta, ensure_ascii=False)}")
            if isinstance(cap_delta, dict):
                lost = expected_keys - set(cap_delta.keys())
                if lost:
                    print(f"  [FAIL] deep_diff STRIPPED these fields: {sorted(lost)}  <-- OWM deep_diff issue")
                else:
                    print(f"  [OK]   deep_diff kept all fields")

        # V1 变换后的 mod task
        v1_task = owm_translator.task_v2_to_v1(mod_delta)
        cap_v1 = v1_task.get("custom_action_param")
        print(f"  V1 mod task 里的 custom_action_param = {json.dumps(cap_v1, ensure_ascii=False)}")
        if isinstance(cap_v1, dict):
            lost_v1 = expected_keys - set(cap_v1.keys())
            if lost_v1:
                print(f"  [FAIL] V1 mod missing fields after translator: {sorted(lost_v1)}")
            else:
                print(f"  [OK]   V1 mod has all fields")

    print("\nDone.")


if __name__ == "__main__":
    main()
