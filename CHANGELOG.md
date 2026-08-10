# Changelog

本文档记录 MaaOWM 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

V3 是相对 V2 的彻底重写，不再尝试在外部重新实现 MaaFramework 的合并语义，
而是把 MaaFramework 自己的 PipelineParser + PipelineDumper 当作 oracle 调用。
详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## [0.7.13] — 双重判定推进到 type 层

### Fixed
- **V1「默认 type 省略」吞掉"改回默认 type"的 override**（ARCHITECTURE 7.10,
  实例 MFABD2 `Event_GoinEvent`）: base 是 `recognition: And` + `action: Click`
  + `target: [1118,387]`, 在挂载态工作区把 `action` 改成 `DoNothing`（意图是
  只识别不点, base 那个坐标已成盲点, 改由下游 `_OcrCk` 真点）。卸载后适配包里
  该节点**没有 action**, 运行时 [base, pc] 合并沿用 base 的 `Click`, 盲点照点
  不误。反复改反复丢 —— 每次挂载工作区都显示回 `Click`。
  同一条路径上 `recognition` 从 `And`/`OCR` 改成 `DirectHit` 也会整段消失。
- 根因是 `translator.task_v2_to_v1()` 的 V1 输出规则 3「type 是默认值且 param
  为空 → 整段省略」。它依赖的等价关系「V1 省略 recognition/action ≡ 该字段取
  框架默认值」只对**独立完整节点**成立; 本工具的两个产物（卸载的 mod delta、
  挂载的挂载态工作区）都是 overlay 的一层, 层里省略的语义是"不覆盖"而非
  "取默认值", base 的非默认 type 会透过字段级合并盖回。
  与 7.3 / 7.6 / 7.7 同族的第四例: 双重判定做到了 **param 层**, 漏了 **type 层**
  —— 且做判定的 `def_table` 与执行省略的 `translator` 分离, 后者手上没有 base。
- **隐蔽变体**: mod 里原本手写着 `"action": "DoNothing"` 时, 一次 mount/unmount
  空转（用户一个字没改）就能把它永久抹掉 —— 挂载省略进不了工作区, 卸载 diff
  出来又被省略。
- 修复: `task_v2_to_v1(base_task=)` / `pipeline_v2_to_v1(canonical_base=)` 接收
  base 对照, 省略需同时满足「当前 type == 默认 且 base 同字段 type 也是默认
  （或 base 无此 task / 无此字段）」。挂载卸载两端都传。

### Changed
- `def_table` 侧**未动**。丢信息的是 V1 输出这一步, 不是剥离 —— 剥掉
  `DoNothing` 的空 `param` 本身不损语义, 剥完只剩 `{"type":"DoNothing"}` 正好
  命中 translator 的省略规则。在剥离侧打补丁只会掩盖真正的边界。
- `_sub_v2_to_v1()`（And/Or 内联 sub）**不需要** base 对照: 列表字段整段进 mod
  （`deep_diff` 对 list 不递归）, 每个 sub 都是完整节点, 省略语义成立。

### Verified
- MaaFW 5.11.1 `canonicalize_overlay` 实测三种 mod 形态（base = `And` +
  `Click[1118,387]`）: mod 省略 action → 合并得 `Click` + `target
  [1118,387,1,1]`（bug 的运行时后果坐实）; V1 写 `"DoNothing"` → 合并得
  `DoNothing` + `param {}`（修复产物有效）; V2 写 `{"type":"DoNothing"}` →
  同上（"V2 不受影响"成立）。
- 附带查明: reco/action **换 type 时 MaaFW 整体替换**, param 按新 type 重置,
  不与 base 旧 type 的 param 做 dict-merge。故 mod 只写一个 type 名就够, 无需
  连带清理 base 的参数字段。

### Compatibility
- 不传对照时退化为旧行为, 纯格式转换的直调不受影响; 16 个原有 translator
  自检 case 全绿。
