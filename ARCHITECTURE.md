# MaaOWM V3 架构文档

> 本文档面向想深入理解 MaaOWM 设计、或准备接手维护的开发者。
> 如果你只是想用这个工具，请看 [README.md](README.md)。
>
> 这份文档记录的是"为什么这样设计"——代码会变，但设计意图是持久的。
> 它也是作者给未来的自己留的备忘。

---

## 目录

1. [项目定位](#1-项目定位)
2. [核心设计哲学](#2-核心设计哲学)
3. [数据流全景](#3-数据流全景)
4. [模块清单与职责](#4-模块清单与职责)
5. [关键算法](#5-关键算法)
6. [.maaowm 状态目录](#6-maaowm-状态目录)
7. [Bug 修复史](#7-bug-修复史)
8. [版本演进](#8-版本演进)
9. [已知限制与未来方向](#9-已知限制与未来方向)
10. [给接手者的话](#10-给接手者的话)

---

## 1. 项目定位

MaaOWM (MaaFramework Overlay Workspace Manager) 解决 MaaFramework 多适配包项目的一个具体痛点：

当一个项目有 `base/` 通用包 + 多个适配包 (`PC/`、`Mobile/` 等)，适配包通常只写"覆盖 base 的那几个字段"。但图形编辑器 (MaaPipelineEditor 等) 打开适配包时，只能看到这寥寥几行 override，看不到 base 的完整上下文，开发体验很差。

MaaOWM 的方案：

- **挂载 (mount)**：把 `base + 适配包` 用 MaaFramework 真实加载一遍，合并成全字段工作区写回适配包目录。编辑器现在能看到完整世界。
- **卸载 (unmount)**：把工作区和 base 做字段级 diff，提取出最小化的 mod 增量，写回适配包目录。

挂载/卸载之间，开发者用编辑器正常工作。

### 与 V2 的关系

V3 是对 V2 的彻底重写。V2 自己实现了一套 merge/diff 算法，结果是：diff 出来的总是整个节点而非字段级增量、没动过的 JSON 被替换成空对象。根本问题是 **V2 在重新实现 MaaFramework 的合并语义**，永远追不上框架的真实行为。V3 的核心转变就是不再自己实现这套语义（见下一节）。

---

## 2. 核心设计哲学

### 2.1 信任 MaaFramework 作为 oracle

这是 V3 最重要的决策。

**oracle** 在这里指"权威答案的来源"。MaaOWM 不自己实现 pipeline 的 merge 和 canonical 化——它把 JSON 喂给 MaaFramework 的 `PipelineParser` + `PipelineDumper` 真实跑一遍，**dumper 吐出来的形态就是 canonical**。

为什么这样做：

- MaaFramework 的合并语义（字段默认值、V1/V2 兼容、字段类型校验）是一套复杂且持续演进的规则。任何"在外部重新实现一遍"的尝试都会和真实行为漂移。
- 用 oracle 后，MaaOWM 看到的 canonical 永远等于运行时真实形态。框架升级了？重新探一遍 def 表即可，不用改 diff 逻辑。

代价：MaaOWM 强依赖 MaaFramework 的 Python 绑定 (`maafw` PyPI 包)，且依赖 dumper 的输出质量（dumper 有 bug 时需要 fixup，见 [第 7 节](#7-bug-修复史)）。

### 2.2 快照减数

挂载时，MaaOWM 把 `canonical_base`（base 层的 canonical 全字段形态）存进 `.maaowm/snapshot.json`。

卸载时做 diff，**被减数不是"当前重新加载的 base"，而是挂载时存的那份快照**。

为什么：把 diff 的基准固定在挂载那一刻。这样即使开发者在挂载期间 `git pull` 改了 base，卸载时的 diff 依然是相对"挂载时的 base"算的，行为可预测。挂载和卸载之间 base 的变动与 MaaOWM 解耦。

（这条是开发过程中由项目作者提出的洞察。）

### 2.3 文件路由 = 备份 + 替换

挂载时 MaaOWM 建立一个 `origin` 索引：`{task_name: 原文件相对路径}`，存进 `.maaowm/origin.json`。

挂载会**清空适配包的 pipeline 目录**（原内容已备份到 `.maaowm/<timestamp>/`），然后把合并后的工作区写进去。

卸载时按 `origin` 索引，把每个 task 写回它原本所在的文件，保持适配包的目录结构不被打乱。

### 2.4 只处理 pipeline

image / model 文件 MaaOWM 不碰，挂载/卸载时原样保留（passthrough）。MaaOWM 的所有 diff/剥离逻辑只作用于 pipeline JSON。

---

## 3. 数据流全景

### 3.1 挂载 (mount) 链路

```
1.  读 overlay_config.json, 解析路径
2.  oracle.init() — 加载 MaaFramework Python 绑定
3.  oracle.canonicalize(base 各层) → canonical_base (全字段形态)
4.  snapshot.make_snapshot(canonical_base) → 存 .maaowm/snapshot.json
5.  def_table.build_def_tables(base) → 探针出各 type 的默认字段表
                                       存 .maaowm/def_tables.json
6.  extras.collect_layered_extras(base + mod) → 收集 doc/desc 等非 MaaFW 字段
                                                 + 节点顺序
                                                 存 .maaowm/extras.json
7.  routing 建立 origin 索引 (task → 原文件) → 存 .maaowm/origin.json
8.  oracle.canonicalize_overlay(base + mod) → canonical_merged (合并全字段)
9.  备份当前 mod 包到 .maaowm/<timestamp>/mod/
10. 清空 mod 的 pipeline 目录
11. 写文件流水线:
      canonical_merged
        → def 剥离 (按 def 表, 不带 base 对比 — 工作区独立加载靠 def)
        → V1/V2 转译 (按 output_format)
        → next/on_error 紧凑写法 (按 compact_node_refs)
        → wait_freezes 紧凑写法
        → extras 注入 (doc/desc 塞回每个 task)
        → 按 node_order 重排各文件
        → routing.write_mod_files (按 origin 索引写回各文件)
12. 写工作区根目录的 __OWM_README__.md
```

挂载后，适配包目录里是全字段（但经过 def 剥离精简）的工作区，编辑器可以正常打开。

### 3.2 卸载 (unmount) 链路

```
1.  读 config, oracle.init()
2.  preflight.validate_workspace() — dry-run 加载工作区, 验证没有语法/字段错误
      失败 → 拒绝卸载, 报告错误位置
3.  备份当前工作区到 .maaowm/<timestamp>/work/
4.  读 .maaowm/def_tables.json (挂载时存的)
5.  扫工作区原始 JSON, 收集最新 extras + 节点顺序 (用户可能改了 doc/desc)
      ★ 必须在 canonicalize 之前 — oracle 不输出 doc/desc
6.  oracle.canonicalize(工作区) → canonical_w
7.  读 .maaowm/snapshot.json → canonical_base
8.  diff.compute_minimal_mod(canonical_w, canonical_base) → minimal_mod
      逐 task 分类: IDENTICAL / MODIFIED / MOD_ONLY / DELETED
9.  extras.diff_extras(工作区 extras vs 挂载时 extras)
      extras 变了但 oracle 看 IDENTICAL 的 task → 强制加进 minimal_mod
10. def 剥离 (带 canonical_base — 双重判定, 见 5.2)
11. V1/V2 转译 + next/wait_freezes 紧凑写法
12. extras 注入 (把工作区的 doc/desc 塞回 minimal_mod)
13. 按 node_order 重排
14. routing.write_mod_files — 按 origin 索引写回各文件
15. 清理 .maaowm/ 下的状态文件 (snapshot/origin/def_tables/extras)
      备份保留
```

### 3.3 关键中间态

| 名称 | 含义 | 哪里产生 | 存哪 |
|------|------|---------|------|
| `canonical_base` | base 层的 canonical 全字段形态 | mount step 3 | snapshot.json |
| `canonical_merged` | base+mod 合并的 canonical | mount step 8 | 内存 (写工作区) |
| `canonical_w` | 工作区的 canonical | unmount step 6 | 内存 |
| `def_tables` | 各 type 的默认字段表 | mount step 5 | def_tables.json |
| `minimal_mod` | diff 出的最小 mod 增量 | unmount step 8 | 内存 (写 mod) |
| `origin` 索引 | task → 原文件路径 | mount step 7 | origin.json |
| `extras` | 非 MaaFW 字段 + 节点顺序 | mount step 6 | extras.json |

---

## 4. 模块清单与职责

代码在 `maaowm-v3/` 目录下。入口是 `overlay_tool.py`，核心逻辑在 `core/`。

### `overlay_tool.py` (~610 行)

TUI 入口。基于 `rich` 库的终端界面。负责状态机 (未挂载 / 已挂载)、菜单按键分发 ([M]/[U]/[C]/[V]/[N]/[L]/[B]/[H]/[Q])、用户确认交互。所有重逻辑委托给 `core/inplace.py`。挂载/卸载/检查前调 `env_check` 预检 maa 环境。

### `core/config.py` (~265 行)

配置加载。解析 `overlay_config.json`，处理相对路径（相对配置文件目录）、`../` 向上导航。提供 `OverlayConfig` 数据类，含 `target` / `base_layers` / `output_format` / `compact_node_refs` / `maa_pkg_dir` 等字段，以及 `base_pipeline_dirs()` / `workspace_pipeline_dir()` / `owm_dir` 等路径计算方法。

### `core/oracle.py` (~319 行)

MaaFramework 加载封装——MaaOWM 的"oracle 接口"。`init()` 加载 maa 绑定；`canonicalize(dir)` 把一个目录的 pipeline 喂给 MaaFramework 加载并 dump 出 canonical 形态；`canonicalize_overlay(layers)` 加载多层并合并。内部含 JSONC 解析、`load_pipeline_json()` 保序 JSON 读取。所有"问 MaaFramework 真实行为"的请求都走这里。

### `core/fixup.py` (~241 行)

修补 PipelineDumper 的输出缺陷。目前主要处理 dumper 输出 sub_recognition 时 `type`/`param` 字段嵌套层级错误的 bug（见 [7.1](#71-dumper-sub_recognition-字段错位)）。fixup 在 oracle 内部跑，确保给上层的 canonical 是 parser 能重新接受的形态。

### `core/snapshot.py` (~160 行)

挂载时快照。`make_snapshot(canonical_base)` 把 base 的 canonical 形态 + sha256 指纹存进 `.maaowm/snapshot.json`。卸载时读回作为 diff 的被减数。实现"快照减数"设计（见 [2.2](#22-快照减数)）。

### `core/routing.py` (~247 行)

文件路由。挂载时 `build_origin_index()` 扫描原 mod 目录，建立 `{task_name: 相对路径}` 索引。`group_by_target_file()` 把 pipeline 按 origin 索引分组。`write_mod_files()` 把分组后的 task 写入各文件——**信任调用方传入的 task 顺序**（不强制排序，配合 extras 的 node_order）。

### `core/def_table.py` (~1176 行)

最大的模块。两部分职责：

**探 def 表**：`build_def_tables(base_dir)` 对每个已知的 recognition / action type，构造一个空 task 喂给 oracle，看 dumper 给出什么默认值——这就是该 type 的默认字段表。探针失败的 type（如外接模型 NeuralNetwork 系列）自动进黑名单。动态形成的白名单 = 探针成功的 type 集合。进程级缓存避免重复探针。

**字段剥离**：`strip_mod_with_def()` 按 def 表把"值等于默认值"的字段剥掉。卸载端额外接收 `canonical_base` 做"双重判定"（见 [5.2](#52-双重判定剥离)）。处理 task 顶层标量、recognition/action 的 param、wait_freezes、attach/anchor 嵌套、And/Or 的 sub-recognition 递归。

### `core/diff.py` (~277 行)

语义 diff 主流程。`compute_minimal_mod(canonical_w, canonical_base)` 逐 task 比对，分四类：

- **IDENTICAL**：与 base 完全一致，不写 mod
- **MODIFIED**：有字段级差异，调用 `deep_diff` 提取差异字段
- **MOD_ONLY**：base 没有此 task（新建），整段保留
- **DELETED**：base 有但工作区没了，警告（不自动处理）

### `core/deep_diff.py` (~335 行)

子字段递归 diff（项目内俗称"路 D"）。`deep_filter_raw_delta()` 处理 MODIFIED task 中的 dict 类型字段：递归进入嵌套 dict，逐子字段和 base 对比，相等的剥掉、不等的保留。这让 diff 能做到真正的字段级精度，而不是"整个 recognition 对象一起写"。

### `core/translator.py` (~635 行)

格式转换。三块：

- **V1 ↔ V2 转译**：`task_v2_to_v1()` 把嵌套形态拍平成 MaaPipelineEditor 风格。含 `_sub_v2_to_v1()` 递归处理 And/Or 的 sub-recognition。
- **next/on_error 紧凑写法**：`simplify_node_refs_in_pipeline()` 把 `{next: "X"}` 这种 dict 退化成字符串 `"X"`，带 `[JumpBack]`/`[Anchor]` 前缀语法。
- **wait_freezes 紧凑写法**：`simplify_wait_freezes_in_pipeline()` 把仅含 `time` 一个字段的 `pre_wait_freezes` 退化成标量。

### `core/extras.py` (~704 行)

非 MaaFramework 字段处理。MaaFramework 不识别 `doc`/`desc` 这类注释字段，oracle 也不会输出它们。本模块：

- `build_maafw_field_sets()`：合并硬编码已知字段集 + 动态探针表，得到"什么是 MaaFramework 字段"的全集。不在全集里的就是 extras。
- `collect_layered_extras()`：扫 base + mod 原始 JSON，按层覆盖式合并，收集 extras + 节点顺序。
- `inject_extras_into_pipeline()`：把 extras 注入回 pipeline（顶层 + sub-node 递归）。
- `reorder_pipeline_by_node_order()`：按记录的 base 节点顺序重排。
- `diff_extras()`：对比工作区和挂载时的 extras，找出用户改过 doc/desc 的 task。

### `core/preflight.py` (~322 行)

卸载前的 dry-run 预检。`validate_workspace()` 模拟加载工作区，捕捉 JSON 语法错误、字段类型错误等，在真正卸载前拦截。也提供文件级变动统计供 [C] 检查菜单使用。

### `core/env_check.py` (~221 行)

maa 环境预检。挂载/卸载/检查前调用。`precheck()` 尝试 `import maa`，失败时构造友好诊断——展示当前 Python 版本、maa 路径、常见原因；若从 `maa_pkg_dir` 路径识别出虚拟环境，附上精准的"用该环境 Python 运行"命令。不抛异常，返回 `EnvError` 让 TUI 自行决定显示。

### `core/inplace.py` (~700 行)

mount / unmount 主流程编排。把上述所有模块串成 [第 3 节](#3-数据流全景)描述的两条链路。也含 `__OWM_README__.md` 的文案、备份逻辑、状态文件读写。这是理解数据流的入口文件。

---

## 5. 关键算法

### 5.1 def 探针与白名单/黑名单

问题：要剥离"等于默认值"的字段，得先知道每个 type 的默认值是什么。MaaFramework 没有公开这套默认值表。

解法：**主动探针**。对每个已知 type（OCR、TemplateMatch、Click、Swipe 等），构造一个最小 task `{"recognition": "OCR"}` 喂给 oracle，dumper 会把所有字段连同默认值一起吐出来——这就是 OCR 的默认字段表。

按 type 分别探针是必须的，因为同名字段在不同 type 下默认值可能不同。

探针失败的 type（典型：NeuralNetworkClassifier / NeuralNetworkDetector 这类需要外接模型文件的，构造空 task 会加载失败）自然形成**黑名单**——白名单是探针成功的 type 集合，黑名单是失败的。剥离时只对白名单 type 动手，黑名单 type 的字段整段保留（宁可冗余，不可错删）。

### 5.2 双重判定剥离

剥离的朴素逻辑是"字段值 == 默认值 → 删"。但这在卸载端会出错。

考虑：base 写了 `post_delay: 5000`，用户在工作区改成 `post_delay: 200`（200 恰好是框架默认值）。朴素逻辑会把 `post_delay: 200` 当默认值剥掉——但用户是**有意**改成 200 的，剥掉后 mod 不写，重新挂载时 base 的 5000 又回来了，用户的修改丢失。

**双重判定**：字段要被剥，必须同时满足

1. 字段值 == 该 type 的默认值
2. base 对应字段值 == 默认值（即 base 也没改过这个字段）

两条都成立，才说明"这个字段在 mod 里写不写都一样"，可以剥。

实现上，`strip_mod_with_def()` 卸载端接收 `canonical_base` 参数。挂载端不传——因为挂载写的是工作区，工作区独立加载，缺失字段用框架默认值补齐，base 是什么不影响。

**MOD_ONLY 的特例**（V0.7.5 修复）：如果一个 task 在 base 中根本不存在（用户新建的），双重判定的"base 对应字段"取不到值。此时应退化为朴素逻辑——base 没这个 task，等价于 base 全用默认值，按默认值剥即可。否则会因为"base 字段全是 None，永远不等于默认值"导致整个新建 task 一个字段都剥不掉。

### 5.3 路 D 递归子字段 diff

MODIFIED task 里，`recognition` / `action` 等是嵌套 dict。朴素的"字段值 != base → 整个写"会导致：用户只改了 `recognition.param.threshold`，结果整个 `recognition` 对象（含十几个没动的子字段）都被写进 mod。

路 D（`deep_diff`）递归进入嵌套 dict，逐子字段和 base 对比。只有真正变了的子字段进 minimal_mod，没动的剥掉。这是 V3 能做到"字段级精度"的关键，也是 V2 最大的失败点（V2 做不到这层递归）。

### 5.4 extras 黑白名单合并

挑战：怎么判断一个字段是 MaaFramework 字段还是 extras（如 doc/desc）？

方案：构造"MaaFramework 字段全集"。它 = 硬编码已知集（含 V1 拍平时散在顶层的 param 字段名、探针失败 type 的字段名）∪ 动态探针表（task_top / 各 type param / wait_freezes）。任何 task 字段不在这个全集里，就是 extras。

这样设计的好处：MaaFramework 升级新增字段时，动态探针会自动捕获，不会误把新字段当 extras。硬编码部分只是兜底（探针失败 type、V1 独有形态）。

extras 收集对 base 多层 + mod 做覆盖式合并（mod 优先），sub-node 内的 extras 也递归处理。

### 5.5 节点顺序持久化

oracle 输出的 canonical，task 顺序由 dumper 内部决定（非 base 原序）。直接写出去会打乱 base 的"叙事流"，git diff 也乱。

挂载时记录 base 各文件中 task 的出现顺序到 `extras.json` 的 `node_order` 字段。写工作区/写 mod 时按这个顺序排：base 中出现过的 task 按 base 顺序，新建 task 按字母序排在文件末尾。

注意：这里说的是**节点之间**的顺序（task 在文件里谁先谁后），不是节点内字段的顺序。字段顺序不管（编辑器有自己的排序）。

---

## 6. .maaowm 状态目录

挂载期间，适配包目录下会有一个 `.maaowm/` 目录存放状态：

```
.maaowm/
├── snapshot.json        挂载时的 canonical_base + 指纹 (diff 被减数)
├── origin.json          task → 原文件路径 索引 (写回时用)
├── def_tables.json      探针出的各 type 默认字段表
├── extras.json          非 MaaFW 字段 (doc/desc) + 节点顺序
└── <timestamp>/         备份目录 (时间戳命名)
    ├── mod/             挂载前的 mod 包原貌
    └── work/            卸载前的工作区原貌
```

卸载完成后，前四个状态文件被清理，备份目录保留。`is_mounted()` 通过检测 `snapshot.json` 是否存在来判断挂载状态。

误操作时，可从 `<timestamp>/` 备份目录手动恢复。

---

## 7. Bug 修复史

记录 V3 开发中发现并解决的关键问题。详细复现脚本见仓库的 issue 文档和 git 历史。

### 7.1 dumper sub_recognition 字段错位

**现象**：含 And/Or 的 task，PipelineDumper 输出的 sub-recognition 把 `type`/`param` 放在 sub 顶层，但 PipelineParser 期望它们嵌套在 `recognition` 子对象里。dumper 的输出 parser 自己加载不了，round-trip 断裂。

**根因**：MaaFramework 的 dumper 与 parser 对 sub_recognition 形态的约定不一致（框架侧 bug）。

**对策**：`core/fixup.py` 在 oracle 内部把 dumper 输出的 sub_recognition 重新包装成 parser 接受的嵌套形态。已向 MaaFramework 提 issue。

### 7.2 ColorMatch 空数组

**现象**：ColorMatch type 下用户没填 `lower`/`upper` 时，dumper 输出 `lower: []`，但 parser 拒绝接受空数组（字段不存在 → 用默认值 OK；字段存在但是空数组 → 报错）。又是 dumper 输出自己 parser 不接受。

**对策**：按 type 的 def 剥离正好覆盖这个场景——`lower: []` 等于默认值会被剥掉，剥掉后 parser 走"字段不存在用默认"分支。已向 MaaFramework 提 issue。

### 7.3 def 剥离破坏路 D 成果（双重判定的由来）

**现象**：用户把 base 设过的 `wait_freezes.time: 3000` 重置为 `0`（0 是默认值）。路 D 正确判定 time 字段与 base 不同应保留，但紧接着的 def 剥离层看到 `time == 默认值 0` 又把它剥了，用户的重置丢失。

**根因**：def 剥离层和路 D 各自独立运作，def 剥离不知道路 D 的判定。

**对策**：引入双重判定（见 [5.2](#52-双重判定剥离)）。def 剥离卸载端接收 `canonical_base`，字段值 == 默认 **且** base 也 == 默认才剥。

### 7.4 MOD_ONLY task 剥不掉 def 字段

**现象**：用户新建的 task（base 没有），卸载后 mod 产物含大量默认值字段（enabled/inverse/max_hit/post_delay 等十几个），没被剥离。

**根因**：双重判定在 base 不含该 task 时，"base 对应字段"全取到 None，永远不等于默认值，导致该剥的都不剥。

**对策**：MOD_ONLY task 退化为朴素剥离逻辑——base 没这个 task 等价于 base 全用默认值。

### 7.5 numpy 跨 Python 版本不兼容

**现象**：用系统 Python 运行 MaaOWM，但 `maa_pkg_dir` 指向项目虚拟环境的 maa 包，maa 内部 `import numpy` 触发 C 扩展加载失败。

**根因**：不是 MaaOWM 的 bug，是运行环境问题——maa 装在虚拟环境（某个 Python 版本），却用了不同版本的 Python 来跑。

**对策**：`core/env_check.py` 在挂载/卸载/检查前预检，识别这类问题并给出友好诊断和精准的修复命令。不越权自动处理，让开发者自己决策。

### 其他较小的修复

- 空 mod 目录导致 oracle 加载失败 → 检测空 mod 跳过加载
- `routing.write_mod_files` 曾强制字母序排序，覆盖了 node_order → 去掉强制排序，信任上游
- 单独修改 doc/desc 不触发 mod 写回 → `extras.diff_extras` 检测 extras 变化，强制相关 task 入 mod
- 探针的 stderr 噪音 → 进程级缓存避免重复探针

---

## 8. 版本演进

```
V3.0   oracle-based 基础架构 — fixup / snapshot / routing / 路 D
V3.1   def 剥离 — 按 type 探针 + 动态白名单/黑名单
V3.2   V1 输出格式 + 探针进程级缓存
V3.3   preflight 卸载预检 + [C] 检查菜单
V3.4   next/on_error 紧凑写法 + [V]/[N] 格式开关
V3.5   workspace minimal 化 — def 剥离也用于工作区, 体积大幅缩减
V3.6   V1 子嵌套递归拍平 — And/Or 的 sub-recognition 也 V1 化
V3.7   extras (doc/desc) + 节点顺序持久化
V3.7.2 extras diff — 单独改 doc/desc 也能写回 mod
V3.7.3 双重判定剥离 + wait_freezes 紧凑写法
V3.7.4 env_check — maa 环境友好诊断
V3.7.5 MOD_ONLY task 剥离修复
```

每一步都是被实际问题驱动的，不是预先规划的路线图。V3 整体方法论：设计先讨论清楚再写代码（V2 的教训），每个改动配自检，重要假设用 verify 脚本实证而非推理。

---

## 9. 已知限制与未来方向

### 当前限制

- **只处理 pipeline**：image / model 仅 passthrough，不做 diff。
- **强依赖 maafw 绑定**：没有 MaaFramework Python 绑定的环境无法运行。
- **同时只能挂载一个适配包**：多适配包项目需切换 `target` 重新挂载。
- **挂载会清空适配包 pipeline 目录**：有备份兜底，但需要开发者理解这个行为。
- **依赖 dumper 输出质量**：dumper 出新 bug 时可能需要扩展 fixup。

### 可能的未来方向

- image / model 的 hash 级 diff（V2 曾有，V3 暂时砍掉）
- 多适配包批量操作
- 节点级删除的更智能处理（当前只警告）
- 打包成 pip 包分发（当前是 git clone 使用）

---

## 10. 给接手者的话

### 怎么跑测试

每个 `core/` 模块都有自检，直接运行即可：

```bash
cd maaowm-v3
python -m core.def_table      # 17 个剥离 case
python -m core.translator     # V1/V2 + 紧凑写法 case
python -m core.extras         # extras 收集/注入/diff case
python -m core.env_check      # venv 识别 case
# ... 其他模块同理
```

仓库还有若干 `verify_*.py` 脚本，是开发过程中用来实证关键假设的（如 `verify_workspace_minimal_v2.py` 验证激进剥离的 round-trip 闭合性）。这些需要一个真实的 base pipeline 目录作参数。

### 改代码的注意事项

- **不要重新实现 MaaFramework 的语义**。这是 V2 的死因。任何"判断字段该不该这样"的问题，答案应该来自 oracle，不是来自你写的规则。
- **def 剥离的双重判定不能简化**。挂载端不传 base、卸载端传 base、MOD_ONLY 退化——这三种情况各有原因（见 [5.2](#52-双重判定剥离)），看起来啰嗦但都是踩过坑的。
- **extras 收集必须在 canonicalize 之前**。oracle 不输出 doc/desc，一旦 canonicalize 就丢了。
- **改剥离逻辑后跑 verify 脚本**。剥离是否安全（round-trip 是否闭合）要用真实数据实证，不能靠推理。

### MaaFramework 升级时要重新验证什么

- **重新探 def 表**：升级可能改默认值。def 表是动态探针的，理论上自动跟随，但要确认探针没失败。
- **检查 dumper 输出**：升级可能修了旧 bug（fixup 可以精简），也可能引入新 bug（fixup 需要扩展）。跑 `verify` 系列脚本看 round-trip 是否还闭合。
- **新增的 type**：如果 MaaFramework 加了新的 recognition/action type，探针会自动尝试，但 `extras.py` 里硬编码的字段集兜底部分可能需要补。

### 设计的精神

V3 的核心不是某个算法，是**承认自己不是权威，把权威让给 MaaFramework**。MaaOWM 做的事情是：组织数据流、在 oracle 周围做编排、处理 oracle 不管的东西（extras、节点顺序、文件路由）。一旦想"自己判断 pipeline 该怎样"，就走回了 V2 的老路。

---

*本文档随 MaaOWM V3 维护。最后更新对应版本 V3.7.5。*
