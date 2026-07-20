"""
core/config.py — 配置加载与校验

overlay_config.json 字段:
  target          : 必填, mod 包目录 (相对配置文件位置, 或绝对路径)
  base_layers     : 必填, base 包目录列表 (按顺序加载, 后者覆盖前者)
  maa_pkg_dir     : 选填, 显式 site-packages/maa 目录 (覆盖自动检测)
  maaowm_dir      : 选填, 显式 maaowm/ 目录 (覆盖自动定位的项目根/maaowm)

路径规则:
  - 支持绝对路径和相对路径 (相对配置文件位置)
  - 支持 ../ 向上导航

owm 文件布局 (统一到被管理项目根 maaowm/):
  maaowm/.state/    本地状态 (快照/备份/日志), 自动 gitignore
  maaowm/baseline/  base' 基线 (进 git 共享), 见 core/baseline.py
  项目根定位: 显式 maaowm_dir > git toplevel(target) > target 向上找 assets
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import tempfile
from typing import List, Optional


@dataclasses.dataclass
class OverlayConfig:
    config_path: pathlib.Path                    # 配置文件本身的路径
    config_root: pathlib.Path                    # 配置文件所在目录
    target_path: pathlib.Path                    # mod 包目录 (绝对)
    base_layer_paths_resolved: List[pathlib.Path]  # base 目录列表 (绝对)
    maa_pkg_dir: Optional[pathlib.Path]          # 显式 maa 包目录 (绝对)
    output_format: str = "v2"                    # "v2" (默认) 或 "v1"
    compact_node_refs: bool = True               # next/on_error 紧凑写法 (默认 True)
    maaowm_dir_explicit: Optional[pathlib.Path] = None  # 配置显式 maaowm_dir (绝对)
    _maaowm_root_cache: Optional[pathlib.Path] = dataclasses.field(
        default=None, init=False, repr=False, compare=False,
    )

    def base_layer_paths(self) -> List[pathlib.Path]:
        return self.base_layer_paths_resolved

    def _resolve_project_root(self) -> pathlib.Path:
        """定位被管理项目根: git toplevel(target) > target 向上找含 assets 的目录 >
        target.parent (兜底)。"""
        # 1. git toplevel (baseline 已依赖 git, 这是最稳的)
        try:
            from . import gitinfo
            top = gitinfo.git_toplevel(self.target_path)
            if top is not None:
                return top
        except Exception:
            pass
        # 2. 向上找名为 assets 的祖先, 取其父
        for anc in self.target_path.parents:
            if anc.name == "assets":
                return anc.parent
        # 3. 兜底
        return self.target_path.parent

    @property
    def maaowm_root(self) -> pathlib.Path:
        """被管理项目根下的统一 maaowm/ 目录 (显式 maaowm_dir 优先)。"""
        if self.maaowm_dir_explicit is not None:
            return self.maaowm_dir_explicit
        if self._maaowm_root_cache is None:
            self._maaowm_root_cache = self._resolve_project_root() / "maaowm"
        return self._maaowm_root_cache

    @property
    def owm_dir(self) -> pathlib.Path:
        """本地状态目录 maaowm/.state/, 存快照、备份、日志 (整目录 gitignore)。"""
        return self.maaowm_root / ".state"

    @property
    def baseline_root(self) -> pathlib.Path:
        """base' 基线根 maaowm/baseline/ (进 git 共享)。"""
        return self.maaowm_root / "baseline"

    def baseline_dir(self, slug: str) -> pathlib.Path:
        """某分支 slug 的 baseline 目录 maaowm/baseline/<slug>/。"""
        return self.baseline_root / slug

    @property
    def workspace_dir(self) -> pathlib.Path:
        """工作区目录就是目标 mod 包目录本身 (inplace 模式)。"""
        return self.target_path

    @property
    def pipeline_subdir(self) -> str:
        """Pipeline 子目录名 (MaaFW 约定)。"""
        return "pipeline"

    def workspace_pipeline_dir(self) -> pathlib.Path:
        """工作区中 pipeline 子目录的路径。"""
        return self.workspace_dir / self.pipeline_subdir

    def base_pipeline_dirs(self) -> List[pathlib.Path]:
        """所有 base 层中 pipeline 子目录的路径列表。"""
        return [b / self.pipeline_subdir for b in self.base_layer_paths_resolved]

    def validate(self) -> List[str]:
        """返回所有校验错误的字符串列表 (空表示 OK)。"""
        errs: List[str] = []
        if not self.target_path.is_dir():
            errs.append(f"target 目录不存在: {self.target_path}")
        for b in self.base_layer_paths_resolved:
            if not b.is_dir():
                errs.append(f"base layer 目录不存在: {b}")
        if not self.workspace_pipeline_dir().exists():
            errs.append(
                f"target 下没有 pipeline/ 子目录: {self.workspace_pipeline_dir()}\n"
                f"  MaaFW 约定 pipeline JSON 应放在 <bundle>/pipeline/ 下"
            )
        for bp in self.base_pipeline_dirs():
            if not bp.exists():
                errs.append(f"base 层下没有 pipeline/ 子目录: {bp}")
        return errs


