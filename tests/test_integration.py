#!/usr/bin/env python3
"""
集成测试：模拟完整的 挂载→编辑→Diff→回写 流程。

创建 base 和 PC 覆盖包的目录结构，验证正向合并和逆向 Diff 的正确性。
"""

import json
import shutil
import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import OverlayConfig
from core.merger import merge_to_workspace
from core.differ import compute_diff
from core.writer import write_back

# ============================================================
#  测试夹具
# ============================================================

TEST_DIR = Path(__file__).parent / "_test_workspace"


def setup_test_dirs():
    """创建模拟的项目目录结构。"""
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)

    # === Base 层 ===
    base_pipeline = TEST_DIR / "base" / "pipeline"
    base_pipeline.mkdir(parents=True)

    # base/pipeline/main.json — 通用入口
    (base_pipeline / "main.json").write_text(
        json.dumps(
            {
                "StartTask": {
                    "next": ["CheckLogin", "EnterMainMenu"],
                },
                "CheckLogin": {
                    "recognition": "TemplateMatch",
                    "template": "login_btn.png",
                    "action": "Click",
                    "next": ["WaitMainMenu"],
                },
                "EnterMainMenu": {
                    "recognition": "OCR",
                    "expected": "主菜单",
                    "action": "Click",
                    "next": ["SelectFunction"],
                },
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )

    # base/pipeline/fishing.json — 钓鱼功能
    (base_pipeline / "fishing.json").write_text(
        json.dumps(
            {
                "StartFishing": {
                    "recognition": "TemplateMatch",
                    "template": "fishing_icon.png",
                    "action": "Click",
                    "next": ["WaitFishBite", "HandlePopup"],
                    "timeout": 30000,
                },
                "WaitFishBite": {
                    "recognition": "ColorMatch",
                    "lower": [200, 50, 50],
                    "upper": [255, 100, 100],
                    "method": 4,
                    "action": "Click",
                    "next": ["CatchFish"],
                },
                "HandlePopup": {
                    "recognition": "OCR",
                    "expected": "关闭",
                    "action": "Click",
                    "next": ["StartFishing"],
                },
                "CatchFish": {
                    "recognition": "TemplateMatch",
                    "template": "fish_caught.png",
                    "action": "Click",
                    "post_delay": 500,
                    "next": ["StartFishing"],
                },
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )

    # Base 的 image 目录
    base_image = TEST_DIR / "base" / "image"
    base_image.mkdir(parents=True)
    (base_image / "login_btn.png").write_bytes(b"FAKE_PNG_BASE_LOGIN")
    (base_image / "fishing_icon.png").write_bytes(b"FAKE_PNG_BASE_FISHING")
    (base_image / "fish_caught.png").write_bytes(b"FAKE_PNG_BASE_CAUGHT")

    # base/pipeline/v2_mixed.json — v1/v2 混用测试
    (base_pipeline / "v2_mixed.json").write_text(
        json.dumps(
            {
                # v2 格式节点
                "V2TemplateNode": {
                    "recognition": {
                        "type": "TemplateMatch",
                        "param": {
                            "template": "icon.png",
                            "threshold": 0.7,
                            "order_by": "Horizontal",
                        },
                    },
                    "action": {
                        "type": "Click",
                        "param": {
                            "target": True,
                        },
                    },
                    "next": ["V1SimpleNode"],
                    "timeout": 20000,
                },
                # v1 格式节点（同文件混用）
                "V1SimpleNode": {
                    "recognition": "OCR",
                    "expected": "确认",
                    "action": "Click",
                },
                # 另一个 v2 节点
                "V2OcrNode": {
                    "recognition": {
                        "type": "OCR",
                        "param": {
                            "expected": ["开始", "Start"],
                            "threshold": 0.3,
                        },
                    },
                    "action": {"type": "DoNothing"},
                    "next": [],
                },
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )

    # === PC 覆盖层（初始状态：有一些覆盖） ===
    pc_pipeline = TEST_DIR / "PC" / "pipeline"
    pc_pipeline.mkdir(parents=True)

    # PC 端钓鱼：修改了超时和颜色范围
    (pc_pipeline / "fishing.json").write_text(
        json.dumps(
            {
                "StartFishing": {
                    "timeout": 60000,  # PC 端超时更长
                },
                "WaitFishBite": {
                    "lower": [180, 40, 40],  # PC 端颜色范围不同
                    "upper": [255, 120, 120],
                },
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )

    # PC 端 combat.json：模拟"原本有覆盖但编辑后被还原"的场景
    # 这里写入与 base 完全一致的值（相当于覆盖已无意义）
    (base_pipeline / "combat.json").write_text(
        json.dumps(
            {"EnterCombat": {"recognition": "OCR", "expected": "战斗", "action": "Click"}},
            ensure_ascii=False, indent=4,
        ),
        encoding="utf-8",
    )
    (pc_pipeline / "combat.json").write_text(
        json.dumps(
            {"EnterCombat": {"expected": "战斗"}},  # 原本有覆盖但值和 base 一样
            ensure_ascii=False, indent=4,
        ),
        encoding="utf-8",
    )

    # PC 端图片
    pc_image = TEST_DIR / "PC" / "image"
    pc_image.mkdir(parents=True)
    (pc_image / "fishing_icon.png").write_bytes(b"FAKE_PNG_PC_FISHING_DIFFERENT")

    # PC 端 v2_mixed.json：只覆盖 v2 节点的部分 param
    (pc_pipeline / "v2_mixed.json").write_text(
        json.dumps(
            {
                "V2TemplateNode": {
                    "recognition": {
                        "param": {
                            "threshold": 0.9,  # 只改阈值，template 和 order_by 应继承 base
                        },
                    },
                    "timeout": 30000,  # 也改了一个 v1 风格的扁平字段
                },
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )

    # === 配置文件 ===
    config_data = {
        "workspace_dir": ".workspace",
        "target": "PC",
        "base_layers": ["base"],
        "resource_types": ["pipeline", "image"],
    }
    (TEST_DIR / "overlay_config.json").write_text(
        json.dumps(config_data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )

    return TEST_DIR / "overlay_config.json"


# ============================================================
#  测试用例
# ============================================================


def test_forward_merge(config):
    """测试正向合并。"""
    print("\n" + "=" * 60)
    print("TEST: 正向合并")
    print("=" * 60)

    result = merge_to_workspace(config, progress_callback=lambda m: print(f"  → {m}"))
    print(f"\n合并结果: {result.summary()}")

    ws = config.workspace_path

    # 验证 pipeline 合并
    with open(ws / "pipeline" / "fishing.json", "r", encoding="utf-8") as f:
        fishing = json.load(f)

    # StartFishing 应该有 base 的所有字段 + PC 覆盖的 timeout
    sf = fishing["StartFishing"]
    assert sf["recognition"] == "TemplateMatch", f"recognition 应继承 base, got {sf['recognition']}"
    assert sf["template"] == "fishing_icon.png", f"template 应继承 base"
    assert sf["timeout"] == 60000, f"timeout 应为 PC 覆盖值 60000, got {sf['timeout']}"
    assert sf["next"] == ["WaitFishBite", "HandlePopup"], "next 应继承 base"
    print("  ✓ StartFishing 字段级合并正确")

    # WaitFishBite 应该有 PC 覆盖的颜色范围 + base 的其他字段
    wfb = fishing["WaitFishBite"]
    assert wfb["lower"] == [180, 40, 40], "lower 应为 PC 覆盖值"
    assert wfb["method"] == 4, "method 应继承 base"
    assert wfb["action"] == "Click", "action 应继承 base"
    print("  ✓ WaitFishBite 字段级合并正确")

    # HandlePopup 应完整继承 base（PC 未覆盖）
    hp = fishing["HandlePopup"]
    assert hp["recognition"] == "OCR", "HandlePopup 应完整继承 base"
    print("  ✓ HandlePopup 完整继承 base")

    # main.json 应完整继承 base（PC 无此文件覆盖）
    with open(ws / "pipeline" / "main.json", "r", encoding="utf-8") as f:
        main = json.load(f)
    assert "StartTask" in main, "main.json 应包含 base 的节点"
    print("  ✓ main.json 完整继承 base")

    # 验证 image 合并
    # fishing_icon.png 应该是 PC 版本
    pc_icon = (ws / "image" / "fishing_icon.png").read_bytes()
    assert pc_icon == b"FAKE_PNG_PC_FISHING_DIFFERENT", "fishing_icon 应为 PC 覆盖版本"
    print("  ✓ image 文件覆盖正确")

    # login_btn.png 应该是 base 版本
    base_login = (ws / "image" / "login_btn.png").read_bytes()
    assert base_login == b"FAKE_PNG_BASE_LOGIN", "login_btn 应继承 base"
    print("  ✓ image 文件继承正确")

    # === v2 混用测试 ===
    with open(ws / "pipeline" / "v2_mixed.json", "r", encoding="utf-8") as f:
        v2_mixed = json.load(f)

    # V2TemplateNode: PC 只覆盖了 threshold，其余 param 应继承 base
    v2t = v2_mixed["V2TemplateNode"]
    rec = v2t["recognition"]
    assert isinstance(rec, dict), "v2 的 recognition 应为 dict"
    assert rec["type"] == "TemplateMatch", "type 应继承 base"
    assert rec["param"]["threshold"] == 0.9, "threshold 应为 PC 覆盖值 0.9"
    assert rec["param"]["template"] == "icon.png", "template 应继承 base（不被覆盖丢失）"
    assert rec["param"]["order_by"] == "Horizontal", "order_by 应继承 base"
    print("  ✓ V2TemplateNode recognition.param 深度合并正确")

    # action 应完整继承 base（PC 未覆盖 action）
    assert v2t["action"] == {"type": "Click", "param": {"target": True}}, "action 应完整继承"
    # timeout 应为 PC 覆盖值
    assert v2t["timeout"] == 30000, "timeout 应为 PC 覆盖值"
    # next 应继承 base
    assert v2t["next"] == ["V1SimpleNode"], "next 应继承 base"
    print("  ✓ V2TemplateNode 扁平字段合并正确")

    # V1SimpleNode: 同文件的 v1 节点应正常继承（PC 未覆盖）
    v1s = v2_mixed["V1SimpleNode"]
    assert v1s["recognition"] == "OCR", "v1 节点 recognition 应为字符串"
    assert v1s["expected"] == "确认", "v1 节点应完整继承 base"
    print("  ✓ V1SimpleNode（同文件 v1 节点）正常继承")

    # V2OcrNode: PC 未覆盖，应完整继承
    v2o = v2_mixed["V2OcrNode"]
    assert v2o["recognition"]["type"] == "OCR", "V2OcrNode 应完整继承"
    assert v2o["recognition"]["param"]["expected"] == ["开始", "Start"]
    print("  ✓ V2OcrNode 完整继承 base")

    print("\n  [PASS] 正向合并全部通过（含 v2 混用）")


def test_diff_no_edit(config):
    """测试：不做任何编辑，Diff 应该只产出原始 PC 覆盖的内容。"""
    print("\n" + "=" * 60)
    print("TEST: Diff（无编辑 — 还原原始覆盖状态）")
    print("=" * 60)

    diff = compute_diff(config)
    for line in diff.summary_lines():
        print(f"  {line}")

    # fishing.json 应该有 2 个修改节点（原始 PC 覆盖了 StartFishing 和 WaitFishBite）
    fishing_diff = diff.pipeline_diffs.get("pipeline/fishing.json")
    assert fishing_diff is not None, "应存在 fishing.json 的 diff"
    assert len(fishing_diff.modified_nodes) == 2, (
        f"应有 2 个修改节点, got {len(fishing_diff.modified_nodes)}"
    )
    assert "StartFishing" in fishing_diff.modified_nodes
    assert "WaitFishBite" in fishing_diff.modified_nodes
    print("  ✓ 修改节点数量正确")

    # StartFishing 的 diff 应该只有 timeout
    sf_diff = fishing_diff.modified_nodes["StartFishing"]
    assert sf_diff == {"timeout": 60000}, f"StartFishing diff 应只有 timeout, got {sf_diff}"
    print("  ✓ StartFishing diff 字段精确")

    # WaitFishBite 的 diff 应该只有 lower 和 upper
    wfb_diff = fishing_diff.modified_nodes["WaitFishBite"]
    assert set(wfb_diff.keys()) == {"lower", "upper"}, f"WaitFishBite diff 字段不对: {wfb_diff.keys()}"
    print("  ✓ WaitFishBite diff 字段精确")

    # main.json 应该无差异
    main_diff = diff.pipeline_diffs.get("pipeline/main.json")
    assert main_diff is not None, "main.json 应存在于 diff 结果中"
    assert not main_diff.has_changes, "main.json 应无差异"
    print("  ✓ main.json 无差异")

    # image diff
    assert "image/fishing_icon.png" in diff.image_diff.modified_files, "fishing_icon 应为修改"
    assert "image/login_btn.png" in diff.image_diff.unchanged_files, "login_btn 应为无变化"
    print("  ✓ image diff 正确")

    # === v2 混用 diff 测试 ===
    v2_diff = diff.pipeline_diffs.get("pipeline/v2_mixed.json")
    assert v2_diff is not None, "v2_mixed.json 应存在于 diff 结果中"

    # V2TemplateNode 应检测到差异（threshold + timeout）
    assert "V2TemplateNode" in v2_diff.modified_nodes, "V2TemplateNode 应有差异"
    v2t_diff = v2_diff.modified_nodes["V2TemplateNode"]

    # recognition diff 应输出最小增量（仅变化的 param 子字段）
    assert "recognition" in v2t_diff, "recognition 应在 diff 中"
    rec_diff = v2t_diff["recognition"]
    assert isinstance(rec_diff, dict), "v2 recognition diff 应为 dict"
    assert "type" not in rec_diff, "type 未变化，不应在 diff 中"
    assert rec_diff == {"param": {"threshold": 0.9}}, \
        f"recognition diff 应仅含变化的 param 子字段, got {rec_diff}"
    print("  ✓ V2TemplateNode recognition diff 输出最小增量")

    # timeout diff
    assert v2t_diff.get("timeout") == 30000, "timeout 应在 diff 中"
    # action 和 next 不应出现（未变化）
    assert "action" not in v2t_diff, "action 未变化不应在 diff 中"
    assert "next" not in v2t_diff, "next 未变化不应在 diff 中"
    print("  ✓ V2TemplateNode 扁平字段 diff 正确")

    # V1SimpleNode 应无差异
    assert v2_diff.unchanged_count >= 2, "V1SimpleNode 和 V2OcrNode 应无差异"
    print("  ✓ 同文件 v1/v2 未修改节点正确剔除")

    print("\n  [PASS] 无编辑 Diff 全部通过（含 v2 混用）")


def test_diff_after_edit(config):
    """测试：模拟在工作区中编辑后的 Diff。"""
    print("\n" + "=" * 60)
    print("TEST: Diff（模拟编辑后）")
    print("=" * 60)

    ws = config.workspace_path

    # 模拟编辑：修改 fishing.json
    with open(ws / "pipeline" / "fishing.json", "r", encoding="utf-8") as f:
        fishing = json.load(f)

    # 1. 修改 CatchFish 的 post_delay（base 有，改个值）
    fishing["CatchFish"]["post_delay"] = 1000

    # 2. 删除 StartFishing 的 next（模拟 PC 端该节点为终点）
    del fishing["StartFishing"]["next"]

    # 3. 新增一个 PC 独有的节点
    fishing["PCOnlyNode"] = {
        "recognition": "OCR",
        "expected": "PC专属",
        "action": "Click",
    }

    with open(ws / "pipeline" / "fishing.json", "w", encoding="utf-8") as f:
        json.dump(fishing, f, ensure_ascii=False, indent=4)

    # 模拟编辑：修改 v2_mixed.json 中的 v2 节点
    with open(ws / "pipeline" / "v2_mixed.json", "r", encoding="utf-8") as f:
        v2_mixed = json.load(f)

    # 4. 修改 V2OcrNode 的 param.expected（v2 嵌套字段）
    v2_mixed["V2OcrNode"]["recognition"]["param"]["expected"] = ["开始", "Start", "Начать"]

    with open(ws / "pipeline" / "v2_mixed.json", "w", encoding="utf-8") as f:
        json.dump(v2_mixed, f, ensure_ascii=False, indent=4)

    # 执行 Diff
    diff = compute_diff(config)
    for line in diff.summary_lines():
        print(f"  {line}")

    fishing_diff = diff.pipeline_diffs["pipeline/fishing.json"]

    # CatchFish 应出现在修改节点中，只有 post_delay 变化
    assert "CatchFish" in fishing_diff.modified_nodes
    cf_diff = fishing_diff.modified_nodes["CatchFish"]
    assert cf_diff["post_delay"] == 1000, f"post_delay 应为 1000, got {cf_diff.get('post_delay')}"
    print("  ✓ CatchFish 修改检测正确")

    # StartFishing 的 diff 应包含 next: []（删除标记）+ timeout 覆盖
    sf_diff = fishing_diff.modified_nodes["StartFishing"]
    assert sf_diff.get("next") == [], f"next 应为 [] (删除标记), got {sf_diff.get('next')}"
    assert sf_diff.get("timeout") == 60000, "timeout 覆盖应保留"
    print("  ✓ StartFishing 字段删除检测正确 (next → [])")

    # PCOnlyNode 应出现在新增节点中
    assert "PCOnlyNode" in fishing_diff.new_nodes
    print("  ✓ PCOnlyNode 新增节点检测正确")

    # === v2 编辑 diff ===
    v2_diff = diff.pipeline_diffs["pipeline/v2_mixed.json"]

    # V2OcrNode 应检测到 recognition 变化
    assert "V2OcrNode" in v2_diff.modified_nodes, "V2OcrNode 应有差异"
    v2o_diff = v2_diff.modified_nodes["V2OcrNode"]
    # 应输出最小增量的 recognition diff（仅变化的 param 子字段）
    assert "recognition" in v2o_diff
    assert v2o_diff["recognition"] == {"param": {"expected": ["开始", "Start", "Начать"]}}, \
        f"recognition diff 应仅含变化的 param.expected, got {v2o_diff['recognition']}"
    assert "type" not in v2o_diff["recognition"], "type 未变化不应在 diff 中"
    # action 未变化不应出现
    assert "action" not in v2o_diff, "action 未变不应在 diff 中"
    print("  ✓ V2OcrNode v2 嵌套字段编辑 diff 正确（最小增量）")

    print("\n  [PASS] 编辑后 Diff 全部通过（含 v2 混用）")


def test_write_back(config):
    """测试回写。"""
    print("\n" + "=" * 60)
    print("TEST: 回写")
    print("=" * 60)

    diff = compute_diff(config)
    result = write_back(config, diff, progress_callback=lambda m: print(f"  → {m}"))
    print(f"\n回写结果: {result.summary()}")

    target = config.target_path

    # 验证 fishing.json 回写内容
    with open(target / "pipeline" / "fishing.json", "r", encoding="utf-8") as f:
        pc_fishing = json.load(f)

    # 应该包含修改和新增的节点
    assert "StartFishing" in pc_fishing, "StartFishing 应在回写结果中"
    assert "WaitFishBite" in pc_fishing, "WaitFishBite 应在回写结果中"
    assert "CatchFish" in pc_fishing, "CatchFish 应在回写结果中"
    assert "PCOnlyNode" in pc_fishing, "PCOnlyNode 应在回写结果中"
    print("  ✓ 覆盖包包含所有有差异的节点")

    # 不应该包含完整继承的节点
    assert "HandlePopup" not in pc_fishing, "HandlePopup 无差异，不应在覆盖包中"
    print("  ✓ 无差异节点被正确剔除")

    # 验证字段内容
    assert pc_fishing["StartFishing"].get("next") == [], "next 删除标记应写入"
    assert pc_fishing["StartFishing"].get("timeout") == 60000, "timeout 应保留"
    # StartFishing 的覆盖不应包含 base 的其他字段
    assert "recognition" not in pc_fishing["StartFishing"], "recognition 应被剔除（与 base 相同）"
    assert "template" not in pc_fishing["StartFishing"], "template 应被剔除（与 base 相同）"
    print("  ✓ 覆盖字段精确无冗余")

    # main.json：PC 覆盖包原本没有此文件，无差异时不应创建
    assert not (target / "pipeline" / "main.json").exists(), \
        "main.json 原本不在覆盖包中，无差异时不应新建"
    print("  ✓ 覆盖包中未新建原本不存在的无差异文件")

    # combat.json：PC 覆盖包原本有此文件，但无差异，应保留为空 {}
    assert (target / "pipeline" / "combat.json").exists(), \
        "combat.json 原本在覆盖包中，应保留"
    with open(target / "pipeline" / "combat.json", "r", encoding="utf-8") as f:
        pc_combat = json.load(f)
    assert pc_combat == {}, f"combat.json 无差异应为空 {{}}, got {pc_combat}"
    print("  ✓ 覆盖包中原有的无差异文件保留为空 {}")

    # image: fishing_icon 应存在（不同于 base），login_btn 不应存在（与 base 相同）
    assert (target / "image" / "fishing_icon.png").exists(), "修改的图片应存在"
    assert not (target / "image" / "login_btn.png").exists(), "与 base 相同的图片应被清理"
    print("  ✓ image 回写正确")

    print("\n  [PASS] 回写全部通过")


# ============================================================
#  运行测试
# ============================================================


def main():
    print("MFABD2 Overlay Tool — 集成测试\n")

    try:
        config_path = setup_test_dirs()
        from core.config import load_config

        config = load_config(config_path)
        errors = config.validate()
        if errors:
            print(f"配置校验失败: {errors}")
            return

        test_forward_merge(config)
        test_diff_no_edit(config)
        test_diff_after_edit(config)
        test_write_back(config)

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED ✓")
        print("=" * 60)

    finally:
        # 清理测试目录
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR)
            print(f"\n[cleanup] 测试目录已清理: {TEST_DIR}")


if __name__ == "__main__":
    main()
