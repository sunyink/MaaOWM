"""
core/snapshot.py — canonical_base 快照管理

核心思路 (你的设计):
  mount 时: canonical_base = canonicalize(base)
            存到 .maaowm/snapshot.json, 永久固定
  unmount 时: 读快照当减数, 不重新 canonicalize(base)

为什么:
  - 隔离 mount 和 unmount 之间 base 文件可能发生的变化 (git pull 等)
  - 减数永远对应"用户挂载时看到的 base 状态", 与 workspace 直接对应
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import pathlib
import sys
import tempfile
from typing import Dict, List, Optional


SNAPSHOT_FILENAME = "snapshot.json"
SNAPSHOT_VERSION = "1"


@dataclasses.dataclass
class Snapshot:
    """挂载时刻的 base canonical 快照 + 元数据。"""
    version: str
    mount_ts: str                            # ISO 时间戳
    base_dirs: List[str]                     # 挂载时的 base 目录列表 (绝对路径字符串)
    base_fingerprint: str                    # canonical 的 sha256, 防被篡改
    canonical_base: Dict[str, dict]          # {task_name: full canonical dict}

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        d = json.loads(text)
        return cls(
            version=d["version"],
            mount_ts=d["mount_ts"],
            base_dirs=d["base_dirs"],
            base_fingerprint=d["base_fingerprint"],
            canonical_base=d["canonical_base"],
        )


def _fingerprint(canonical: Dict[str, dict]) -> str:
    """对 canonical 做稳定 sha256, 用于 unmount 时校验快照未被篡改。"""
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_snapshot(
    canonical_base: Dict[str, dict],
    base_dirs: List[pathlib.Path],
) -> Snapshot:
    """从 canonical 字典建立 Snapshot 对象。"""
    return Snapshot(
        version=SNAPSHOT_VERSION,
        mount_ts=datetime.datetime.now().isoformat(timespec="seconds"),
        base_dirs=[str(p.resolve()) for p in base_dirs],
        base_fingerprint=_fingerprint(canonical_base),
        canonical_base=canonical_base,
    )


def write_snapshot(snapshot: Snapshot, owm_dir: pathlib.Path) -> pathlib.Path:
    """写到 .maaowm/snapshot.json (atomic via os.replace)。"""
    owm_dir.mkdir(parents=True, exist_ok=True)
    target = owm_dir / SNAPSHOT_FILENAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(snapshot.to_json(), encoding="utf-8")
    tmp.replace(target)
    return target


def read_snapshot(owm_dir: pathlib.Path) -> Snapshot:
    """读 .maaowm/snapshot.json。校验版本和指纹。"""
    target = owm_dir / SNAPSHOT_FILENAME
    if not target.exists():
        raise SnapshotError(
            f"找不到 snapshot: {target}\n"
            f"  说明此目录未通过 OWM 挂载, 或 .maaowm/ 已损坏。"
        )
    snap = Snapshot.from_json(target.read_text(encoding="utf-8"))

    if snap.version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"snapshot 版本不匹配 (期望 {SNAPSHOT_VERSION}, 实际 {snap.version})"
        )

    actual_fp = _fingerprint(snap.canonical_base)
    if actual_fp != snap.base_fingerprint:
        raise SnapshotError(
            f"snapshot 指纹校验失败, 文件可能已被篡改。\n"
            f"  期望 sha256: {snap.base_fingerprint}\n"
            f"  实际 sha256: {actual_fp}"
        )
    return snap


class SnapshotError(Exception):
    pass


# ============================================================
# 自检
# ============================================================

def _self_test() -> bool:
    print("snapshot 自检")
    print("─" * 60)

    canonical = {
        "TaskA": {"recognition": {"type": "OCR", "param": {}}, "rate_limit": 1000},
        "TaskB": {"recognition": {"type": "DirectHit", "param": {}}, "post_delay": 200},
    }
    base_dirs = [pathlib.Path("/fake/base"), pathlib.Path("/fake/base2")]

    with tempfile.TemporaryDirectory() as tmp:
        owm_dir = pathlib.Path(tmp) / ".maaowm"

        snap1 = make_snapshot(canonical, base_dirs)
        path = write_snapshot(snap1, owm_dir)
        print(f"  写入: {path.relative_to(tmp)}")
        print(f"  指纹: {snap1.base_fingerprint[:16]}...")

        snap2 = read_snapshot(owm_dir)
        ok_roundtrip = (
            snap2.canonical_base == canonical
            and snap2.base_dirs == [str(p.resolve()) for p in base_dirs]
            and snap2.base_fingerprint == snap1.base_fingerprint
        )
        print(f"  round-trip: {'✓' if ok_roundtrip else '✗'}")

        # 篡改测试: 改 canonical 但不更新指纹
        target = owm_dir / SNAPSHOT_FILENAME
        text = target.read_text(encoding="utf-8")
        text = text.replace('"OCR"', '"FAKE"')
        target.write_text(text, encoding="utf-8")

        ok_tamper_detected = False
        try:
            read_snapshot(owm_dir)
        except SnapshotError as e:
            ok_tamper_detected = "指纹校验失败" in str(e)
        print(f"  篡改检测: {'✓' if ok_tamper_detected else '✗'}")

    return ok_roundtrip and ok_tamper_detected


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
