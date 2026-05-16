<div align="center">

# MaaOWM — MaaFramework Overlay Workspace Manager

[![pip](https://img.shields.io/badge/requires-pip-3775A9?logo=pypi&logoColor=white)](https://pip.pypa.io)
[![Rich](https://img.shields.io/badge/dep-rich-af00ff?logo=python&logoColor=white)](https://github.com/Textualize/rich)
[![MaaFw](https://img.shields.io/badge/PyPI-MaaFw-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/MaaFw)
[![License](https://img.shields.io/badge/License-MIT-blue)](./LICENSE)
[![Stars](https://img.shields.io/github/stars/sunyink/MaaOWM?label=给项目点赞&color=f39c12&logo=github)](https://github.com/sunyink/MaaOWM)

</div>

> 为 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 多资源项目 (multi-target mod) 打造的覆盖包工作区管理器。
> 挂载时把 base + mod 合并成可编辑的全量工作区；卸载时按字段级语义 diff 提取最小化 mod 增量，让多端适配开发的循环干净又高效。

---

## 这是给谁用的

如果你在为同一个 MaaFramework 项目维护多个适配包 (例如 PC / Mobile / 不同设备分辨率)：

```
assets/resource/
├── base/              ← 通用 pipeline
├── PC/                ← PC 适配包 (仅含覆盖 base 的字段)
├── Mobile/            ← 移动端适配包
└── BlueStacks_4K/     ← 特定设备包
```

此时用 [MaaPipelineEditor](https://github.com/MaaXYZ/MaaPipelineEditor) 等编辑器开发其中某个适配包时，会遇到这些痛苦：

- **编辑器看不见 base 的字段**，只看到 PC 包里那寥寥几行 override
- 想试着改一个字段时，得手动从 base **复制全字段到 PC** 包，再改
- 改完后 PC 包变成一坨全字段的复制品，**和 base 的差异完全淹没**了
- git diff 一片红，看不清自己到底改了什么
- 友军 review 时不知道哪些是有意覆盖、哪些是无意残留

**MaaOWM 解决这个问题**：

```
挂载: base + PC → PC 目录变成全量工作区 (编辑器看见的世界完整了)
       ↓
       MaaPipelineEditor 正常开发, 改字段、加 task、调注释
       ↓
卸载: 工作区 - base → 写回 PC 包的最小 mod (只保留你真正改了的)
```

---

## 它如何工作

MaaOWM 完全信任 MaaFramework 自己的合并语义——不重新实现一套 merge/diff 算法。挂载/卸载所有阶段都通过 MaaFramework 的 PipelineParser + PipelineDumper 跑一遍真实加载，**得到的 canonical 形态就是运行时真实形态**。不会因为协议升级或字段细节同步不到位而漂移。

```
┌─────────────────────────────────────────────────────────────┐
│                  挂载 (mount)                                │
│                                                              │
│   base/ ─┐                                                   │
│          ├─→ MaaFW 加载 ─→ canonical (全字段) ─→ 工作区     │
│   PC/  ──┘                       │                           │
│                                   ↓                          │
│                            存快照到 .maaowm/snapshot.json    │
│                                                              │
│   工作区做了 def 剥离 + 节点重排 + extras 注入,              │
│   接近 base 的简洁风格便于阅读                               │
└─────────────────────────────────────────────────────────────┘

   ↓ 编辑器开发 ↓

┌─────────────────────────────────────────────────────────────┐
│                  卸载 (unmount)                              │
│                                                              │
│   工作区 ─→ MaaFW 加载 ─→ canonical_w                       │
│                              ↓                               │
│   .maaowm/snapshot.json → canonical_base                    │
│                              ↓                               │
│                        字段级 diff → minimal_mod             │
│                              ↓                               │
│                     def 剥离 (双重判定)                      │
│                     V1/V2 转译 + 紧凑写法 + extras 注入      │
│                              ↓                               │
│                          写回 PC/ 目录                        │
└─────────────────────────────────────────────────────────────┘
```

---

## 快速开始

### 1. 准备环境

MaaOWM 依赖 `rich` (终端 UI) 和 `maafw` (PyPI 包, 含 MaaFramework 的 Python 绑定):

```bash
pip install rich maafw
```

**重要**: 如果你的 MaaFramework 项目用了虚拟环境 (例如 `.venv/`)，请用该虚拟环境的 Python 运行 MaaOWM。MaaFramework 加载会触发 numpy/cv2 等 C 扩展，Python 版本必须对得上。MaaOWM 启动时会自动检测，发现问题会给出精准的修复命令。

### 2. 准备配置文件

在 MaaOWM 目录下创建 `overlay_config.json`:

```json
{
    "target": "../MFAyourProject/assets/resource/pc",
    "base_layers": ["../MFAyourProject/assets/resource/base"],
    "output_format": "v2"
}
```

最小配置就这些。**路径相对配置文件所在目录**。

### 3. 运行

```bash
python overlay_tool.py
```

进入 TUI 界面后按 `[M]` 挂载、`[U]` 卸载、`[C]` 检查工作区状态。

### 4. 编辑流程

```
[M] 挂载  →  用编辑器在 target 目录 (PC/) 改东西  →  [U] 卸载
```

挂载后 PC 目录会变成全字段的工作区，你的编辑器可以正常看到所有字段并修改。卸载时 MaaOWM 自动把改动浓缩为最小 mod，写回 PC 目录。

### 5. 在你的资源项目里忽略 `.maaowm/`

MaaOWM 挂载会在 target 适配包下生成 `.maaowm/` 目录存放快照、备份等工作状态。这些是本地状态文件, 不应进 git。

请在你**资源项目的根 `.gitignore`** 加一行:

```gitignore
# MaaOWM 工作状态与备份
.maaowm/
```

这样无论哪个适配包被挂载, 生成的 `.maaowm/` 都不会被 git 追踪。

---

## 配置详解

`overlay_config.json` 字段:

| 字段 | 必需 | 说明 |
|------|------|------|
| `target` | ✓ | mod 包路径 (要 overlay 编辑的那个适配包) |
| `base_layers` | ✓ | base 层列表 (按数组顺序覆盖, 后面的优先级高) |
| `output_format` | | `"v2"` (默认, 嵌套) 或 `"v1"` (拍平, MPE 风格) |
| `compact_node_refs` | | `true` (默认, next/on_error 用字符串紧凑形态) |
| `pipeline_subdir` | | pipeline 子目录名, 默认 `"pipeline"` |
| `maa_pkg_dir` | | maa 包路径, 默认 `null` 让 MaaOWM 自动从 `import maa` 找 |

**典型配置 (.venv 项目)**:

```json
{
    "target": "../MFAyourProject/assets/resource/pc",
    "base_layers": ["../MFAyourProject/assets/resource/base"]
}
```

**多 base 层 (例如 base + 通用 base)**:

```json
{
    "target": "../proj/assets/resource/PC",
    "base_layers": [
        "../proj/assets/resource/base",
        "../proj/assets/resource/common"
    ]
}
```

后面的 base 覆盖前面的。

**显式指定 maa 位置**:

```json
{
    "target": "...",
    "base_layers": ["..."],
    "maa_pkg_dir": "F:/projects/.venv/Lib/site-packages/maa"
}
```

通常不需要 —— `import maa` 能找到就行。

---

## 工作区编辑准则

挂载后的工作区文件长这样:

```jsonc
{
    "Weekly_LastNight_HomePage": {
        "doc": "识别左下抽抽乐-主页-点击选择章节",
        "focus": "PM.主页面",
        "next": ["Weekly_MoreBook_LastNight", "Weekly_LastNight_HomePageSwipe"],
        "recognition": "And",
        "all_of": [
            {
                "sub_name": "Main_OCR",
                "recognition": "OCR",
                "expected": ["抽抽乐"],
                "roi": [98, 656, 66, 32]
            },
            "Global_Main_Clr"
        ],
        "action": "Click",
        "target": [1111, 650, 1, 1]
    }
}
```

字段值等于框架默认的会省略, next 等用紧凑字符串, doc/desc 等注释保留。

### ✓ 允许做的事

- **改字段值** —— `post_delay: 3000` 改成 `200` 之类
- **给 task 加新字段**
- **新建 task**
- **改 / 加 / 删 doc/desc** 等注释字段 (会持久化)

### ✗ 不要做的事

#### 删字段想"还原 base 的值"
这是个反直觉的陷阱。工作区是**独立加载**的: 缺失字段会用**框架默认值**, 不是 base 的值。
如果你删掉 `post_delay`, 加载时 `post_delay` 会是默认 `200`, 而不是 base 写的 `1000`。

**做法**: 如要还原 base 的某字段, 直接把工作区的值改成你期望的形态。

#### 随意删被引用的 task
`next` / `on_error` 引用不存在的 task 会让 MaaFramework 拒绝加载。

**做法**: 改 `enabled: false` 而非删除整个 task。

### doc/desc 字段行为

工作区的 doc/desc 字段是从 base/mod 原始文件抓出来的 (MaaFramework 自己不管这些字段)。MaaOWM 在挂载时注入回工作区, 卸载时写回 mod:

| 你的操作 | 卸载后 mod 行为 | 下次挂载 |
|---------|----------------|---------|
| 改 doc 内容 | mod 写新 doc | 工作区显示新 doc |
| 整字段删 doc | mod **不写** | doc 从 base 重新注入 (等于撤回修改) |
| 写 `doc: ""` (空字符串) | mod 写 `doc: ""` | 工作区 doc 是空字符串 |

---

## 状态机与 TUI 操作

```
                  ┌──── [M] ────┐
                  │             ↓
            ┌──────────┐   ┌──────────┐
            │ 未挂载    │   │ 已挂载    │
            │ UNMOUNTED │   │ MOUNTED  │
            └──────────┘   └──────────┘
                  ↑             │
                  └──── [U] ────┘
                                │
                          [C] 检查 (随时)
                          [B] 备份 (随时)
                          [V] 切 V1/V2 (未挂载时)
                          [N] 切紧凑 (未挂载时)
```

### 主菜单

| 按键 | 状态 | 行为 |
|------|------|------|
| `[M]` | 未挂载 | 把 base + mod 合并写入工作区 |
| `[U]` | 已挂载 | 提取 minimal mod 写回 mod 包 |
| `[C]` | 已挂载 | dry-run 检查工作区是否能加载, 看文件级变动统计 |
| `[V]` | 未挂载 | 切换输出格式 V2 ↔ V1 |
| `[N]` | 未挂载 | 切换 next/on_error 紧凑写法开关 |
| `[L]` | 任意 | 看最近一次操作日志 |
| `[B]` | 任意 | 列出 .maaowm/ 下的备份 |
| `[H]` | 任意 | 使用说明 |
| `[Q]` | 任意 | 退出 |

### 安全机制

- **挂载前自动备份 mod 包** 到 `.maaowm/<timestamp>/mod/`
- **卸载前自动备份工作区** 到 `.maaowm/<timestamp>/work/`
- **卸载前自动 preflight**: 跑一次 dry-run, 工作区有语法/字段错误时拒绝执行卸载, 给具体错误位置
- **`.maaowm/` 目录** 存放快照、def 表、extras、备份等状态。删了状态会丢但备份还在

---

## 输出格式选项

### V2 (默认, 嵌套)

```json
{
    "Task1": {
        "recognition": {
            "type": "OCR",
            "param": {"expected": ["X"]}
        },
        "action": {
            "type": "Click",
            "param": {"target": [100, 200]}
        }
    }
}
```

### V1 (拍平, MaaPipelineEditor 风格)

```json
{
    "Task1": {
        "recognition": "OCR",
        "expected": ["X"],
        "action": "Click",
        "target": [100, 200]
    }
}
```

V1 模式下 `And`/`Or` 的 sub-recognition 也递归拍平。两种格式都接受紧凑 `next` 写法。

切换格式: TUI 里 `[V]` (仅未挂载时可切, 避免编辑半截改格式)。

---

## def 剥离 (字段级精简)

挂载时 MaaOWM 会自动探针出当前 MaaFramework 各 type 的默认字段表 (`OCR.threshold = 0.3` 这种), 然后:

- **挂载工作区**: 字段值 == 默认 → 不写出 (减少视觉噪音, 工作区接近 base 简洁形态)
- **卸载 mod 产物**: 字段值 == 默认 AND base 同字段也 == 默认 → 才剥 (双重判定)

双重判定是关键: 它保护"用户在 mod 写默认值想覆盖 base 非默认值"的情况。例如:

```
base 写: post_delay: 5000
工作区改: post_delay: 200   (用户想还原默认)
卸载 mod: post_delay: 200   ← 保留! 不会因为 200 是默认值就被吞掉
```

---

## extras (doc/desc 等非 MaaFramework 字段)

MaaFramework 自己不识别 `doc` / `desc` 这类注释字段, 但开发时这些字段对可读性至关重要。MaaOWM 自动:

1. 挂载前扫描 base + mod 原始 JSON, 收集所有非 MaaFramework 字段
2. 写工作区时注入回每个 task
3. 卸载时扫描工作区最新 extras, 和挂载时对比, 变化的 task 写回 mod

详细行为见上面"工作区编辑准则"的 doc/desc 部分。

---

## 节点顺序

挂载时记录 base 各文件中 task 的出现顺序到 `.maaowm/extras.json`, 写工作区/写 mod 时按这个顺序排:

- base 中出现过的 task → 按 base 顺序
- 工作区新增的 task → 按字母序排在文件末尾

这样 git diff 干净, base 的"叙事流"不被打乱。

---

## 已知限制

- **只处理 pipeline 文件**: image/model 不动 (passthrough)
- **不支持完全没装 maafw 的环境**: oracle 依赖 MaaFramework Python 绑定
- **挂载时会清空 mod 目录**: 备份在 `.maaowm/` 下, 误操作可恢复
- **同一时间只能挂载一个 mod**: 多 mod 项目按需切换 target 重新挂载

---

## 故障速查

### 启动报错: `ModuleNotFoundError: No module named 'numpy.core._multiarray_umath'`

**原因**: 当前 Python 解释器和 MaaFramework 所在环境不匹配。

**解决**: MaaOWM 自动检测并给出精准命令。如果你的项目用 `.venv`, 会提示用 `.venv/Scripts/python.exe overlay_tool.py` 运行。

### 卸载报错: `加载工作区失败`

**原因**: 工作区有 JSON 语法错误或字段类型错误。

**解决**: 用 VSCode + [MaaSupport 插件](https://marketplace.visualstudio.com/items?itemName=MaaXYZ.maasupport) 查具体错误位置。修好再卸载。或在 TUI 按 `[C]` 主动检查。

### 工作区某 task 在 base 已存在 mod 不写, 但卸载后 mod 包含了它

**原因**: 你可能改了它的 doc/desc 字段。MaaOWM 检测到 extras 变化会强制把 task 加进 mod, 注入新 doc。

### 挂载后看到一堆字段我不认识

**原因**: 工作区是 base + mod 合并后的 canonical 形态, 含有你 mod 里没写但 base 写了的字段。

**做法**: 这是正常的, 工作区就是给编辑器看的"完整世界"。卸载后这些 base 的字段不会进 mod。

### 切了 V2 ↔ V1 后, mod 里所有 task 都变了

**原因**: V1/V2 是不同的表达形式, 同样的语义在两种格式下文本表现不同。

**做法**: 在团队中协商好统一用一种, 然后挂载时保持一致。

---

## 进阶: 项目内部架构

如要深入了解 V3 的设计 (oracle 信任、双重判定剥离、字段路由等), 见 `ARCHITECTURE.md` (待补)。

模块概览:

```
overlay_tool.py        TUI 入口 (rich)
core/
  config.py            配置加载 + 路径解析
  oracle.py            MaaFramework 加载 + canonicalize 封装
  fixup.py             修补 dumper 输出 (sub_recognition 字段错位 bug)
  snapshot.py          挂载时快照 (canonical_base 持久化)
  routing.py           task → 原文件路径 索引 (origin.json)
  def_table.py         探 MaaFramework def 表 + 字段剥离
  diff.py              语义 diff (canonical_w vs canonical_base)
  deep_diff.py         子字段递归路 D 算法
  translator.py        V1/V2 转换 + next/wait_freezes 紧凑写法
  extras.py            非 MaaFramework 字段 (doc/desc) + 节点顺序
  preflight.py         工作区 dry-run 预检
  env_check.py         maa 环境预检 + 友好诊断
  inplace.py           mount/unmount 主流程
```

---

## 致谢

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework) —— 提供 oracle 语义的基础
- [MaaPipelineEditor](https://github.com/MaaXYZ/MaaPipelineEditor) —— 启发 V1 格式 + extras 处理思路

---

## License

MIT License. 见 [LICENSE](LICENSE).
