"""
正向合并引擎。

将多个 Base Layer 和 Target Layer 按顺序合并，输出到工作区。
- pipeline: JSON 节点级字段 dict merge（后覆盖前）
- image/model: 文件级覆盖（同相对路径后来居上）
"""

import json
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

from .config import OverlayConfig


# --- Pipeline JSON 合并 ---


def deep_merge_node(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """
    节点级字段合并（v1 扁平结构）。

    overlay 的字段直接覆盖 base 的同名字段。
    base 中有但 overlay 中没有的字段保留。

    NOTE: 当前为 v1 扁平策略。未来升级 v2 时，可在此处对
          'recognition' 和 'action' 字段做递归 dict merge。
    """
    merged = dict(base)
    merged.update(overlay)
    return merged


def merge_pipeline_files(layers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    合并多层 pipeline JSON 数据。

    每层是一个 {node_name: {field: value, ...}, ...} 的字典。
    按顺序逐层合并，后来居上。
    """
    result: Dict[str, Any] = {}

    for layer_data in layers:
        for node_name, node_fields in layer_data.items():
            if node_name in result:
                result[node_name] = deep_merge_node(result[node_name], node_fields)
            else:
                # 新节点，完整复制
                result[node_name] = dict(node_fields)

    return result


def _collect_pipeline_files(root_dir: Path) -> Dict[str, Path]:
    """
    收集目录下所有 pipeline JSON 文件，返回 {相对路径: 绝对路径} 映射。
    忽略以 '.' 开头的目录和文件，忽略以 '$' 开头的 root field（在加载时处理）。
    """
    result = {}
    pipeline_dir = root_dir / "pipeline"
    if not pipeline_dir.exists():
        return result

    for json_file in pipeline_dir.rglob("*.json"):
        # 跳过隐藏文件/目录
        parts = json_file.relative_to(root_dir).parts
        if any(part.startswith(".") for part in parts):
            continue
        rel_path = str(json_file.relative_to(root_dir))
        result[rel_path] = json_file

    return result


def _load_pipeline_json(file_path: Path) -> Dict[str, Any]:
    """加载单个 pipeline JSON 文件，过滤 '$' 开头的顶层字段。"""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 过滤 '$' 开头的字段（按 MaaFW 协议不解析）
    return {k: v for k, v in data.items() if not k.startswith("$")}


def _collect_binary_files(root_dir: Path, subdir: str) -> Dict[str, Path]:
    """收集 image 或 model 目录下的所有文件，返回 {相对路径: 绝对路径}。"""
    result = {}
    target_dir = root_dir / subdir
    if not target_dir.exists():
        return result

    for file_path in target_dir.rglob("*"):
        if file_path.is_file():
            parts = file_path.relative_to(root_dir).parts
            if any(part.startswith(".") for part in parts):
                continue
            rel_path = str(file_path.relative_to(root_dir))
            result[rel_path] = file_path

    return result


# --- 主合并流程 ---


class MergeResult:
    """合并结果统计。"""

    def __init__(self):
        self.pipeline_files: int = 0
        self.pipeline_nodes: int = 0
        self.image_files: int = 0
        self.model_files: int = 0
        self.errors: List[str] = []

    def summary(self) -> str:
        lines = []
        if self.pipeline_files:
            lines.append(f"Pipeline: {self.pipeline_files} 个文件, {self.pipeline_nodes} 个节点")
        if self.image_files:
            lines.append(f"Image: {self.image_files} 个文件")
        if self.model_files:
            lines.append(f"Model: {self.model_files} 个文件")
        if self.errors:
            lines.append(f"错误: {len(self.errors)} 个")
        return " | ".join(lines) if lines else "无内容"


def merge_to_workspace(
    config: OverlayConfig,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> MergeResult:
    """
    执行正向合并，将所有层合并后输出到工作区。

    Args:
        config: 已验证的配置对象。
        progress_callback: 进度回调，用于 TUI 显示。

    Returns:
        MergeResult 合并结果统计。
    """
    result = MergeResult()
    workspace = config.workspace_path

    def log(msg: str):
        if progress_callback:
            progress_callback(msg)

    # 清理并创建工作区
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    # 构建层列表：base_layers 按顺序 + target 最后
    all_layers = config.base_layer_paths() + [config.target_path]

    # === Pipeline 合并 ===
    if "pipeline" in config.resource_types:
        log("合并 Pipeline...")

        # 1. 收集所有层的所有 pipeline 文件
        # key: 相对路径, value: 按层顺序排列的 JSON 数据列表
        all_pipeline_data: Dict[str, List[Dict[str, Any]]] = {}

        for layer_path in all_layers:
            files = _collect_pipeline_files(layer_path)
            for rel_path, abs_path in files.items():
                try:
                    data = _load_pipeline_json(abs_path)
                    if rel_path not in all_pipeline_data:
                        all_pipeline_data[rel_path] = []
                    all_pipeline_data[rel_path].append(data)
                except Exception as e:
                    result.errors.append(f"加载失败 {abs_path}: {e}")

        # 2. 逐文件合并并写出
        for rel_path, layers_data in all_pipeline_data.items():
            merged = merge_pipeline_files(layers_data)
            out_path = workspace / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=4)

            result.pipeline_files += 1
            result.pipeline_nodes += len(merged)

        log(f"Pipeline 合并完成: {result.pipeline_files} 个文件")

    # === Image / Model 文件覆盖 ===
    for res_type in ["image", "model"]:
        if res_type not in config.resource_types:
            continue

        log(f"合并 {res_type.capitalize()}...")

        # 文件级覆盖：后来居上
        merged_files: Dict[str, Path] = {}
        for layer_path in all_layers:
            files = _collect_binary_files(layer_path, res_type)
            merged_files.update(files)  # 后来的覆盖前面的

        # 复制到工作区
        count = 0
        for rel_path, src_path in merged_files.items():
            dst_path = workspace / rel_path
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            count += 1

        if res_type == "image":
            result.image_files = count
        else:
            result.model_files = count

        log(f"{res_type.capitalize()} 合并完成: {count} 个文件")

    return result
