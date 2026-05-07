"""
core/oracle.py — MaaFW 语义代理

把"加载 pipeline 目录 → 获取规范化 canonical"这件事封装成纯字典操作。
OWM 其余模块只跟 dict 打交道, 不感知 maa 包的存在。

DLL 路径策略 (优先级从高到低):
  1. config 里显式的 maa_pkg_dir (site-packages/maa 级别)
  2. import maa 后的 maa.__file__.parent (系统/venv 自动检测)
  3. 报错并提示

注: 即使 1 指定了路径, 也仍然需要 import maa (代码本身); 1 只是
指定 dll 来源。所以"显式路径"等于"用这个 maa 包目录加载"。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
from typing import Dict, List, Optional, Set


# ============================================================
# DLL / maa 包路径解析
# ============================================================

class OracleError(Exception):
    """oracle 层错误 (DLL 路径解析失败、加载失败等)。"""


def _resolve_maa_pkg_dir(explicit: Optional[pathlib.Path]) -> pathlib.Path:
    """定位 maa 包目录 (含 *.py 和 bin/*.dll 那一级)。"""
    if explicit:
        p = explicit.resolve()
        if not p.is_dir():
            raise OracleError(f"配置 maa_pkg_dir 不是目录: {p}")
        if not (p / "resource.py").exists():
            raise OracleError(
                f"maa_pkg_dir 中找不到 resource.py: {p}\n"
                f"  请指向 site-packages/maa 级别 (含 __init__.py、resource.py 等)"
            )
        if not (p / "bin").is_dir():
            raise OracleError(f"maa_pkg_dir 中找不到 bin/: {p}")
        return p

    # 自动检测: import maa 然后取 __file__.parent
    try:
        import maa  # type: ignore
    except ImportError as e:
        raise OracleError(
            f"未找到 maa 包: {e}\n"
            f"  请 pip install MaaFw, 或在 overlay_config.json 设置 maa_pkg_dir"
        ) from None

    pkg_dir = pathlib.Path(maa.__file__).parent
    if not (pkg_dir / "bin").is_dir():
        raise OracleError(
            f"自动检测到的 maa 包目录无 bin/: {pkg_dir}\n"
            f"  可能是源码版而非 pip 版, 请显式设置 maa_pkg_dir"
        )
    return pkg_dir


_initialized = False


def init(maa_pkg_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    """全局初始化 (整个进程只调一次)。返回最终使用的 maa 包目录。"""
    global _initialized
    pkg_dir = _resolve_maa_pkg_dir(maa_pkg_dir)

    if _initialized:
        return pkg_dir

    # 把指定的 maa 包路径放到 sys.path 最前 (如果用户用了显式 maa_pkg_dir 但
    # 当前 import maa 已经指到别处, 显式路径优先)
    parent = str(pkg_dir.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    from maa.library import Library  # type: ignore
    Library.open(pkg_dir / "bin")
    _initialized = True
    return pkg_dir


# ============================================================
# JSONC 解析 + 自己枚举 task 名
# ============================================================

_LINE_COMMENT_RE = re.compile(r'(?<!:)//[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _strip_jsonc(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub('', text)
    text = _LINE_COMMENT_RE.sub('', text)
    return text


def load_pipeline_json(path: pathlib.Path) -> Optional[dict]:
    """加载单个 pipeline JSON/JSONC, 失败返回 None。"""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as e:
        print(f"  ⚠ 读取失败 {path}: {e}", file=sys.stderr)
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_strip_jsonc(text))
    except json.JSONDecodeError as e:
        print(f"  ⚠ JSON 解析失败 {path}: {e}", file=sys.stderr)
        return None


def list_node_names(pipeline_dir: pathlib.Path) -> List[str]:
    """递归扫描目录, 收集所有 pipeline JSON 顶层 task 名。

    遵循 MaaFW 协议:
      - 以 . 开头的目录/文件忽略
      - 以 $ 开头的 root key 忽略 (用作 schema 引用)
      - 仅识别 .json / .jsonc 后缀
    """
    names: Set[str] = set()
    for path in pipeline_dir.rglob('*'):
        if not path.is_file():
            continue
        if any(part.startswith('.') for part in path.relative_to(pipeline_dir).parts):
            continue
        if path.suffix.lower() not in ('.json', '.jsonc'):
            continue
        data = load_pipeline_json(path)
        if not isinstance(data, dict):
            continue
        for key in data:
            if key and not key.startswith('$'):
                names.add(key)
    return sorted(names)


def list_node_names_with_origin(pipeline_dir: pathlib.Path) -> Dict[str, pathlib.Path]:
    """收集 task 名同时记录每个 task 来源文件 (用于 routing/origin 索引)。

    若某 task 在多个文件里都有定义, 取第一个遇到的 (后续会被 MaaFW 加载报错,
    但我们这里宽松处理)。
    """
    origin: Dict[str, pathlib.Path] = {}
    for path in sorted(pipeline_dir.rglob('*')):
        if not path.is_file():
            continue
        if any(part.startswith('.') for part in path.relative_to(pipeline_dir).parts):
            continue
        if path.suffix.lower() not in ('.json', '.jsonc'):
            continue
        data = load_pipeline_json(path)
        if not isinstance(data, dict):
            continue
        for key in data:
            if not key or key.startswith('$'):
                continue
            if key not in origin:
                origin[key] = path
    return origin


# ============================================================
# canonicalize: 加载 pipeline 目录 → {task_name: canonical 全字段 dict}
# ============================================================

def canonicalize(pipeline_dir: pathlib.Path) -> Dict[str, dict]:
    """加载一个 pipeline 目录, 返回每个 task 的 canonical V2 形态。

    canonical 来源 = MaaFW 内部 PipelineDumper 输出, 含全部默认字段, 格式始终为 V2。
    """
    if not _initialized:
        raise OracleError("oracle 未初始化, 请先调用 oracle.init()")

    from maa.resource import Resource  # type: ignore

    res = Resource()
    job = res.post_pipeline(str(pipeline_dir))
    job.wait()
    if not res.loaded:
        raise OracleError(
            f"MaaFW 加载 pipeline 失败: {pipeline_dir}\n"
            f"  常见原因: JSON 语法错误, 或 next/on_error 引用了不存在的 task。\n"
            f"  请查看上方日志确认具体 task。"
        )

    names = list_node_names(pipeline_dir)
    result: Dict[str, dict] = {}
    missing: List[str] = []
    for name in names:
        node = res.get_node_data(name)
        if node is None:
            missing.append(name)
        else:
            result[name] = node

    if missing:
        print(
            f"  ⚠ {len(missing)} 个 task 在 JSON 中存在但 get_node_data 返回 None: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}",
            file=sys.stderr,
        )

    # MaaFW 5.10.0b2 兼容: 修正 sub_recognition 形态 (parser/dumper 握手不对齐)
    # 详见 core/fixup.py
    from . import fixup as _fixup
    _fixup.fixup_sub_recognition(result)

    return result


def canonicalize_overlay(*pipeline_dirs: pathlib.Path) -> Dict[str, dict]:
    """按顺序加载多个 pipeline 目录, 返回合并后的 canonical 字典。

    用于 mount: oracle.canonicalize_overlay(base_dir, mod_dir)
    后加载的目录覆盖前面的 (与 MaaFW 自身的 post_pipeline 多次调用语义一致)。
    """
    if not _initialized:
        raise OracleError("oracle 未初始化, 请先调用 oracle.init()")
    if not pipeline_dirs:
        raise OracleError("canonicalize_overlay 需要至少一个目录")

    from maa.resource import Resource  # type: ignore

    res = Resource()
    for pd in pipeline_dirs:
        job = res.post_pipeline(str(pd))
        job.wait()
        if not res.loaded:
            raise OracleError(f"MaaFW 加载 pipeline 失败: {pd}")

    # 收集所有目录里出现过的 task 名
    all_names: Set[str] = set()
    for pd in pipeline_dirs:
        all_names.update(list_node_names(pd))

    result: Dict[str, dict] = {}
    for name in sorted(all_names):
        node = res.get_node_data(name)
        if node is not None:
            result[name] = node

    # MaaFW 5.10.0b2 兼容: 修正 sub_recognition 形态
    from . import fixup as _fixup
    _fixup.fixup_sub_recognition(result)

    return result


# ============================================================
# 自检 — 不调 dll, 只测 JSONC 解析和 task 名枚举
# ============================================================

def _self_test() -> bool:
    """容器可跑的自检。不依赖 maa 包, 仅测 JSON 解析与 task 枚举。"""
    print("oracle 自检 (不调 dll)")
    print("─" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        # 普通 JSON
        (root / "main.json").write_text(
            json.dumps({"TaskA": {}, "TaskB": {}}), encoding="utf-8"
        )
        # JSONC 含注释 + BOM
        (root / "extra.jsonc").write_text(
            '\ufeff// 头部注释\n'
            '{ "TaskC": {}, /* 块注释 */ "TaskD": {} }',
            encoding="utf-8",
        )
        # $ 开头要忽略
        (root / "schema.json").write_text(
            json.dumps({"$schema": "...", "TaskE": {}}), encoding="utf-8",
        )
        # . 开头目录要忽略
        (root / ".hidden").mkdir()
        (root / ".hidden" / "ghost.json").write_text(
            json.dumps({"GhostTask": {}}), encoding="utf-8",
        )
        # 嵌套目录
        (root / "sub").mkdir()
        (root / "sub" / "nested.json").write_text(
            json.dumps({"TaskF": {}}), encoding="utf-8",
        )

        names = list_node_names(root)
        expected = ["TaskA", "TaskB", "TaskC", "TaskD", "TaskE", "TaskF"]

        print(f"  扫描结果:  {names}")
        print(f"  期望:      {expected}")
        ok_names = (names == expected)
        print(f"  task 名枚举: {'✓' if ok_names else '✗'}")

        # origin 测试
        origin = list_node_names_with_origin(root)
        ok_origin = (
            origin.get("TaskA") == (root / "main.json")
            and origin.get("TaskC") == (root / "extra.jsonc")
            and origin.get("TaskF") == (root / "sub" / "nested.json")
        )
        print(f"  origin 索引: {'✓' if ok_origin else '✗'}")
        for k, v in sorted(origin.items()):
            print(f"    {k:10s} -> {v.relative_to(root)}")

    return ok_names and ok_origin


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
