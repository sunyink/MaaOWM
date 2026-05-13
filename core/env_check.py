"""
core/env_check.py — maa 环境预检 (挂载/卸载/检查时调用)

设计:
  - 不在启动时跑 (避免 maa 加载失败时连 TUI 都进不去)
  - 仅在 action_mount/unmount/validate 等"真要用 maa"的时机调
  - 返回 EnvError 而不抛异常 (TUI 自行决定怎么显示)
  - 失败时给环境信息 + 让用户自行排查, 不臆造解决方案
  - 若能识别到 maa_pkg_dir 在 venv 中, 附上精准命令做参考

参考: git 风格的友好报错 — 多给上下文, 让用户决策.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import sys
from typing import Optional, Tuple


# venv 目录名识别 — 大小写不敏感, 匹配常见命名
VENV_NAME_PATTERN = re.compile(
    r"^\.?(?:venv|env|virtualenv)$",
    re.IGNORECASE,
)


@dataclasses.dataclass
class EnvError:
    """maa 环境预检失败的信息载体。

    title: 一行简短标题
    sections: 多段诊断信息 (标题 → 内容字符串)
    formatted_message: 给 TUI 直接打印的完整字符串
    """
    title: str
    formatted_message: str


def find_venv_root(maa_pkg_path: pathlib.Path) -> Tuple[Optional[pathlib.Path], Optional[str]]:
    """从 maa_pkg_dir 沿父级目录向上, 找名字像 venv 的目录。

    返回 (venv_root, platform_label) 或 (None, None)。
    platform_label = "windows" / "unix" — 决定生成 Scripts\\python.exe 还是 bin/python.
    """
    if not isinstance(maa_pkg_path, pathlib.Path):
        return None, None
    for p in maa_pkg_path.parents:
        if VENV_NAME_PATTERN.match(p.name):
            # 验证: 至少存在 Windows 或 Unix 的 python 入口
            if (p / "Scripts" / "python.exe").exists():
                return p, "windows"
            if (p / "bin" / "python").exists():
                return p, "unix"
            if (p / "bin" / "python3").exists():
                return p, "unix"
    return None, None


def format_run_command(
    venv_root: pathlib.Path,
    platform_label: str,
    script_name: str = "overlay_tool.py",
) -> str:
    """生成"用 venv 的 Python 运行 OWM"的精准命令。"""
    if platform_label == "windows":
        py = venv_root / "Scripts" / "python.exe"
        return f'& "{py}" {script_name}'
    py = venv_root / "bin" / "python"
    return f'"{py}" {script_name}'


def format_activate_command(
    venv_root: pathlib.Path,
    platform_label: str,
) -> str:
    """生成"激活 venv"的命令。"""
    if platform_label == "windows":
        ps1 = venv_root / "Scripts" / "Activate.ps1"
        bat = venv_root / "Scripts" / "activate.bat"
        return f'& "{ps1}"   (PowerShell)\n    或 "{bat}"   (CMD)'
    activate = venv_root / "bin" / "activate"
    return f'source "{activate}"'


def precheck(cfg) -> Optional[EnvError]:
    """尝试 import maa, 失败时返回 EnvError 含友好诊断。成功返回 None。

    cfg: OverlayConfig 实例, 用于读 maa_pkg_dir.
    """
    # 步骤 1: try import maa
    # 若 cfg.maa_pkg_dir 显式指定, 临时加入 sys.path 让 import 走该路径
    inserted = False
    try:
        maa_pkg_dir = getattr(cfg, "maa_pkg_dir", None)
        if maa_pkg_dir is not None:
            site_packages = maa_pkg_dir.parent  # .../site-packages
            if site_packages.exists():
                sp = str(site_packages)
                if sp not in sys.path:
                    sys.path.insert(0, sp)
                    inserted = True

        try:
            import maa  # noqa: F401
            # 进一步: 确认 maa.library 也能加载 (它会触发 numpy/cv2 加载)
            from maa import library  # noqa: F401
            return None  # 成功!
        except Exception as e:
            return _build_env_error(cfg, exc=e)
    finally:
        if inserted and sys.path and sys.path[0] == sp:
            sys.path.pop(0)


def _build_env_error(cfg, exc: BaseException) -> EnvError:
    """构造 EnvError, 含环境信息 + venv 提示 (如适用)。"""
    lines = []
    lines.append("[red]✗ 无法加载 MaaFramework[/red]\n")

    # 环境信息
    lines.append("[bold]环境信息[/bold]")
    lines.append(f"  Python 解释器:  {sys.executable}")
    lines.append(f"  Python 版本:    {sys.version.split()[0]}")
    lines.append(f"  运行平台:       {sys.platform}")

    maa_pkg_dir = getattr(cfg, "maa_pkg_dir", None)
    if maa_pkg_dir:
        lines.append(f"  MaaFramework:   {maa_pkg_dir}")
    else:
        lines.append("  MaaFramework:   (未在配置中指定, 从 import maa 自动定位)")

    lines.append("")

    # 常见原因 (不臆造原因, 列已知常见原因让用户对照)
    lines.append("[bold]常见原因[/bold]")
    lines.append("  • 当前 Python 解释器与 MaaFramework 所在 Python 环境不匹配")
    lines.append("    (例: maa 装在 .venv 用 Python 3.10, 但当前是系统 Python 3.11)")
    lines.append("    导致 maa 的 C 扩展依赖 (numpy/cv2 等) 跨版本不兼容")
    lines.append("  • MaaFramework 所在环境的依赖损坏 (pip 重装 numpy/cv2 可修)")
    lines.append("  • maa_pkg_dir 配置路径错误或目录已不存在")
    lines.append("")

    # 如果识别到 venv, 给精准命令
    if maa_pkg_dir is not None:
        venv_root, platform = find_venv_root(maa_pkg_dir)
        if venv_root and platform:
            lines.append("[bold]检测到 MaaFramework 装在虚拟环境中[/bold]")
            lines.append(f"  虚拟环境位置: {venv_root}")
            lines.append("")
            lines.append("  请尝试用该环境的 Python 运行 OWM:")
            lines.append("")
            lines.append(f"    [cyan]{format_run_command(venv_root, platform)}[/cyan]")
            lines.append("")
            lines.append("  或先激活该环境再运行:")
            lines.append("")
            lines.append(f"    [cyan]{format_activate_command(venv_root, platform)}[/cyan]")
            lines.append("    [cyan]python overlay_tool.py[/cyan]")
            lines.append("")

    lines.append("[dim]在 TUI 中, [L] 查看日志 / [H] 查看说明 仍可用; 仅 [M]/[U]/[C] 需要 MaaFramework.[/dim]")

    return EnvError(
        title="无法加载 MaaFramework",
        formatted_message="\n".join(lines),
    )


# ============================================================
# 自检 — 合成数据测 venv 识别
# ============================================================

def _self_test() -> bool:
    print("env_check 自检")
    print("─" * 60)
    all_ok = True

    # ─── case 1: 找 .venv 路径 (Windows 假装) ───
    # 由于自检不能真造 Scripts/python.exe, 仅测正则匹配
    test_paths = [
        ("F:/Git BD2/MFABD2-main/.venv/Lib/site-packages/maa", ".venv"),
        ("/home/user/proj/venv/lib/python3.10/site-packages/maa", "venv"),
        ("/home/user/proj/env/lib/site-packages/maa", "env"),
        ("/home/user/proj/.env/lib/site-packages/maa", ".env"),
        ("/home/user/proj/virtualenv/lib/site-packages/maa", "virtualenv"),
        ("/home/user/proj/myenv/lib/site-packages/maa", None),  # 自定义名不匹配
        ("/usr/lib/python3/site-packages/maa", None),  # 系统装的不匹配
    ]
    for path_str, expected_venv_name in test_paths:
        p = pathlib.PurePosixPath(path_str)
        matched = None
        for parent in p.parents:
            if VENV_NAME_PATTERN.match(parent.name):
                matched = parent.name
                break
        ok = (matched == expected_venv_name)
        all_ok = all_ok and ok
        print(f"  {'✓' if ok else '✗'} 路径识别 {path_str!r}")
        print(f"      期望: {expected_venv_name!r}  实际: {matched!r}")

    # ─── case 2: format_run_command ───
    venv = pathlib.Path("F:/proj/.venv")
    cmd_win = format_run_command(venv, "windows")
    ok = '"' in cmd_win and "Scripts" in cmd_win and "python.exe" in cmd_win
    all_ok = all_ok and ok
    print(f"  {'✓' if ok else '✗'} Windows 命令格式: {cmd_win}")

    venv = pathlib.Path("/home/user/proj/.venv")
    cmd_unix = format_run_command(venv, "unix")
    ok = "bin/python" in cmd_unix
    all_ok = all_ok and ok
    print(f"  {'✓' if ok else '✗'} Unix 命令格式: {cmd_unix}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
