# Changelog

本文档记录 MaaOWM 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

V3 是相对 V2 的彻底重写，不再尝试在外部重新实现 MaaFramework 的合并语义，
而是把 MaaFramework 自己的 PipelineParser + PipelineDumper 当作 oracle 调用。
详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## [0.7.6] — 文档完善

### Changed
- 重写 HELP_TEXT (TUI 内 [H] 帮助), 按实操流程组织
- 删除过期描述 ("V3 永远 V2 输出"、"sub-object 整段写入" 等已不准确的)
- 所有行控制在 72 字符以内, 兼容 Windows 中文终端
- README.md 加 ".gitignore 提醒" 小节, 引导用户在资源项目里忽略 `.maaowm/`

### Added
- ARCHITECTURE.md 深度技术文档 (~440 行, 含设计哲学/数据流/模块清单/Bug 修复史)
- CHANGELOG.md (本文件)
- .gitignore (MaaOWM 仓库自身)
- overlay_config.example.json 模板

---

## [0.7.5] — MOD_ONLY task 剥离修复

### Fixed
- 用户新建的 task (base 不含) 卸载时, 默认值字段没被剥离, mod 产物含大量
  `enabled: true` / `inverse: false` / `post_delay: 200` 等冗余字段。
- 根因: V0.7.3 引入双重判定时, base 不含该 task 的场景下, base 对应字段
  全取到 None, 双重判定的"base 也是 def 值"条件永远不成立。
- 修复: `strip_mod_with_def` 内引入 `use_base_compare` 局部判定。
  base_task 不存在时退化为单纯 def 剥离 (等价于 V0.6.x 行为)。
- 自检 case 17 覆盖此场景。

---

## [0.7.4] — 环境预检

### Added
- `core/env_check.py` 模块。挂载/卸载/检查前预检 maa 环境可用性。
- 失败时给环境信息 (Python 版本/路径、maa 路径) + 常见原因 + 识别到虚拟
  环境时附精准的"用该环境 Python 运行"命令。
- 不在启动时跑预检 (避免 maa 加载失败时连 TUI 都进不去)。
- venv 识别正则: `.venv` / `venv` / `env` / `.env` / `virtualenv`,
  大小写不敏感, 验证 `Scripts/python.exe` 或 `bin/python` 存在。

### Background
- 实战触发: 用户用系统 Python 3.11 运行 OWM, 但 maa_pkg_dir 指向 .venv
  里 Python 3.10 装的 maa, numpy C 扩展版本不匹配崩溃。
- 设计决策: 不越权自动重启或切环境, 让开发者自己决策, 仅展示信息+精准命令。

---

## [0.7.3] — 双重判定剥离 + wait_freezes 紧凑

### Fixed
- 修复一个潜在 bug (从 V0.6.0 起一直存在):
  base 改过非默认值的字段, 用户在工作区改成默认值想还原, 卸载时被 def 剥离
  误剥掉, mod 不写, 重新挂载后 base 的非默认值又回来, 用户修改丢失。
- 修复: `strip_mod_with_def` 卸载端接收 `canonical_base` 参数, 启用双重判定:
  字段值 == def 默认 **且** base 同字段也 == def 默认, 才剥离。
- 影响范围超出 wait_freezes —— 任何 base 改过 def 值的字段都受益:
  顶层标量 (post_delay 等) / recognition.param / action.param / attach.

### Added
- `translator.simplify_wait_freezes_in_pipeline`: 仅含 `time` 一个字段的
  wait_freezes 退化为标量 `3000` 形态 (parser 支持的紧凑写法)。
- 自检扩到 16 case (新增 13-16 覆盖双重判定的各场景)。

---

## [0.7.2] — extras diff

### Fixed
- 用户仅修改 doc/desc 字段时, oracle 看 task IDENTICAL, 不进 minimal_mod,
  导致 doc 改动无法写回 mod。
- 修复: `extras.diff_extras()` 对比工作区和挂载时 extras, 找出变化 task,
  强制加进 minimal_mod (即使 oracle 看 IDENTICAL), 后续 inject_extras
  会把新 doc 注入到产物。

### Semantics
- 整字段删 doc → 视为"撤回修改", mod 不写, 重挂载从 base 恢复。
- 写 `doc: ""` → 视为修改, mod 显式写入空字符串。
- 这套语义和用户的"删字段不应被强制持久化"直觉一致。

---

## [0.7.1] — routing 顺序修复

### Fixed
- `routing.write_mod_files` 强制按字母序排序 task, 覆盖了上游 extras 的
  node_order 重排。
- 修复: write_mod_files 信任上游传入的 dict 顺序 (Python 3.7+ 保序),
  不再二次排序。

---

## [0.7.0] — extras (doc/desc) 与节点顺序

### Added
- `core/extras.py` 新模块。处理 MaaFramework 不识别的字段 (doc/desc 等)。
- 挂载时扫 base + mod 原始 JSON, 按层覆盖式合并, 收集 extras + 节点顺序,
  存到 `.maaowm/extras.json`。