- 状态文件格式全部不变（snapshot / def_tables / origin / extras）, 可安全回滚。
- **存量适配包需人工回看**: 已经被吞掉的 `action` / `recognition` 在产物里不留
  痕迹（表现为"这个节点没覆盖该字段"）, 工具无法反查区分"本来就没打算覆盖"和
  "被吞了"。修复只保证此后不再发生。已知一例: MFABD2
  `assets/resource/pc/pipeline/EventBattle.json` 的 `Event_GoinEvent` 缺
  `"action": "DoNothing"`。

---

## [0.7.12] — 落点第三级：工作区新建节点写回原文件

### Fixed
- **工作区新建的节点被搬进 `__mod_extras__.json`**（ARCHITECTURE 7.9, 实例
  MFABD2 `feat/EventBattle` 的 `Event_GoinEvent_OcrCk`）: 在挂载态工作区某个
  json 里新建节点, 卸载后它不在原文件, 而在适配包新生成的
  `__mod_extras__.json` 里, 原文件只剩一句 `next` 指向它。根因是
  `decide_target_file()` 只查 `mod_origin` / `base_origin` 两张**挂载时快照**
  表, 工作区新建的节点两张都命中不了 → 落进硬编码兜底文件名。
- **粘死**（同一 bug 的第二层）: 兜底文件生成后就躺在适配包里, 下次挂载扫
  mod 包建 `mod_origin` 时命中它, 而 `mod_origin` 优先级最高 —— 一旦落进去,
  在工作区手动搬回会被下次卸载搬走, 在产物上手动搬回会被下次挂载搬走。
- 修复: `decide_target_file()` 增加第三级 `workspace_origin`, 数据来自卸载时
  `oracle.list_node_names_with_origin(工作区)` 现扫的 `{task: 文件相对路径}`。
  纯 JSON 扫描、不依赖 def 表, 对 `canonical_w` 无条件全覆盖。

### Removed
- **`EXTRAS_FILENAME` 常量与兜底分支整体删除**。三级全 miss 改抛
  `routing.RoutingError` —— 理论上不可达（挂载端 `canonical_merged ⊆ base ∪ mod`
  前两级必然命中; 卸载端 `minimal_mod ⊆ canonical_w` 必被第三级覆盖）。
  宁可炸掉也不再发明兜底文件: 上一个兜底文件就是这么来的。
- **适配包目录内不再生成任何 owm 文件。** 资源包只装能被 MaaFramework 加载的
  东西, owm 自己的记账一律留在 `maaowm/` 下。

### Changed
- `_clean_pipeline_dir()` 从"分组之前"移到"分组之后、落盘之前"（挂载/卸载
  两端一致）。落点分组是写盘流水线上唯一会抛错的一步, 旧顺序抛错会留下一个
  空的 pipeline 目录, 只剩备份可救; 现在异常都停在"目录原样未动"的状态。
- 三级顺序是"旧归属优先于工作区现状": 在挂载态**跨文件搬动已有节点仍会被
  撤销**, 有意保留 —— mod 的文件划分继续镜像 base。只有工作区新建的节点
  （前两级必然 miss）才走第三级。

### Compatibility
- 不写迁移代码。修复后不再产生 `__mod_extras__.json`, 存量由使用者删一次即可
  （删之前先把里面的节点搬回它们该在的文件）。**未删干净时该文件仍会被
  `mod_origin` 命中而继续粘住**, 这是选择不做迁移的已知代价。
- 状态文件格式全部不变（snapshot / def_tables / origin / extras）, 可安全
  回滚。`OriginIndex` 序列化结构未动 —— `workspace_origin` 现扫现用, 不落盘。
- 资源项目侧: 若曾在 `.gitignore` 里忽略 `**/__mod_extras__.json`, 应当移除
  该行。它装的是真实可运行的节点定义, 忽略掉等于提交出一个 `next` 指向不存在
  节点的断链资源包。（已核实: MaaFramework 5.11.1 **会**正常加载 `_` / `__`
  前缀的 json, 只跳过路径含 `.` 开头组件的文件 —— 所以本地跑得好好的。）

---

## [0.7.11] — extras 层归属 + V1 输出保真

