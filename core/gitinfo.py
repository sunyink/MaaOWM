"""
core/gitinfo.py — git 信息封装 (全部容错)

baseline (base') 按当前 git 分支隔离, 需要拿分支名、仓库根、分支列表、HEAD
commit 等。本模块把这些 git 调用收口, 并保证 **全部容错**:

  git 缺失 / 不在仓库 / detached HEAD / 任何子进程错误 → 一律返回 None / 空集,
  绝不抛异常。调用方据此决定退化为 flat baseline (见 DESIGN-base-baseline.md §3.3)。

设计: baseline 是"本分支本地护栏", git 是它的天然依赖; 但 owm 主流程 (mount/
unmount) 不该因为 git 缺失就崩, 所以这里只提供"尽力而为"的信息。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile
from typing import List, Optional, Set, Tuple


# Windows 文件名非法字符 + 路径分隔符
_ILLEGAL = re.compile(r'[/\\:*?"<>|]+')


def _dir_of(path: pathlib.Path) -> pathlib.Path:
    """git -C 需要目录; 传进来的若是文件则取其父目录。"""
    p = pathlib.Path(path)
    return p if p.is_dir() else p.parent


def _run_git(args: List[str], cwd: pathlib.Path) -> Optional[str]:
    """跑一条 git 命令, 成功返回 stdout(strip), 任何失败返回 None。"""
    try:
        cp = subprocess.run(
            ["git", "-C", str(_dir_of(cwd)), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


_git_available_cache: Optional[bool] = None


def git_available() -> bool:
    """git 可执行是否存在 (缓存)。"""
    global _git_available_cache
    if _git_available_cache is None:
        try:
            cp = subprocess.run(
                ["git", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            _git_available_cache = (cp.returncode == 0)
        except (OSError, subprocess.SubprocessError):
            _git_available_cache = False
    return _git_available_cache


def git_toplevel(cwd: pathlib.Path) -> Optional[pathlib.Path]:
    """仓库工作区根 (git rev-parse --show-toplevel)。非仓库/无 git 返回 None。"""
    if not git_available():
        return None
    out = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if not out:
        return None
    return pathlib.Path(out)


def current_branch(cwd: pathlib.Path) -> Optional[str]:
    """当前分支名。detached HEAD (返回 'HEAD') 视为无分支 → None。"""
    if not git_available():
        return None
    out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if not out or out == "HEAD":
        return None
    return out


def all_branches(cwd: pathlib.Path) -> Set[str]:
    """所有分支名 (本地 + 远程), 已剥离 '* '、'remotes/' 前缀与 'origin/HEAD' 指针。

    用于 self-clean 判定某 baseline 对应的分支是否仍存在。无 git 返回空集
    (调用方应据此跳过清理, 不误删)。
    """
    if not git_available():
        return set()
    out = _run_git(["branch", "-a", "--format=%(refname:short)"], cwd)
    if out is None:
        return set()
    names: Set[str] = set()
    for line in out.splitlines():
        name = line.strip()
        if not name or name.endswith("/HEAD"):
            continue
        # 远程分支 origin/foo → 也收 foo, 同时保留 origin/foo 本身
        names.add(name)
        if "/" in name:
            names.add(name.split("/", 1)[1])
    return names


def head_commit(cwd: pathlib.Path) -> Optional[str]:
    """当前 HEAD 短 commit。无 git 返回 None。"""
    if not git_available():
        return None
    return _run_git(["rev-parse", "--short", "HEAD"], cwd)


def git_user(cwd: pathlib.Path) -> Optional[str]:
    """git user.name (供 _anchor.json 记录操作人)。无则 None。"""
    if not git_available():
        return None
    return _run_git(["config", "user.name"], cwd)


def slugify_branch(name: str) -> str:
    """分支名 → 文件系统安全的目录名。

    '/'、'\\' 与 Windows 非法字符替换为 '_'; 去首尾点/空格 (Windows 限制)。
    可逆性不强求, 原始分支名另存 _anchor.json。
    """
    slug = _ILLEGAL.sub("_", name).strip(" .")
    return slug or "_"


def resolve_branch_slug(
    cwd: pathlib.Path,
) -> Tuple[Optional[str], Optional[str], str]:
    """解析当前应使用的 baseline slug。

    返回 (slug, original_branch, reason):
      - slug 非 None: 正常按分支隔离, original_branch 为原始分支名。
      - slug 为 None: 应退化为 flat baseline, reason 说明原因 (供一次性提示)。
    """
    if not git_available():
        return (None, None, "未找到 git 可执行, 退化为单一基线")
    if git_toplevel(cwd) is None:
        return (None, None, "target 不在 git 仓库, 退化为单一基线")
    br = current_branch(cwd)
    if br is None:
        return (None, None, "detached HEAD / 无分支名, 退化为单一基线")
    return (slugify_branch(br), br, "")


# ============================================================
# 自检 (不依赖真实仓库的部分)
# ============================================================

def _self_test() -> bool:
    print("gitinfo 自检")
    print("─" * 60)
    all_ok = True

    # slugify
    cases = [
        ("main", "main"),
        ("feature/x", "feature_x"),
        ("origin/feature/x", "origin_feature_x"),
        ('a:b*c?d"e<f>g|h', "a_b_c_d_e_f_g_h"),
        ("  .weird. ", "weird"),
        ("/", "_"),
    ]
    for raw, expect in cases:
        got = slugify_branch(raw)
        ok = (got == expect)
        all_ok = all_ok and ok
        print(f"  {'✓' if ok else '✗'} slugify({raw!r}) = {got!r} (期望 {expect!r})")

    # 容错: 对一个保证非 git 仓库的临时目录, 所有查询应返回 None/空集不抛
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        try:
            tl = git_toplevel(d)
            cb = current_branch(d)
            ab = all_branches(d)
            slug, orig, reason = resolve_branch_slug(d)
            # 在非仓库下 (有 git 时) toplevel/branch 应为 None
            ok_safe = (cb is None) and isinstance(ab, set)
            # slug 在非仓库下应为 None 且带 reason
            ok_degrade = (slug is None and reason != "")
            all_ok = all_ok and ok_safe and ok_degrade
            print(f"  {'✓' if ok_safe else '✗'} 非仓库容错: toplevel={tl} branch={cb} branches={len(ab)}")
            print(f"  {'✓' if ok_degrade else '✗'} 非仓库退化: slug={slug} reason={reason!r}")
        except Exception as e:
            all_ok = False
            print(f"  ✗ 容错失败, 抛了异常: {e}")

    print(f"  git_available(): {git_available()}")
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