class ConfigError(Exception):
    pass


def _resolve_path(raw: str, config_root: pathlib.Path) -> pathlib.Path:
    p = pathlib.Path(raw).expanduser()
    if not p.is_absolute():
        p = (config_root / p).resolve()
    else:
        p = p.resolve()
    return p


def load_config(config_path: pathlib.Path) -> OverlayConfig:
    """读 overlay_config.json, 解析为 OverlayConfig。不做存在性校验, 那是 validate() 的事。"""
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")

    text = config_path.read_text(encoding="utf-8-sig")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件不是合法 JSON: {e}") from None

    if not isinstance(raw, dict):
        raise ConfigError("配置文件根必须是 object")

    if "target" not in raw or not isinstance(raw["target"], str):
        raise ConfigError("配置缺少 target 字段 (字符串)")
    if "base_layers" not in raw or not isinstance(raw["base_layers"], list):
        raise ConfigError("配置缺少 base_layers 字段 (字符串数组)")
    if not all(isinstance(x, str) for x in raw["base_layers"]):
        raise ConfigError("base_layers 必须全是字符串")

    config_root = config_path.parent
    target_path = _resolve_path(raw["target"], config_root)
    base_layer_paths = [_resolve_path(b, config_root) for b in raw["base_layers"]]

    maa_pkg_dir: Optional[pathlib.Path] = None
    if "maa_pkg_dir" in raw and raw["maa_pkg_dir"]:
        if not isinstance(raw["maa_pkg_dir"], str):
            raise ConfigError("maa_pkg_dir 必须是字符串")
        maa_pkg_dir = _resolve_path(raw["maa_pkg_dir"], config_root)

    output_format = "v2"
    if "output_format" in raw:
        of = raw["output_format"]
        if of not in ("v1", "v2"):
            raise ConfigError(
                f"output_format 必须是 'v1' 或 'v2', 实际: {of!r}"
            )
        output_format = of

    compact_node_refs = True
    if "compact_node_refs" in raw:
        cr = raw["compact_node_refs"]
        if not isinstance(cr, bool):
            raise ConfigError(
                f"compact_node_refs 必须是 true/false, 实际: {cr!r}"
            )
        compact_node_refs = cr

    maaowm_dir_explicit: Optional[pathlib.Path] = None
    if "maaowm_dir" in raw and raw["maaowm_dir"]:
        if not isinstance(raw["maaowm_dir"], str):
            raise ConfigError("maaowm_dir 必须是字符串")
        maaowm_dir_explicit = _resolve_path(raw["maaowm_dir"], config_root)

    return OverlayConfig(
        config_path=config_path,
        config_root=config_root,
        target_path=target_path,
        base_layer_paths_resolved=base_layer_paths,
        maa_pkg_dir=maa_pkg_dir,
        output_format=output_format,
        compact_node_refs=compact_node_refs,
        maaowm_dir_explicit=maaowm_dir_explicit,
    )