### Fixed
- **卸载注入把 base 层 desc 写进 mod**（ARCHITECTURE 7.8, 实例
  `Arbitrage_Buy_Select_QE5`）: 挂载把 base 的 desc 灌进工作区（设计意图,
  全字段视图）, 卸载注入只按「task 在不在 minimal_mod」过滤, 不辨字段层
  归属 → base 内容污染 mod。修复: `extras.json` 增记 `base_extras`
  （mod 合并前的 base-only 快照）, 卸载注入前 `subtract_base_extras`
  逐字段过滤——值 == base 同字段值归 base 不写 mod; 不同或 base 无则保留。
  sub-node extras 按下标同规则。
- 语义说明: mod 作者显式抄写的与 base 同值 desc 也会被清（minimal 哲学）;
  用户把 desc 改回 base 值 = 撤回 override, 该 task 若因此变空 `{}` 会
  从 mod 剔除（仅限因 extras 变更被强制入 mod 的 task）。

### Changed
- **V1 输出解包单元素目标数组**: MaaFW 5.10 dumper 把 Swipe `end` 规范成
  `[[x,y,w,h]]`（begin 仍是平坦单目标, 不对称）。`task_v2_to_v1` 在 V1
  输出端把 len==1 的目标数组拍回 `[x,y,w,h]`; 真·多段（len>1）不动;
  MultiSwipe 的 `swipes[i].end` 同规则。canonical 层不动（oracle 哲学）,
  再次 canonicalize 会被重新包裹, diff 恒在 canonical 层, 无幻影 diff。
- **sub_name 永远保留**: 移除「sub_name == recognition.type 时主动删除」
  规则（原依赖 parser 回填）。主动删除用户写下的内容违背最小侵入。
  `verify_workspace_minimal_v2.py` 的同款规则同步移除。

### Compatibility
- 旧 `extras.json`（无 `base_extras` 键）→ 过滤优雅退化为 0.7.9 行为,
  不崩不变形。挂载于旧版、卸载于新版时层归属过滤该轮不生效（fix2/fix3
  正常生效, 无状态依赖）; 建议「卸载 → 升级 → 重挂载」。
- 新 `extras.json` 被旧版读: 未知键被忽略, 可安全回滚。
  snapshot.json / def_tables.json / origin.json 格式均不变。

---

## [0.7.10] — 挂载端双重判定

### Fixed
- 挂载端 def 剥离改传 `canonical_base`, 与卸载端对齐双重判定（ARCHITECTURE
  7.7）。base 非默认、mod 覆盖成恰好等于框架默认值的字段不再被裸剥,
  overlay 合并不再让 base 值盖回（实例 `Arbitrage_Card5#_Goin` threshold）。
- OWM_README_TEXT 文案同步纠正"工作区独立加载"表述。

---

## [0.7.9] — base 基线漂移检测 (base')

### Added
- 持久、手动 ack、进 git 的 base 基线（`core/baseline.py`）, 按 git 分支
  隔离（`core/gitinfo.py`）; 双 canonicalize 消版本假阳性; 挂载发一行摘要,
  TUI [R] 只读漂移面板。详见 DESIGN-base-baseline.md。
- owm 文件统一到项目根 `maaowm/`（`.state/` 本地 + `baseline/` 共享）,
  owm_dir 重指向 `maaowm/.state/`。

---

## [0.7.8] — base type 漂移修复 + 挂载护栏

### Fixed
- base reco/action type 改变时双重判定失效, 新 type 的默认字段剥不掉
  （与 0.7.5 MOD_ONLY 同构）→ type 不一致时退化朴素剥离（ARCHITECTURE 7.6）。

### Added
- `detect_type_drift` 挂载护栏: mod override 盖住 base 新 type 时发警告。

---

## [0.7.7] — custom param 原子保护

### Fixed
- `custom_action_param` / `custom_recognition_param` 注册进
  `_ATOMIC_DICT_KEYS`, 阻止 deep_diff 递归剥离其与 base 相同的子字段。
  MaaFW 对它们是整体替换而非 dict-merge, 剥离会导致运行时缺失必要参数。

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
