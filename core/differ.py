"""
逆向语义 Diff 引擎。

比对编辑后的工作区与合并后的 Base Layers，提取干净的增量差异。
- pipeline: 节点级字段语义比对（非文本行比对）
- image/model: SHA256 文件 hash 比对

设计为策略模式，当前实现 v1 扁平结构比对，预留 v2 嵌套结构扩展口。
"""

import json
import hashlib
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Set, Tuple

from .config import OverlayConfig
from .merger import (
    _collect_pipeline_files,
    _load_pipeline_json,
    _collect_binary_files,
    merge_pipeline_files,
)


# ============================================================
#  Pipeline Diff 策略接口（扩展点）
# ============================================================


class PipelineDiffStrategy(ABC):
    """
    Pipeline 节点比对策略抽象基类。

    未来升级 v2 嵌套结构时，只需新增子类实现即可。
    """

    @abstractmethod
    def diff_node(
        self,
        node_name: str,
        workspace_node: Dict[str, Any],
        base_node: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        比对单个节点，返回需要写入覆盖包的增量字段。

        Returns:
            - dict: 有差异的字段集合（覆盖包内容）
            - None: 完全无差异，该节点不需要出现在覆盖包中
        """
        ...


class PipelineDiffV1(PipelineDiffStrategy):
    """
    节点字段级比对策略，兼容 v1 和 v2。

    逐字段使用 Python == 比对（对 dict/list 做深度比较）。
    - v1 字段（str/int/list 等）：逐字段提取差异值。
    - v2 字段（recognition/action 为 dict）：深度比对其内部结构，
      仅输出有变化的子字段。与 merger 的 deep_merge_node 逻辑对称，
      确保覆盖包最小化且合并后能还原完整节点。

    同一文件内 v1/v2 节点可混用，比对逻辑自动适配。
    """

    def diff_node(
        self,
        node_name: str,
        workspace_node: Dict[str, Any],
        base_node: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        diff_fields: Dict[str, Any] = {}

        # 1. 检查修改和新增的字段
        for key, ws_value in workspace_node.items():
            if key not in base_node:
                # 新增字段（base 没有）
                diff_fields[key] = ws_value
            elif ws_value != base_node[key]:
                # 字段值变化
                if key in ("recognition", "action") and \
                        isinstance(ws_value, dict) and isinstance(base_node[key], dict):
                    # V2 嵌套结构：深度 diff，与 merger.deep_merge_node 对称
                    v2_diff = _diff_v2_field(ws_value, base_node[key])
                    if v2_diff:
                        diff_fields[key] = v2_diff
                else:
                    # V1 或普通字段：直接输出变化值
                    diff_fields[key] = ws_value

        # 2. 检查删除的字段（base 有但工作区没有）
        for key in base_node:
            if key not in workspace_node:
                # 字段被删除，写入对应的空值标记
                diff_fields[key] = _get_empty_value(key, base_node[key])

        if not diff_fields:
            return None

        return diff_fields


def _diff_v2_field(
    ws_field: Dict[str, Any],
    base_field: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    深度比对 V2 的 recognition / action 字典。

    与 merger.deep_merge_node 中对 recognition/action 的合并逻辑严格对称：
    - type: 直接比较，变化则输出
    - param: 当两边都是 dict 时，逐 key 比较，仅输出变化的 param key
    - 其他子字段: 直接比较，变化则输出

    Returns:
        有差异的子字段字典，或 None（完全无差异）。
    """
    diff: Dict[str, Any] = {}

    # 1. 检查修改和新增的子字段
    for sub_key, ws_val in ws_field.items():
        if sub_key not in base_field:
            # 新增子字段
            diff[sub_key] = ws_val
        elif ws_val != base_field[sub_key]:
            if sub_key == "param" and \
                    isinstance(ws_val, dict) and isinstance(base_field[sub_key], dict):
                # param 子字典：逐 key 比对，与 merger 的 param 递归 merge 对称
                param_diff = _diff_dict_shallow(ws_val, base_field[sub_key])
                if param_diff:
                    diff["param"] = param_diff
            else:
                # type 或其他子字段：直接输出变化值
                diff[sub_key] = ws_val

    # 2. 检查删除的子字段
    for sub_key in base_field:
        if sub_key not in ws_field:
            diff[sub_key] = _get_empty_value(sub_key, base_field[sub_key])

    return diff if diff else None


def _diff_dict_shallow(
    ws_dict: Dict[str, Any],
    base_dict: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    浅层 dict 比对，用于 param 级别。

    逐 key 比较，仅输出有变化或新增的 key。
    对于被删除的 key，输出对应类型的空值标记。
    """
    diff: Dict[str, Any] = {}

    for key, ws_val in ws_dict.items():
        if key not in base_dict:
            diff[key] = ws_val
        elif ws_val != base_dict[key]:
            diff[key] = ws_val

    for key in base_dict:
        if key not in ws_dict:
            diff[key] = _get_empty_value(key, base_dict[key])

    return diff if diff else None


def _get_empty_value(field_name: str, original_value: Any) -> Any:
    """
    为被删除的字段生成空值标记。

    根据原始值类型和 MaaFW Pipeline 协议返回合适的空值，
    使覆盖包能正确切断对底层的继承。
    """
    if isinstance(original_value, list):
        return []
    elif isinstance(original_value, str):
        return ""
    elif isinstance(original_value, bool):
        # bool 必须在 int 之前检查（Python 中 bool 是 int 子类）
        return False
    elif isinstance(original_value, int):
        return 0
    elif isinstance(original_value, float):
        return 0.0
    elif isinstance(original_value, dict):
        return {}
    elif original_value is None:
        return None
    else:
        return None


# ============================================================
#  文件级 Hash 比对
# ============================================================


def _sha256_file(file_path: Path) -> str:
    """计算文件的 SHA256 哈希值。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================
#  Diff 结果数据结构
# ============================================================


@dataclass
class FileDiffInfo:
    """单个 pipeline 文件的 Diff 结果。"""

    rel_path: str
    modified_nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    new_nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    unchanged_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.modified_nodes or self.new_nodes)


@dataclass
class BinaryDiffInfo:
    """image/model 目录的 Diff 结果。"""

    new_files: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    unchanged_files: List[str] = field(default_factory=list)


@dataclass
class DiffResult:
    """完整的 Diff 结果。"""

    pipeline_diffs: Dict[str, FileDiffInfo] = field(default_factory=dict)
    image_diff: BinaryDiffInfo = field(default_factory=BinaryDiffInfo)
    model_diff: BinaryDiffInfo = field(default_factory=BinaryDiffInfo)
    errors: List[str] = field(default_factory=list)

    def summary_lines(self) -> List[str]:
        """生成可读的摘要行。"""
        lines = []

        # Pipeline 摘要
        total_modified = 0
        total_new = 0
        total_unchanged = 0

        for rel_path, diff_info in self.pipeline_diffs.items():
            mod = len(diff_info.modified_nodes)
            new = len(diff_info.new_nodes)
            unch = diff_info.unchanged_count
            total_modified += mod
            total_new += new
            total_unchanged += unch

            status_parts = []
            if mod:
                status_parts.append(f"修改 {mod}")
            if new:
                status_parts.append(f"新增 {new}")
            if unch:
                status_parts.append(f"无变化 {unch}")

            status = " | ".join(status_parts) if status_parts else "空文件"

            # 标记整个文件是否有差异
            marker = "  " if diff_info.has_changes else "○ "
            lines.append(f"  {marker}{rel_path}: {status}")

        if self.pipeline_diffs:
            lines.insert(0, "[Pipeline]")
            lines.append(
                f"  合计: 修改 {total_modified} 节点 | "
                f"新增 {total_new} 节点 | "
                f"剔除 {total_unchanged} 无变化节点"
            )

        # Image 摘要
        img = self.image_diff
        if img.new_files or img.modified_files or img.unchanged_files:
            lines.append("[Image]")
            parts = []
            if img.new_files:
                parts.append(f"新增 {len(img.new_files)}")
            if img.modified_files:
                parts.append(f"修改 {len(img.modified_files)}")
            if img.unchanged_files:
                parts.append(f"剔除 {len(img.unchanged_files)}")
            lines.append(f"  {' | '.join(parts)}")

        # Model 摘要
        mdl = self.model_diff
        if mdl.new_files or mdl.modified_files or mdl.unchanged_files:
            lines.append("[Model]")
            parts = []
            if mdl.new_files:
                parts.append(f"新增 {len(mdl.new_files)}")
            if mdl.modified_files:
                parts.append(f"修改 {len(mdl.modified_files)}")
            if mdl.unchanged_files:
                parts.append(f"剔除 {len(mdl.unchanged_files)}")
            lines.append(f"  {' | '.join(parts)}")

        if self.errors:
            lines.append(f"[错误] {len(self.errors)} 个")
            for err in self.errors:
                lines.append(f"  ! {err}")

        return lines


# ============================================================
#  主 Diff 流程
# ============================================================


def compute_diff(
    config: OverlayConfig,
    strategy: Optional[PipelineDiffStrategy] = None,
) -> DiffResult:
    """
    计算工作区相对于 Base Layers 的差异。

    Args:
        config: 已验证的配置对象。
        strategy: Pipeline 比对策略，默认使用 v1 扁平策略。

    Returns:
        DiffResult 差异结果。
    """
    if strategy is None:
        strategy = PipelineDiffV1()

    result = DiffResult()
    workspace = config.workspace_path
    base_paths = config.base_layer_paths()

    # === Pipeline Diff ===
    if "pipeline" in config.resource_types:
        # 1. 合并 base layers 得到基准数据（不含 target）
        base_merged: Dict[str, Dict[str, Any]] = {}  # {rel_path: {node: fields}}
        for layer_path in base_paths:
            files = _collect_pipeline_files(layer_path)
            for rel_path, abs_path in files.items():
                try:
                    data = _load_pipeline_json(abs_path)
                    if rel_path not in base_merged:
                        base_merged[rel_path] = {}
                    base_merged[rel_path] = merge_pipeline_files(
                        [base_merged[rel_path], data]
                    )
                except Exception as e:
                    result.errors.append(f"加载 base 失败 {abs_path}: {e}")

        # 2. 读取工作区 pipeline
        ws_files = _collect_pipeline_files(workspace)

        # 3. 逐文件逐节点比对
        all_rel_paths = set(base_merged.keys()) | set(ws_files.keys())

        for rel_path in sorted(all_rel_paths):
            diff_info = FileDiffInfo(rel_path=rel_path)

            # 加载工作区数据
            ws_data: Dict[str, Any] = {}
            if rel_path in ws_files:
                try:
                    ws_data = _load_pipeline_json(ws_files[rel_path])
                except Exception as e:
                    result.errors.append(f"加载工作区失败 {ws_files[rel_path]}: {e}")
                    continue

            base_data = base_merged.get(rel_path, {})

            # 比对每个节点
            all_nodes = set(ws_data.keys()) | set(base_data.keys())

            for node_name in all_nodes:
                if node_name in ws_data and node_name not in base_data:
                    # 工作区新增的节点（base 没有）
                    diff_info.new_nodes[node_name] = ws_data[node_name]

                elif node_name in ws_data and node_name in base_data:
                    # 两边都有，做字段级 Diff
                    node_diff = strategy.diff_node(
                        node_name, ws_data[node_name], base_data[node_name]
                    )
                    if node_diff is not None:
                        diff_info.modified_nodes[node_name] = node_diff
                    else:
                        diff_info.unchanged_count += 1

                else:
                    # base 有但工作区没有该节点
                    # 这意味着用户在工作区删除了整个节点。
                    # 我们不自动生成 enabled:false，仅发出警告。
                    result.errors.append(
                        f"警告: 节点 '{node_name}' 在 {rel_path} 中被删除。"
                        f"如需禁用，请在覆盖包中手动添加 \"enabled\": false。"
                    )

            result.pipeline_diffs[rel_path] = diff_info

    # === Image / Model Diff ===
    for res_type in ["image", "model"]:
        if res_type not in config.resource_types:
            continue

        # 合并 base 层的文件 hash 映射
        base_hashes: Dict[str, str] = {}  # {rel_path: sha256}
        for layer_path in base_paths:
            files = _collect_binary_files(layer_path, res_type)
            for rel_path, abs_path in files.items():
                base_hashes[rel_path] = _sha256_file(abs_path)

        # 工作区的文件 hash
        ws_files_bin = _collect_binary_files(workspace, res_type)

        diff_info_bin = BinaryDiffInfo()

        for rel_path, ws_abs in ws_files_bin.items():
            ws_hash = _sha256_file(ws_abs)

            if rel_path not in base_hashes:
                diff_info_bin.new_files.append(rel_path)
            elif ws_hash != base_hashes[rel_path]:
                diff_info_bin.modified_files.append(rel_path)
            else:
                diff_info_bin.unchanged_files.append(rel_path)

        if res_type == "image":
            result.image_diff = diff_info_bin
        else:
            result.model_diff = diff_info_bin

    return result
