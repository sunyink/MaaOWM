"""
配置加载与路径解析模块。

支持绝对路径和相对路径（相对于配置文件所在目录）。
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class OverlayConfig:
    """覆盖包工作区配置。"""

    workspace_dir: str = ".workspace"
    target: str = ""
    base_layers: List[str] = field(default_factory=list)
    resource_types: List[str] = field(
        default_factory=lambda: ["pipeline", "image", "model"]
    )

    # --- 运行时解析后的绝对路径 (不序列化) ---
    config_root: Path = field(default_factory=lambda: Path("."), repr=False)

    def resolve_path(self, raw_path: str) -> Path:
        """将路径解析为绝对路径。若已是绝对路径则直接返回，否则相对于配置文件目录。"""
        p = Path(raw_path)
        if p.is_absolute():
            return p
        return (self.config_root / p).resolve()

    @property
    def workspace_path(self) -> Path:
        return self.resolve_path(self.workspace_dir)

    @property
    def target_path(self) -> Path:
        return self.resolve_path(self.target)

    def base_layer_paths(self) -> List[Path]:
        return [self.resolve_path(layer) for layer in self.base_layers]

    def validate(self) -> List[str]:
        """校验配置，返回错误信息列表。空列表表示通过。"""
        errors = []

        if not self.target:
            errors.append("配置缺少 'target' 字段（目标覆盖包目录）。")

        if not self.base_layers:
            errors.append("配置缺少 'base_layers' 字段（至少需要一个底层依赖）。")

        for i, layer in enumerate(self.base_layers):
            path = self.resolve_path(layer)
            if not path.exists():
                errors.append(f"base_layers[{i}] 路径不存在: {path}")

        if self.target:
            target = self.target_path
            if not target.exists():
                errors.append(f"target 路径不存在: {target}")

        if not self.resource_types:
            errors.append("resource_types 不能为空。")

        valid_types = {"pipeline", "image", "model"}
        for rt in self.resource_types:
            if rt not in valid_types:
                errors.append(f"未知的 resource_type: '{rt}'，支持: {valid_types}")

        return errors


def load_config(config_path: str | Path) -> OverlayConfig:
    """从 JSON 文件加载配置。"""
    config_path = Path(config_path).resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    config = OverlayConfig(
        workspace_dir=data.get("workspace_dir", ".workspace"),
        target=data.get("target", ""),
        base_layers=data.get("base_layers", []),
        resource_types=data.get("resource_types", ["pipeline", "image", "model"]),
        config_root=config_path.parent,
    )

    return config