- 写工作区/写 mod 前注入 extras, 按 base 原始节点顺序重排。
- sub-node (And/Or 内部) 的 extras 也递归处理。
- 字段判定: MaaFramework 字段全集 = 硬编码已知集 ∪ 动态探针表。
  不在全集里的字段视为 extras。

### Changed
- 工作区根目录 `__OWM_README__.md` 更新, 解释 doc/desc 编辑行为。

---

## [0.6.2] — V1 子嵌套递归拍平

### Fixed
- V1 输出模式下, And/Or 的 sub-recognition 没有递归拍平, 外层是 V1 但
  内层仍是 V2 形态, 视觉不一致。
- 修复: `translator._sub_v2_to_v1()` 递归处理 sub-node, 把 sub 内的
  `recognition: {type, param}` 拍平到 sub 顶层。
- parser 验证支持此形态 (PipelineParser 调同一个 parse_recognition)。

---

## [0.6.1] — 激进 def 剥离

### Added
- def 剥离扩展 3 条规则:
  - 顶层标量字段按 task_top def 剥 (enabled/inverse/max_hit/post_delay 等)
  - And/Or 的 sub-recognition 数组递归剥
  - And 的 `box_index == 0` 删

### Changed
- 工作区从 V0.6.0 的 "部分剥离" 进化到 "激进剥离":
  剥离字段数从约 45k → 59k (实测 base/PC), 体积缩减率 58.2% → 71.4%。
- `verify_workspace_minimal_v2.py` 脚本预先实证 round-trip 闭合后再实施。

---

## [0.6.0] — workspace minimal 化

### Added
- 挂载写工作区时, 在 def 剥离之后才写, 让工作区接近 base 简洁形态。
- 之前的 V3.5 仅在卸载端做 def 剥离, 工作区是全字段; 现在统一两端行为。
- README 文案更新, 强调"工作区里没写的字段 ≠ base 的值, 可能是框架默认值"。

### Verified
- `verify_workspace_minimal.py` 脚本实证 round-trip 完全闭合
  (1364 task / 45238 def 字段剥离 / 体积 -58.2%)。

---

## [0.5.x] — 双开关 + preflight

### Added
- V0.5.0: `output_format` 配置开关, V2 (嵌套, 默认) ↔ V1 (拍平)。
  TUI [V] 切换 (仅未挂载时)。
- V0.5.1: `compact_node_refs` 开关, 默认开启。next/on_error 用紧凑字符串
  (含 `[JumpBack]` / `[Anchor]` 前缀语法), 而非 `{name: "X"}` dict 形态。
  TUI [N] 切换。
- V0.4.x: `core/preflight.py`。卸载前自动跑 dry-run 验证工作区可加载,
  失败时拒绝执行卸载, 报错位置。TUI [C] 主动检查菜单。

---

## [0.3.x] — V1 输出 + 探针缓存

### Added
- V1 输出格式支持。MaaPipelineEditor 风格的字段拍平形态。
- def 探针进程级缓存 `_def_tables_cache`, 避免一次会话内重复探针, 减少 stderr 噪音。

---

## [0.2.x] — def 剥离

### Added
- `core/def_table.py`。对每个 recognition/action type 主动探针出默认字段表。
- 探针失败的 type (NeuralNetworkClassifier 等需要外接模型的) 自动进黑名单。
- mod 产物中"值等于默认值"的字段自动剥离, 大幅减少冗余。

### Background
- ColorMatch `lower: []` / `upper: []` 等 dumper 输出形态触发 parser 拒绝
  加载, def 剥离恰好覆盖此场景 (剥后 parser 走"字段不存在用默认"分支)。

---

## [0.1.x] — V3 基础架构

### Added
- 整套 V3 架构:
  - `core/oracle.py` — MaaFramework Python 绑定封装
  - `core/fixup.py` — 修补 dumper 输出 (sub_recognition 字段错位 bug)
  - `core/snapshot.py` — 挂载快照 (canonical_base 持久化)
  - `core/routing.py` — task → 原文件路径索引
  - `core/diff.py` + `core/deep_diff.py` — 语义 diff + 路 D 递归子字段
  - `core/inplace.py` — mount/unmount 主流程
  - `overlay_tool.py` — Rich TUI 入口

### Philosophy
- 不再重新实现 MaaFramework 合并语义 (V2 的死因)。
- 信任 MaaFramework 自己的 PipelineParser + PipelineDumper 作 oracle。
- 字段级 diff (而非 V2 的节点级), 配合 deep_diff 实现真正的最小 mod。

---

## V2 (已废弃)

V2 实现路径: 自己写 merge / diff 算法。已知问题:

- diff 出来总是整个节点, 没动过的 JSON 被替换成空对象
- 跟不上 MaaFramework 字段细节变化
- 节点级精度, 无法做字段级最小化

V2 不再维护, V3 是完全重写。
