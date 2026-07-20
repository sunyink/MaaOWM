"""
core/baseline.py — base 基线漂移检测 (base')

完整设计见 DESIGN-base-baseline.md。核心:
  - base' = base/pipeline 源文件的原档副本 (每个 base_layer 一份), 进 git 共享。
  - 只有显式复位才变 (手动 ack), 绝不自动刷新。
  - 按当前 git 分支隔离: maaowm/baseline/<slug>/<layer-name>/pipeline/...
  - 检测 = 用当前 maa 双 canonicalize(base' 原档, 当前 base) 再 diff, 消版本假阳性。
  - 报告纯只读; 本模块只在显式复位/adopt/clean 时动 baseline 自身目录。

布局:
  maaowm/baseline/<slug>/
    ├── _anchor.json          复位元数据 (branch/commit/time/user); 不参与 diff
    └── <layer-name>/pipeline/  该 base 层源文件原档
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import config as config_mod
    from . import gitinfo
except ImportError:
    # 直接运行 python core/baseline.py 时
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    import config as config_mod  # type: ignore
    import gitinfo  # type: ignore


ANCHOR_FILENAME = "_anchor.json"
PIPELINE_SUBDIR = "pipeline"
FLAT_SLUG = "_single"          # 无 git / detached 时退化用的单一基线名

# 路径不存在 / 字段缺失的哨兵
class _Missing:
    _inst = None
    def __repr__(self): return "<缺失>"
MISSING = _Missing()


# ============================================================
# 纯函数: 字段级 leaf diff / 路径取值 / 漂移分类 (不依赖 maa, 可单测)
# ============================================================

def get_path(d: Any, path: str) -> Any:
    """按 'a.b.c' 取嵌套值; 任一层缺失返回 MISSING。"""
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def leaf_diff(a: Any, b: Any, prefix: str = "") -> List[Tuple[str, Any, Any]]:
    """递归比较两值, 返回 [(path, a_val, b_val), ...] (仅不等的叶子)。

    dict 递归; list/标量按整体叶子比较。缺失侧用 MISSING。
    """
    out: List[Tuple[str, Any, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{prefix}.{k}" if prefix else k
            if k not in a:
                out.append((p, MISSING, b[k]))
            elif k not in b:
                out.append((p, a[k], MISSING))
            elif a[k] != b[k]:
                out.extend(leaf_diff(a[k], b[k], p))
    else:
        if a != b:
            out.append((prefix, a, b))
    return out


def classify_field(baseline_val: Any, now_val: Any, mod_val: Any) -> str:
    """对一个漂移字段定级。

    mod_val = 该路径在合并态(base⊕mod)的现值; now_val = 当前 base 值。
      mod 覆盖该字段 (mod_val != now_val):
        mod_val == baseline_val (= 旧 base 值) → "stale" (疑似过时残留)
        否则                                   → "review" (mod 改过, base 也改过)
      mod 未覆盖 (mod_val == now_val)          → "info" (自动跟随)
    """
    if mod_val is MISSING or mod_val == now_val:
        return "info"
    if mod_val == baseline_val:
        return "stale"
    return "review"


# ============================================================
# 报告数据结构
# ============================================================

@dataclasses.dataclass
class FieldDrift:
    field_path: str
    baseline_val: Any        # base' 旧值 (MISSING = base' 无此字段)
    now_val: Any             # 当前 base 新值 (MISSING = base 已删此字段)
    mod_val: Any             # mod override 现值 (MISSING = 工作区无)
    tag: str                 # review / stale / info


@dataclasses.dataclass
class NodeDrift:
    name: str
    kind: str                # changed / added / removed
    tag: str                 # 节点汇总: review / stale / info / dangling
    fields: List[FieldDrift] = dataclasses.field(default_factory=list)
    note: str = ""


@dataclasses.dataclass
class FileDrift:
    rel: str                 # 相对 pipeline 的路径 (posix)
    layer: str               # 所属 base 层名
    file_kind: str           # modified / added / removed
    nodes: List[NodeDrift] = dataclasses.field(default_factory=list)

    def review_count(self) -> int:
        return sum(1 for n in self.nodes if n.tag in ("review", "stale", "dangling"))


@dataclasses.dataclass
class DriftReport:
    files: List[FileDrift]
    slug: str
    flat: bool                       # 是否退化为单一基线
    note: str = ""                   # 退化原因等

    def has_drift(self) -> bool:
        return any(f.nodes for f in self.files)

    def total_review(self) -> int:
        return sum(f.review_count() for f in self.files)


# ============================================================
# 层名 / 目录解析
# ============================================================

def layer_names(cfg: config_mod.OverlayConfig) -> List[str]:
    """各 base 层的目录名 (重名追加序号), 与 cfg.base_layer_paths_resolved 对齐。"""
    names: List[str] = []
    seen: Dict[str, int] = {}
    for p in cfg.base_layer_paths_resolved:
        base = p.name or "base"
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
        names.append(base)
    return names


def snapshot_layer_dirs(cfg: config_mod.OverlayConfig, slug: str) -> List[pathlib.Path]:
    """baseline 下各层的 pipeline 目录 (与 base 层一一对应)。"""
    bdir = cfg.baseline_dir(slug)
    return [bdir / ln / PIPELINE_SUBDIR for ln in layer_names(cfg)]


def has_baseline(cfg: config_mod.OverlayConfig, slug: str) -> bool:
    """是否已建立该 slug 的基线 (以 _anchor.json 为准)。"""
    return (cfg.baseline_dir(slug) / ANCHOR_FILENAME).exists()


# ============================================================
# anchor 读写
# ============================================================

def write_anchor(
    cfg: config_mod.OverlayConfig, slug: str, original_branch: Optional[str],
) -> None:
    """写/更新 _anchor.json (复位元数据)。仅元数据, 不参与 diff。"""
    cwd = cfg.target_path
    anchor = {
        "branch": original_branch,
        "slug": slug,
        "commit": gitinfo.head_commit(cwd),
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "user": gitinfo.git_user(cwd),
        "layers": layer_names(cfg),
    }
    bdir = cfg.baseline_dir(slug)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / ANCHOR_FILENAME).write_text(
        json.dumps(anchor, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def read_anchor(cfg: config_mod.OverlayConfig, slug: str) -> Optional[dict]:
    p = cfg.baseline_dir(slug) / ANCHOR_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ============================================================
# 复位 (源文件原档拷贝)
# ============================================================

def _copy_pipeline(src: pathlib.Path, dst: pathlib.Path) -> int:
    """拷 src 下 pipeline JSON 原档到 dst (沿用 inplace._copy_pipeline_only 规则)。"""
    if not src.is_dir():
        return 0
    count = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(src).parts):
            continue
        if p.suffix.lower() not in (".json", ".jsonc"):
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        count += 1
    return count


def reset_all(
    cfg: config_mod.OverlayConfig, slug: str, original_branch: Optional[str],
) -> int:
    """建立/全量复位基线: 清空该 slug 各层目录, 重拷所有 base 层源文件 + 写 anchor。"""
    config_mod.ensure_maaowm_scaffold(cfg)
    names = layer_names(cfg)
    total = 0
    for ln, layer in zip(names, cfg.base_pipeline_dirs()):
        dst = cfg.baseline_dir(slug) / ln / PIPELINE_SUBDIR
        if dst.exists():
            shutil.rmtree(dst)
        total += _copy_pipeline(layer, dst)
    write_anchor(cfg, slug, original_branch)
    return total


def reset_file(
    cfg: config_mod.OverlayConfig, slug: str, layer_name: str, rel: str,
    original_branch: Optional[str],
) -> bool:
    """按文件复位: 把当前 base 某层某文件原档覆盖进 baseline, 并刷新 anchor。

    若当前 base 已无此文件 (文件级删) → 删除 baseline 中对应文件。
    """
    names = layer_names(cfg)
    if layer_name not in names:
        return False
    idx = names.index(layer_name)
    src_file = cfg.base_pipeline_dirs()[idx] / rel
    dst_file = cfg.baseline_dir(slug) / layer_name / PIPELINE_SUBDIR / rel
    if src_file.is_file():
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
    elif dst_file.is_file():
        dst_file.unlink()          # base 已删该文件 → 基线也移除
    else:
        return False
    write_anchor(cfg, slug, original_branch)
    return True


# ============================================================
# 分支生命周期: fork-adopt / orphan 清理 (§3.3)
# ============================================================

def list_baseline_slugs(cfg: config_mod.OverlayConfig) -> List[str]:
    """baseline_root 下已建立的 slug 列表 (有 _anchor.json 的目录)。"""
    root = cfg.baseline_root
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / ANCHOR_FILENAME).exists()
    )


def find_adoptable(cfg: config_mod.OverlayConfig, current_slug: str) -> List[str]:
    """当前 slug 无基线时, 可供 fork-adopt 改名的其他 slug。"""
    if has_baseline(cfg, current_slug):
        return []
    return [s for s in list_baseline_slugs(cfg) if s != current_slug]


def adopt(cfg: config_mod.OverlayConfig, from_slug: str, to_slug: str,
          original_branch: Optional[str]) -> bool:
    """fork-adopt: 把 from_slug 基线目录改名为 to_slug (不重建, 不丢有效基准)。"""
    src = cfg.baseline_dir(from_slug)
    dst = cfg.baseline_dir(to_slug)
    if not src.is_dir() or dst.exists():
        return False
    src.rename(dst)
    write_anchor(cfg, to_slug, original_branch)   # 刷新 anchor 为新分支
    return True


def find_orphans(cfg: config_mod.OverlayConfig, current_slug: str) -> List[str]:
    """孤儿基线: slug 对应的分支在 git 已不存在 (且非当前)。无 git 返回空 (不误删)。"""
    branches = gitinfo.all_branches(cfg.target_path)
    if not branches:
        return []                  # 无法判定存活 → 一律不清理
    orphans: List[str] = []
    for slug in list_baseline_slugs(cfg):
        if slug == current_slug or slug == FLAT_SLUG:
            continue
        anchor = read_anchor(cfg, slug)
        branch = (anchor or {}).get("branch") or slug
        # 分支名或其 slug 都不在现存分支里 → 孤儿
        if branch not in branches and gitinfo.slugify_branch(branch) not in {
            gitinfo.slugify_branch(b) for b in branches
        }:
            orphans.append(slug)
    return orphans


def remove_baseline(cfg: config_mod.OverlayConfig, slug: str) -> bool:
    """删除某 slug 的整个基线目录 (self-clean, 调用方须先确认)。"""
    d = cfg.baseline_dir(slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ============================================================
# 检测 (依赖 oracle, 需先 oracle.init)
# ============================================================

def _origin_map(
    layer_dirs: List[pathlib.Path], names: List[str],
) -> Dict[str, Tuple[str, str]]:
    """{node: (layer_name, rel_posix)} —— 每个 node 取首个出现的层/文件。"""
    from . import oracle
    out: Dict[str, Tuple[str, str]] = {}
    for ln, d in zip(names, layer_dirs):
        if not d.is_dir():
            continue
        for node, path in oracle.list_node_names_with_origin(d).items():
            if node not in out:
                out[node] = (ln, path.relative_to(d).as_posix())
    return out


def _note_for(node: NodeDrift) -> str:
    if node.kind == "removed":
        if node.tag == "dangling":
            return "base 已删除此节点, 但 mod 仍定义/覆盖 → 确认 pc 是否还需要; 不需要则在工作区删除该 mod 节点"
        return "base 已删除此节点 (mod 未涉及), 仅告知"
    if node.kind == "added":
        return "base 新增此节点, 挂载后工作区自动可见, 仅告知"
    # changed
    if node.tag == "stale":
        return "base 已改此字段, 而 mod 仍固定为旧 base 值 → 疑似过时残留, 考虑跟随 (回工作区改 mod / 删该 override)"
    if node.tag == "review":
        return "base 与 mod 都改过此字段 → 请确认 pc 是否需要跟随 base 的新值"
    return "base 改了此字段, mod 未覆盖 → 挂载后已自动跟随, 仅告知"


def effective_slug(
    cfg: config_mod.OverlayConfig,
) -> Tuple[str, Optional[str], str]:
    """当前应使用的 baseline slug。无 git/detached 时退化为 FLAT_SLUG。

    返回 (slug, original_branch, degrade_reason)。reason 非空表示发生了退化。
    """
    slug, branch, reason = gitinfo.resolve_branch_slug(cfg.target_path)
    if slug is None:
        return FLAT_SLUG, None, reason
    return slug, branch, ""


def detect_drift(
    cfg: config_mod.OverlayConfig,
    slug: str,
    *,
    mounted: bool = False,
    cano_now: Optional[Dict[str, dict]] = None,
    cano_merged: Optional[Dict[str, dict]] = None,
) -> DriftReport:
    """对比 base'(slug) 与当前 base, 产出漂移报告 (只读, 不动任何文件)。

    cano_now / cano_merged 可由调用方注入 (如 mount 已算好), 省一次 canonicalize。
    """
    from . import oracle

    names = layer_names(cfg)
    base_layer_dirs = list(cfg.base_pipeline_dirs())
    bl_layer_dirs = snapshot_layer_dirs(cfg, slug)

    # base' 原档双 canonicalize (同一当前 maa, 消版本假阳性)
    cano_baseline = oracle.canonicalize_overlay(*bl_layer_dirs)

    # 当前 base canonical (可注入)
    if cano_now is None:
        cano_now = oracle.canonicalize_overlay(*base_layer_dirs)

    # 合并态 (base⊕mod), 用于判定 mod 是否 override (可注入)
    if cano_merged is None:
        if mounted:
            cano_merged = oracle.canonicalize(cfg.workspace_pipeline_dir())
        else:
            mod_dir = cfg.workspace_pipeline_dir()
            has_mod = any(
                p.is_file() and p.suffix.lower() in (".json", ".jsonc")
                and not any(part.startswith(".") for part in p.relative_to(mod_dir).parts)
                for p in mod_dir.rglob("*")
            ) if mod_dir.is_dir() else False
            cano_merged = (
                oracle.canonicalize_overlay(*base_layer_dirs, mod_dir)
                if has_mod else cano_now
            )

    now_origin = _origin_map(base_layer_dirs, names)
    bl_origin = _origin_map(bl_layer_dirs, names)

    # 节点级 diff → NodeDrift, 暂存按 (layer, rel) 分组
    node_drifts: List[Tuple[str, str, NodeDrift]] = []   # (layer, rel, drift)

    all_nodes = set(cano_baseline) | set(cano_now)
    for name in sorted(all_nodes):
        in_bl = name in cano_baseline
        in_now = name in cano_now
        if in_bl and in_now:
            leaves = leaf_diff(cano_baseline[name], cano_now[name])
            if not leaves:
                continue
            fields: List[FieldDrift] = []
            for path, b_val, n_val in leaves:
                m_val = get_path(cano_merged.get(name, {}), path)
                tag = classify_field(b_val, n_val, m_val)
                fields.append(FieldDrift(path, b_val, n_val, m_val, tag))
            # 节点汇总 tag: stale > review > info
            if any(f.tag == "stale" for f in fields):
                ntag = "stale"
            elif any(f.tag == "review" for f in fields):
                ntag = "review"
            else:
                ntag = "info"
            nd = NodeDrift(name, "changed", ntag, fields)
            layer, rel = now_origin.get(name, ("?", "?"))
        elif in_now:   # added
            nd = NodeDrift(name, "added", "info")
            layer, rel = now_origin.get(name, ("?", "?"))
        else:          # removed (仅 baseline)
            dangling = name in cano_merged   # mod 让它存活
            nd = NodeDrift(name, "removed", "dangling" if dangling else "info")
            layer, rel = bl_origin.get(name, ("?", "?"))
        nd.note = _note_for(nd)
        node_drifts.append((layer, rel, nd))

    # 文件级: 源文件相对路径集合 (按层)
    def _file_set(layer_dirs):
        s = set()
        for ln, d in zip(names, layer_dirs):
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".json", ".jsonc") and not any(
                    part.startswith(".") for part in p.relative_to(d).parts
                ):
                    s.add((ln, p.relative_to(d).as_posix()))
        return s

    now_files = _file_set(base_layer_dirs)
    bl_files = _file_set(bl_layer_dirs)

    # 组装 FileDrift
    files_map: Dict[Tuple[str, str], FileDrift] = {}

    def _file_kind(key):
        if key in now_files and key not in bl_files:
            return "added"
        if key in bl_files and key not in now_files:
            return "removed"
        return "modified"

    for layer, rel, nd in node_drifts:
        key = (layer, rel)
        if key not in files_map:
            files_map[key] = FileDrift(rel=rel, layer=layer, file_kind=_file_kind(key))
        files_map[key].nodes.append(nd)

    # 纯文件级增删 (无节点漂移也列出, 如空文件或全 identical 节点的新文件)
    for key in (now_files ^ bl_files):
        if key not in files_map:
            files_map[key] = FileDrift(rel=key[1], layer=key[0], file_kind=_file_kind(key))

    files = sorted(files_map.values(), key=lambda f: (f.layer, f.rel))
    return DriftReport(files=files, slug=slug, flat=(slug == FLAT_SLUG))


# ============================================================
# 自检 (纯函数部分; detect_drift 走端到端)
# ============================================================

def _self_test() -> bool:
    print("baseline 自检 (纯函数)")
    print("─" * 60)
    all_ok = True

    # get_path
    d = {"recognition": {"param": {"threshold": 0.3}}}
    ok = (get_path(d, "recognition.param.threshold") == 0.3
          and get_path(d, "recognition.param.nope") is MISSING
          and get_path(d, "x.y") is MISSING)
    all_ok &= ok
    print(f"  {'✓' if ok else '✗'} get_path")

    # leaf_diff
    a = {"recognition": {"type": "OCR", "param": {"threshold": 0.5, "roi": [0, 0, 1, 1]}}}
    b = {"recognition": {"type": "OCR", "param": {"threshold": 0.3, "roi": [0, 0, 1, 1]}}}
    leaves = leaf_diff(a, b)
    ok = (leaves == [("recognition.param.threshold", 0.5, 0.3)])
    all_ok &= ok
    print(f"  {'✓' if ok else '✗'} leaf_diff 单字段: {leaves}")

    # classify_field
    checks = [
        # baseline, now, mod, expect
        (0.5, 0.3, 0.5, "stale"),    # mod 仍是旧 base 值
        (0.5, 0.3, 0.9, "review"),   # mod 改成别的值, base 也改了
        (0.5, 0.3, 0.3, "info"),     # mod 没覆盖 (= 当前 base)
        (0.5, 0.3, MISSING, "info"), # mod 无此字段
    ]
    for bl, now, mod, exp in checks:
        got = classify_field(bl, now, mod)
        ok = (got == exp)
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} classify({bl},{now},{mod}) = {got} (期望 {exp})")

    # reset / adopt / orphan / has_baseline 用临时目录 (不依赖 maa)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "assets/resource/base/pipeline").mkdir(parents=True)
        (root / "assets/resource/PC/pipeline").mkdir(parents=True)
        (root / "assets/resource/base/pipeline/Battle.json").write_text(
            '{"TaskA": {}}', encoding="utf-8")
        cfg_path = root / "overlay_config.json"
        cfg_path.write_text(json.dumps({
            "target": "assets/resource/PC",
            "base_layers": ["assets/resource/base"],
        }), encoding="utf-8")
        cfg = config_mod.load_config(cfg_path)

        ok = (not has_baseline(cfg, "main"))
        n = reset_all(cfg, "main", "main")
        ok &= has_baseline(cfg, "main")
        ok &= (n == 1)
        ok &= (cfg.baseline_dir("main") / "base" / "pipeline" / "Battle.json").exists()
        ok &= (cfg.maaowm_root / ".gitignore").exists()
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} reset_all 建立基线 + .gitignore (拷 {n} 文件)")

        # adopt: main → feature_x
        ad = find_adoptable(cfg, "feature_x")
        ok = (ad == ["main"])
        ok &= adopt(cfg, "main", "feature_x", "feature/x")
        ok &= has_baseline(cfg, "feature_x")
        ok &= (not has_baseline(cfg, "main"))
        anchor = read_anchor(cfg, "feature_x")
        ok &= (anchor is not None and anchor.get("branch") == "feature/x")
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} fork-adopt rename main→feature_x")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
