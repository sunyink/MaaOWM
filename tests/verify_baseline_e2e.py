"""端到端验证 baseline (base') 全链路 —— 用 MFABD2 venv python 跑。

流程: 建立基线 → 检测(应无漂移) → 改基线副本模拟 base 漂移 → 检测(应报漂移)
     → 按文件复位 → 检测(应归零) → 清理 maaowm/。
全程不动真实 base/mod, 只动 maaowm/baseline/ 下我们自己的副本。
"""
import json
import pathlib
import shutil
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core import config as config_mod
from core import baseline as baseline_mod
from core import oracle

ok_all = True
def check(label, cond):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}")

cfg = config_mod.load_config(_ROOT / "overlay_config.json")
errs = cfg.validate()
if errs:
    print("配置校验失败:", errs); sys.exit(1)

print(f"maaowm_root: {cfg.maaowm_root}")
print(f"owm_dir:     {cfg.owm_dir}")
check("maaowm_root 落在被管理项目根 (含 MFABD2)", "MFABD2" in str(cfg.maaowm_root))

slug, branch, reason = baseline_mod.effective_slug(cfg)
print(f"slug={slug} branch={branch} reason={reason!r}")

maaowm_root = cfg.maaowm_root
created_maaowm = not maaowm_root.exists()
try:
    oracle.init(cfg.maa_pkg_dir)

    # 1. 建立基线
    n = baseline_mod.reset_all(cfg, slug, branch)
    check(f"reset_all 建立基线 (拷 {n} 文件 >0)", n > 0)
    check("maaowm/.gitignore 生成", (maaowm_root / ".gitignore").exists())
    check("_anchor.json 生成", baseline_mod.has_baseline(cfg, slug))

    # 2. 检测应无漂移 (基线 == 当前 base)
    rep = baseline_mod.detect_drift(cfg, slug, mounted=False)
    check("刚建立基线 → 无漂移", not rep.has_drift())

    # 3. 改基线副本里 Battle.json 的某个 threshold, 模拟 base 已前进
    layer = baseline_mod.layer_names(cfg)[0]
    bl_battle = cfg.baseline_dir(slug) / layer / "pipeline" / "Battle.json"
    raw = bl_battle.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    # 找一个带 threshold 的节点改值 (改基线=制造 base'!=base)
    touched = None
    for name, node in data.items():
        if isinstance(node, dict):
            r = node.get("recognition")
            if isinstance(r, dict) and isinstance(r.get("param"), dict) and "threshold" in r["param"]:
                r["param"]["threshold"] = 0.123456
                touched = name; break
            if "threshold" in node:   # V1 拍平
                node["threshold"] = 0.123456
                touched = name; break
    if touched is None:
        # 退而求其次: 给第一个节点塞个明显不同的字段值
        first = next(iter(data))
        data[first]["post_delay"] = 987654
        touched = first
    bl_battle.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  (在基线副本里改了节点 {touched})")

    rep2 = baseline_mod.detect_drift(cfg, slug, mounted=False)
    check("改基线副本后 → 检测到漂移", rep2.has_drift())
    battle_fd = [f for f in rep2.files if f.rel.endswith("Battle.json") and f.nodes]
    check("漂移定位到 Battle.json", len(battle_fd) == 1)
    if battle_fd:
        names = [nd.name for nd in battle_fd[0].nodes]
        check(f"漂移节点含 {touched}", touched in names)
        nd = next(nd for nd in battle_fd[0].nodes if nd.name == touched)
        check("changed 节点带字段级 diff", nd.kind == "changed" and len(nd.fields) >= 1)

    # 4. 按文件复位 → 漂移归零
    baseline_mod.reset_file(cfg, slug, battle_fd[0].layer, battle_fd[0].rel, branch)
    rep3 = baseline_mod.detect_drift(cfg, slug, mounted=False)
    battle_left = [f for f in rep3.files if f.rel.endswith("Battle.json") and f.nodes]
    check("复位 Battle.json 后 → 该文件漂移归零", not battle_left)

finally:
    # 清理: 删掉本次创建的 maaowm/ (避免污染 MFABD2 仓库)
    if created_maaowm and maaowm_root.exists():
        shutil.rmtree(maaowm_root)
        print(f"  已清理 {maaowm_root}")
    elif maaowm_root.exists():
        # maaowm 原本就有 → 只删我们这次建的 baseline slug
        bd = cfg.baseline_dir(slug)
        if bd.exists():
            shutil.rmtree(bd)
            print(f"  已清理 {bd}")

print("\n结果:", "全部通过 ✓" if ok_all else "有失败 ✗")
sys.exit(0 if ok_all else 1)
