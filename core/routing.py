"""
core/routing.py — task → file 归属管理

挂载/卸载共用的"task 该写到哪个文件"决策。

挂载阶段产出 OriginIndex:
  来自 base 的 task   → 工作区按 base 同名相对路径写
  来自 mod 的 task    → 工作区按 mod 同名相对路径写 (覆盖 base 的同位文件)
  base 和 mod 都没有但工作区新增的 task → __mod_extras__.json (兜底)

卸载阶段读 OriginIndex:
  task 在 mod 的原归属位置 → 写回该位置 (复用挂载前的 mod 文件结构)
  base 和 mod 都没有但工作区新增 → 写到 __mod_extras__.json
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import tempfile
from typing import Dict, List, Optional, Set


EXTRAS_FILENAME = "__mod_extras__.json"


@dataclasses.dataclass
class OriginIndex:
    """task → 相对路径 (相对 mod 包根) 的归属索引。"""
    # task name -> mod 包内的相对路径 (POSIX 风格, 跨平台)
    mod_origin: Dict[str, str]
    # task name -> base 包内的相对路径 (POSIX 风格)
    base_origin: Dict[str, str]

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "OriginIndex":
        d = json.loads(text)
        return cls(mod_origin=d["mod_origin"], base_origin=d["base_origin"])


def build_origin_index(
    base_dir: pathlib.Path,
    mod_dir: pathlib.Path,
) -> OriginIndex:
    """扫描 base 和 mod 两份 pipeline, 建立 task → 文件归属索引。"""
    from . import oracle

    mod_origin: Dict[str, str] = {}
    base_origin: Dict[str, str] = {}

    for name, path in oracle.list_node_names_with_origin(base_dir).items():
        base_origin[name] = path.relative_to(base_dir).as_posix()

    for name, path in oracle.list_node_names_with_origin(mod_dir).items():
        mod_origin[name] = path.relative_to(mod_dir).as_posix()

    return OriginIndex(mod_origin=mod_origin, base_origin=base_origin)


def decide_target_file(
    task_name: str,
    index: OriginIndex,
) -> str:
    """决定一个 task 应该写到 mod 包的哪个相对路径。

    优先级:
      1. mod 原本就在的位置 (保留挂载前的组织结构)
      2. base 同位置 (mod 用同名文件覆盖)
      3. __mod_extras__.json (用户在工作区新建的 task)
    """
    if task_name in index.mod_origin:
        return index.mod_origin[task_name]
    if task_name in index.base_origin:
        return index.base_origin[task_name]
    return EXTRAS_FILENAME


def group_by_target_file(
    minimal_mod: Dict[str, dict],
    index: OriginIndex,
) -> Dict[str, Dict[str, dict]]:
    """把 minimal_mod 按目标文件分组, 返回 {relative_path: {task_name: task_def}}。"""
    grouped: Dict[str, Dict[str, dict]] = {}
    for task_name, task_def in minimal_mod.items():
        target = decide_target_file(task_name, index)
        grouped.setdefault(target, {})[task_name] = task_def
    return grouped


def write_mod_files(
    grouped: Dict[str, Dict[str, dict]],
    mod_dir: pathlib.Path,
) -> List[pathlib.Path]:
    """把分组后的 task 写入 mod 目录的对应文件。

    返回写入的文件路径列表 (供 TUI 显示)。
    会创建必要的子目录。
    不会删除 mod 目录中其他已存在的文件 (那是 inplace.py 的职责)。
    """
    written: List[pathlib.Path] = []
    for relative, tasks in grouped.items():
        target = mod_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        # 信任调用方传入的 task 顺序 — 上游应已通过 extras.reorder_pipeline_by_node_order
        # 按 base 原序重排 (V0.7.0+). dict 保序 (Python 3.7+).
        text = json.dumps(tasks, ensure_ascii=False, indent=4) + "\n"

        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        written.append(target)
    return written


def list_existing_mod_files(mod_dir: pathlib.Path) -> List[pathlib.Path]:
    """列出 mod 目录下所有 .json/.jsonc 文件 (用于挂载阶段备份/卸载阶段清理)。"""
    files: List[pathlib.Path] = []
    if not mod_dir.is_dir():
        return files
    for p in mod_dir.rglob('*'):
        if not p.is_file():
            continue
        if any(part.startswith('.') for part in p.relative_to(mod_dir).parts):
            continue
        if p.suffix.lower() in ('.json', '.jsonc'):
            files.append(p)
    return files


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    """构造合成 base/mod 目录, 验证 origin 索引和分组逻辑。"""
    print("routing 自检")
    print("─" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        base = root / "base"
        mod = root / "mod"
        base.mkdir()
        mod.mkdir()

        # base 结构
        (base / "main.json").write_text(
            json.dumps({"BaseTaskA": {}, "BaseTaskB": {}}), encoding="utf-8"
        )
        (base / "sub").mkdir()
        (base / "sub" / "battle.json").write_text(
            json.dumps({"Battle_Start": {}}), encoding="utf-8"
        )
        (base / "shop.json").write_text(
            json.dumps({"Shop_Open": {}, "Shop_Close": {}}), encoding="utf-8"
        )

        # mod 结构 (覆盖 base 的 shop.json + 新增 quickhunt.json)
        (mod / "shop.json").write_text(
            json.dumps({"Shop_Open": {}}), encoding="utf-8"
        )
        (mod / "quickhunt.json").write_text(
            json.dumps({"QuickHunt_Hub": {}}), encoding="utf-8"
        )

        # 模拟 oracle.list_node_names_with_origin
        # 这里直接 import 用真 oracle (它的 list_node_names_with_origin 不调 dll)
        sys.path.insert(0, str(root.parent.parent))   # 加到 path 里方便 import
        try:
            from core import oracle  # type: ignore
        except ImportError:
            # 不在 maaowm-v3 包结构里, 直接相对加载
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "core_oracle",
                pathlib.Path(__file__).parent / "oracle.py"
            )
            oracle = importlib.util.module_from_spec(spec)  # type: ignore
            spec.loader.exec_module(oracle)  # type: ignore

        # 临时 monkey-patch _self_test 用的 oracle
        import core.routing as routing_mod  # type: ignore
        routing_mod.oracle = oracle  # type: ignore

        index = build_origin_index(base, mod)

        print(f"  base origin ({len(index.base_origin)}):")
        for k, v in sorted(index.base_origin.items()):
            print(f"    {k:20s} -> {v}")
        print(f"  mod origin ({len(index.mod_origin)}):")
        for k, v in sorted(index.mod_origin.items()):
            print(f"    {k:20s} -> {v}")

        # 验证决策
        assert decide_target_file("Shop_Open", index) == "shop.json", "mod 已有 → mod 原路径"
        assert decide_target_file("BaseTaskA", index) == "main.json", "仅 base → base 同路径"
        assert decide_target_file("Battle_Start", index) == "sub/battle.json", "嵌套目录"
        assert decide_target_file("QuickHunt_Hub", index) == "quickhunt.json", "mod 独有"
        assert decide_target_file("BrandNew_Task", index) == EXTRAS_FILENAME, "工作区新建"
        print("  decide_target_file: ✓")

        # 验证分组
        minimal_mod = {
            "Shop_Open": {"timeout": 5000},          # mod 原位
            "BaseTaskA": {"post_delay": 999},        # base 同位
            "QuickHunt_Hub": {"next": ["X"]},        # mod 原位
            "BrandNew_Task": {"recognition": "DirectHit"},  # extras
        }
        grouped = group_by_target_file(minimal_mod, index)

        expected_grouping = {
            "shop.json": {"Shop_Open"},
            "main.json": {"BaseTaskA"},
            "quickhunt.json": {"QuickHunt_Hub"},
            EXTRAS_FILENAME: {"BrandNew_Task"},
        }
        actual_grouping = {k: set(v.keys()) for k, v in grouped.items()}
        ok_group = (actual_grouping == expected_grouping)
        print(f"  group_by_target_file: {'✓' if ok_group else '✗'}")
        if not ok_group:
            print(f"    期望: {expected_grouping}")
            print(f"    实际: {actual_grouping}")

        # 写文件
        out_dir = root / "out_mod"
        out_dir.mkdir()
        written = write_mod_files(grouped, out_dir)
        print(f"  写入文件 ({len(written)}):")
        for w in sorted(written):
            print(f"    {w.relative_to(out_dir)}")

        # 验证写入内容
        shop_data = json.loads((out_dir / "shop.json").read_text(encoding="utf-8"))
        ok_write = (shop_data == {"Shop_Open": {"timeout": 5000}})
        print(f"  写入内容正确: {'✓' if ok_write else '✗'}")

        return ok_group and ok_write


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
