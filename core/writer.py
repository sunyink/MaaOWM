"""
回写模块。

将 Diff 结果写回到 Target Layer（覆盖包目录）。
- pipeline: 仅写入有差异的节点和字段；无差异文件保留空 {}
- image/model: 仅复制新增或修改的文件
"""

import json
import shutil
from pathlib import Path
from typing import Optional, Callable, List

from .config import OverlayConfig
from .differ import DiffResult


class WriteResult:
    """回写结果统计。"""

    def __init__(self):
        self.pipeline_files_written: int = 0
        self.pipeline_files_emptied: int = 0
        self.pipeline_files_skipped: int = 0
        self.image_files_written: int = 0
        self.model_files_written: int = 0
        self.image_files_removed: int = 0
        self.model_files_removed: int = 0
        self.errors: List[str] = []

    def summary(self) -> str:
        parts = []
        if self.pipeline_files_written:
            parts.append(f"Pipeline 写入 {self.pipeline_files_written} 文件")
        if self.pipeline_files_emptied:
            parts.append(f"Pipeline 清空 {self.pipeline_files_emptied} 文件")
        if self.pipeline_files_skipped:
            parts.append(f"Pipeline 跳过 {self.pipeline_files_skipped} 文件")
        if self.image_files_written:
            parts.append(f"Image 写入 {self.image_files_written} 文件")
        if self.image_files_removed:
            parts.append(f"Image 清理 {self.image_files_removed} 冗余")
        if self.model_files_written:
            parts.append(f"Model 写入 {self.model_files_written} 文件")
        if self.model_files_removed:
            parts.append(f"Model 清理 {self.model_files_removed} 冗余")
        if self.errors:
            parts.append(f"错误 {len(self.errors)} 个")
        return " | ".join(parts) if parts else "无操作"


def write_back(
    config: OverlayConfig,
    diff_result: DiffResult,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> WriteResult:
    """
    将差异结果回写到覆盖包目录。

    Args:
        config: 配置对象。
        diff_result: compute_diff 产出的差异结果。
        progress_callback: 进度回调。

    Returns:
        WriteResult 回写统计。
    """
    result = WriteResult()
    target = config.target_path
    workspace = config.workspace_path

    def log(msg: str):
        if progress_callback:
            progress_callback(msg)

    # === Pipeline 回写 ===
    if "pipeline" in config.resource_types:
        log("回写 Pipeline...")

        for rel_path, diff_info in diff_result.pipeline_diffs.items():
            out_path = target / rel_path

            if diff_info.has_changes:
                # 合并修改的节点和新增的节点
                overlay_data = {}
                overlay_data.update(diff_info.modified_nodes)
                overlay_data.update(diff_info.new_nodes)

                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(overlay_data, f, ensure_ascii=False, indent=4)

                result.pipeline_files_written += 1
            else:
                # 无差异：仅当覆盖包中原本就有该文件时才保留为空 {}
                # 如果覆盖包原本没有此文件，不创建新的空文件
                if out_path.exists():
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump({}, f, ensure_ascii=False, indent=4)
                    result.pipeline_files_emptied += 1
                else:
                    result.pipeline_files_skipped += 1

        log(
            f"Pipeline 回写完成: {result.pipeline_files_written} 写入, "
            f"{result.pipeline_files_emptied} 清空, "
            f"{result.pipeline_files_skipped} 跳过"
        )

    # === Image / Model 回写 ===
    for res_type in ["image", "model"]:
        if res_type not in config.resource_types:
            continue

        log(f"回写 {res_type.capitalize()}...")

        if res_type == "image":
            bin_diff = diff_result.image_diff
        else:
            bin_diff = diff_result.model_diff

        written = 0
        removed = 0

        # 写入新增和修改的文件
        for rel_path in bin_diff.new_files + bin_diff.modified_files:
            src = workspace / rel_path
            dst = target / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)

            if src.exists():
                shutil.copy2(src, dst)
                written += 1
            else:
                result.errors.append(f"工作区文件丢失: {src}")

        # 清理与 base 完全相同的冗余文件
        for rel_path in bin_diff.unchanged_files:
            redundant = target / rel_path
            if redundant.exists():
                redundant.unlink()
                removed += 1

        if res_type == "image":
            result.image_files_written = written
            result.image_files_removed = removed
        else:
            result.model_files_written = written
            result.model_files_removed = removed

        log(f"{res_type.capitalize()} 回写完成: {written} 写入, {removed} 清理")

    return result