def ensure_maaowm_scaffold(cfg: OverlayConfig) -> None:
    """首次建 maaowm/ 时落地脚手架: 建 .state/ 并自动写 maaowm/.gitignore (排 .state/)。

    幂等: 已存在则只补缺失项, 不覆盖。.gitignore 让本地状态默认不进 git,
    无需人手改根 .gitignore。
    """
    root = cfg.maaowm_root
    root.mkdir(parents=True, exist_ok=True)
    cfg.owm_dir.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# MaaOWM 本地状态 (快照/备份/日志), 不进版本库\n.state/\n",
            encoding="utf-8",
        )


def set_output_format_in_config(
    config_path: pathlib.Path,
    new_format: str,
) -> None:
    """原地修改配置文件的 output_format 字段, 保留其他内容。

    用于 TUI 切换格式时持久化到 overlay_config.json。
    """
    if new_format not in ("v1", "v2"):
        raise ConfigError(f"new_format 必须是 'v1' 或 'v2', 实际: {new_format!r}")
    text = config_path.read_text(encoding="utf-8-sig")
    raw = json.loads(text)
    raw["output_format"] = new_format
    config_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )


def set_compact_node_refs_in_config(
    config_path: pathlib.Path,
    enabled: bool,
) -> None:
    """原地修改 compact_node_refs 字段, 保留其他内容。"""
    if not isinstance(enabled, bool):
        raise ConfigError(f"enabled 必须是 bool, 实际: {enabled!r}")
    text = config_path.read_text(encoding="utf-8-sig")
    raw = json.loads(text)
    raw["compact_node_refs"] = enabled
    config_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )


SAMPLE_CONFIG = {
    "target": "assets/resource/PC",
    "base_layers": ["assets/resource/base"],
    "maa_pkg_dir": None,
    "output_format": "v2",
    "//compact_node_refs": "next/on_error 等节点引用是否使用紧凑字符串写法 ('[JumpBack]Foo' 而非 {name:'Foo', jump_back:true}); 默认 true, 大多数项目期望此风格",
    "compact_node_refs": True,
}


def write_sample_config(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(SAMPLE_CONFIG, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    print("config 自检")
    print("─" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        # 建假目录结构
        (root / "assets" / "resource" / "base" / "pipeline").mkdir(parents=True)
        (root / "assets" / "resource" / "PC" / "pipeline").mkdir(parents=True)

        cfg_path = root / "overlay_config.json"
        cfg_path.write_text(json.dumps({
            "target": "assets/resource/PC",
            "base_layers": ["assets/resource/base"],
            "maa_pkg_dir": None,
        }), encoding="utf-8")

        cfg = load_config(cfg_path)
        errs = cfg.validate()
        ok = not errs
        print(f"  target_path:           {cfg.target_path}")
        print(f"  base_layer_paths:      {cfg.base_layer_paths()}")
        print(f"  workspace_pipeline:    {cfg.workspace_pipeline_dir()}")
        print(f"  owm_dir:               {cfg.owm_dir}")
        print(f"  validate:              {'✓' if ok else '✗'}")
        if errs:
            for e in errs:
                print(f"    ✗ {e}")

        # 测试缺字段
        bad_path = root / "bad.json"
        bad_path.write_text('{"target": "x"}', encoding="utf-8")
        try:
            load_config(bad_path)
            print("  缺字段检测:           ✗ (本该报错)")
            return False
        except ConfigError:
            print("  缺字段检测:           ✓")

        # 测试不存在的路径
        bad2_path = root / "bad2.json"
        bad2_path.write_text(json.dumps({
            "target": "nonexistent",
            "base_layers": ["nonexistent_base"],
        }), encoding="utf-8")
        cfg2 = load_config(bad2_path)
        errs2 = cfg2.validate()
        print(f"  不存在路径检测:       {'✓' if errs2 else '✗'}")

        return ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
