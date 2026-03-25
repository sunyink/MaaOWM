# MFABD2 覆盖包工作区管理器 (Overlay Workspace Manager)

解决 MaaFramework V2 跨端适配中的**"编辑器全量覆盖"**问题。

## 核心原理

```
正向合并: base + overlay → 工作区（全量，供编辑器使用）
逆向 Diff: 工作区 - base → 覆盖包（干净增量）
```

## 快速开始

### 1. 安装依赖

```bash
pip install rich
```

### 2. 准备配置文件

在资源目录下创建 `overlay_config.json`：

```json
{
    "workspace_dir": ".workspace",
    "target": "PC",
    "base_layers": ["base"],
    "resource_types": ["pipeline", "image", "model"]
}
```

**路径规则：**
- 支持绝对路径和相对路径（相对于配置文件所在目录）
- 支持 `../` 向上导航，例如工具在 `tools/` 下而资源在 `assets/` 下：
  `"target": "../assets/resource/PC"`
- `base_layers` 数组中，后面的覆盖前面的
- `target` 最后覆盖合并完毕的 base

### 3. 运行工具

```bash
# 自动查找当前目录下的 overlay_config.json
python overlay_tool.py

# 或指定配置文件
python overlay_tool.py /path/to/overlay_config.json
```

## 工作流程

```
[空闲] → [1]挂载工作区 → [工作区就绪] → 使用编辑器编辑 →
       → [2]刷新差异预览 → [差异已生成] →
       → [3]确认回写 → Git 提交 →
       → [4]卸载工作区 → [空闲]
```

1. **挂载工作区**：读取配置，将多层资源正向合并到临时工作区
2. **编辑**：用 MaaPipelineEditor 等编辑器打开工作区
3. **刷新预览**：查看哪些节点/文件发生了变更
4. **确认回写**：将干净的增量差异写回覆盖包目录
5. **卸载**：清理临时目录

## 目录结构示例

```
assets/resource/
├── base/                        ← 基础包
│   ├── pipeline/
│   │   ├── main.json
│   │   └── fishing.json
│   ├── image/
│   └── model/
├── PC/                          ← PC 覆盖包（仅含增量）
│   ├── pipeline/
│   │   └── fishing.json         ← 只有与 base 不同的字段
│   └── image/
├── .workspace/                  ← 工具生成的临时工作区
│   ├── pipeline/                  （包含合并后的全量数据）
│   ├── image/
│   └── model/
└── overlay_config.json          ← 工具配置
```

## Diff 引擎行为

### Pipeline（语义 Diff）

| 情况 | 行为 |
|---|---|
| 字段值与 base 相同 | **剔除**（不写入覆盖包） |
| 字段值与 base 不同 | **写入**覆盖包 |
| base 有但工作区删除了 | 写入对应**空值标记**（`[]` / `""` 等） |
| 工作区新增的节点 | **整个保留** |
| 整个文件无差异（覆盖包原有） | 保留为 **空 `{}`** |
| 整个文件无差异（覆盖包原无） | **跳过**（不新建空文件） |
| 整个节点被删除 | **警告**（不自动处理，需手动 `enabled: false`） |

### Image / Model（文件 Hash 比对）

| 情况 | 行为 |
|---|---|
| SHA256 与 base 不同 | 写入覆盖包 |
| SHA256 与 base 相同 | 不写入（清理冗余） |
| base 中不存在 | 写入覆盖包（新增） |

## 技术架构

```
overlay_tool/
├── overlay_tool.py          ← 主入口 + TUI（rich 终端界面）
├── core/
│   ├── config.py            ← 配置加载与路径解析
│   ├── merger.py            ← 正向合并引擎
│   ├── differ.py            ← 逆向语义 Diff 引擎
│   └── writer.py            ← 回写逻辑
└── tests/
    └── test_integration.py  ← 集成测试
```

### v2 扩展点

Diff 引擎使用策略模式。当前实现 `PipelineDiffV1`（扁平结构），未来可新增 `PipelineDiffV2`（嵌套结构）：

```python
from core.differ import PipelineDiffStrategy

class PipelineDiffV2(PipelineDiffStrategy):
    def diff_node(self, node_name, workspace_node, base_node):
        # 对 recognition/action 字段递归 dict merge 比对
        ...
```

使用时传入：

```python
from core.differ import compute_diff
diff = compute_diff(config, strategy=PipelineDiffV2())
```

## 注意事项

- 工作区活跃期间**不要切换 Git 分支**修改 base 层，如需更新请先卸载再重新挂载
- 如需禁用整个节点，请手动在覆盖包中添加 `"enabled": false`，工具不会自动生成
- JSON 格式化排序请使用 VSCode 的 prettier-plugin-maafw-sort 插件
- Pipeline Diff 基于 MaaFW v1 扁平结构的字段级语义比对，非文本行比对
