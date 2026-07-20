# 设计提案：base 基线漂移检测（base'）

> 状态：**已落地（V3.7.9）**。实现：`core/baseline.py`、`core/gitinfo.py`、`core/config.py`（maaowm/ 布局）、`core/inplace.py`（挂载摘要）、`overlay_tool.py`（[R] 面板）。本文为设计依据，与实现保持同步。
> 关联：解决 `ARCHITECTURE.md` §9「节点级删除的更智能处理」之外的另一类盲点——
> base 自 mod 创建以来漂移、pc 该不该跟随却无人察觉。
> 已落地的 type 漂移护栏（§7.6）是本提案的「轻量前身」。

---

## 1. 问题与定性

pc mod 本质是 base 的一个 fork / patch，base 是上游。这是 **vendoring / patch 跟踪上游变更** 的经典问题。

核心事实：**没有任何工具能自动判断「该不该跟随」——那是意图，不是数据。** 工具能做到的上限是：**报告漂移点，把「难以发现」变成「清单可见」，交人 review。**

两类漂移的难度天差地别：

- **type 漂移**（已解决，§7.6）：mod 写 `OCR`、base 是 `And`——本身就是异常信号，挂载时当场可判，无需历史基线。
- **普通参数漂移**（本提案）：mod 写 `threshold:0.5`、base 是 `0.3`——这俩不同是**正常的**（override 的定义就是不一样）。无法判断 `0.5` 是「有意特化」还是「基于旧 base 的过时残留」。

要区分后者，唯一办法是知道「我当初写 `0.5` 时，base 是多少」：

- 当初 base 也是 `0.3` → `0.5` 是有意特化，安全。
- 当初 base 是 `0.5`（我照抄 base）→ base 现在改成 `0.3`，而我还固定 `0.5` → **疑似过时残留，要 review**。

→ 必须有一个跨挂载周期、持久的「mod 所基于的 base 基线」，记为 **base'**。

---

## 2. 核心机制：base' 是「手动 ack 的基准」，不是「自动刷新的快照」

这是整个设计的灵魂，区别于现有 `snapshot.json`：

| | `snapshot.json` | **base'** |
|---|---|---|
| 生命周期 | 单次挂载周期内，卸载即删 | 持久，**只有手动重置才变** |
| 版本控制 | `.gitignore` 排除 | **被 git 跟踪，多人共享** |
| 用途 | 卸载 diff 的被减数 | 漂移检测的基准锚点 |

**为什么不能自动刷新**：若 base' 随卸载自动更新成当前 base，那「还没处理的漂移」会被悄悄抹掉，警报自己消失，又退回「难以发现」。

**手动 ack 的效果**：base 一旦偏离 base'，**每次挂载都报漂移，直到人主动确认并重置 base'**。这把漂移从「一闪而过的提示」变成「持久待办信号」。base' 进 git → 谁 ack 了、基准推进到哪，团队共享，外部 CI 监控降级为可选。

---

## 3. base' 的形态与存放

### 3.1 形态：base 源文件原档快照（**不是 canonical**）

直觉会想存 canonical（diff 直接字段级跑），但 canonical 是 maa dump 的产物，**绑定 maa 版本**。多人协作下：A 用 maa v1.0 生成、B 用 v1.1 挂载，diff 会冒出一堆「maa 版本差异」假阳性。

→ **base' = `base/pipeline` 源文件的原档副本**。检测时（见 §5）用**当前同一个 maa**，同时 canonicalize「base' 原档」与「当前 base」，再 diff——maa 版本变量被消掉。canonical 始终是运行时即时产物，**从不持久化**。

**多 base 层**：`overlay_config.json` 的 `base_layers` 是数组（mod 叠加的是多层合并后的有效 base）。故 base' 须**每层各存一份原档**（目录树见 §3.2），检测时 canonicalize「所有 baseline 层的合并」对「所有当前层的合并」——与运行时多层 overlay 语义一致。

**附带锚点 `_anchor.json`**：每次复位时同步记录 base 当时的 git commit、时间、操作人。用途：(1) §8.1 的自证——「这份原档声称对齐到 commit X」可被校验；(2) 报告里告诉人「你上次把基准推进到哪个 base 版本」。锚点是**元数据**，不参与 diff（diff 只比 canonical 后的 pipeline 内容）。

### 3.2 存放：被管理项目根的统一 `maaowm/` 目录

**现状问题**：`owm_dir = target.parent/.maaowm`，即 `assets/resource/.maaowm`——**物理上在 resource 内**，落在 MaaFramework 打包枚举范围里（靠 `.gitignore` 排除版本控制，但位置不干净）。

**方案**：在**被管理项目（如 MFABD2）的根目录**建一个统一的 `maaowm/`，收拢所有 owm 文件，按是否进 git 分两类（本地类用点前缀目录，共享类用普通名目录）：

```
<被管理项目根>/maaowm/
├── .state/        本地, 整个目录 .gitignore (= 现在 .maaowm 的内容)
│   ├── snapshot.json / origin.json / def_tables.json / extras.json
│   └── <timestamp>/   备份
├── .gitignore     首建 maaowm/ 时自动写入, 内容 = .state/
└── baseline/      git 跟踪, 多人共享
    └── <branch-slug>/      按当前分支隔离 (无 git 时退化为单一 flat, 见 §3.3)
        ├── _anchor.json    复位时 base 的 git commit / 时间 / 操作人 + 原始分支名
        └── <layer-name>/   每个 base_layer 一份原档 (单层即一个目录)
            └── pipeline/   base' 原档 (该层 base/pipeline 的副本)
```

硬约束（均满足）：

1. **共享类被 git 跟踪**（`baseline/` 进版本库，多人共享）；本地类 `.state/` 进 `.gitignore`。**`.gitignore` 由工具首建 `maaowm/` 时自动写入**（内容 `.state/`），不依赖人手改根 `.gitignore`。
2. **不在任何 MaaFramework/oracle 加载的 pipeline 路径下**——`maaowm/` 在项目根，不在 resource 内，挂载时不会被误当 base/mod 加载。
3. **脱离打包**——MaaFramework 打包枚举 `assets/resource` 下的适配包，项目根的 `maaowm/` 天然在外，排除规则只需排一个目录。

> 澄清：「进 git 的文件」指仓库工作区里被 `git add` 的普通文件，**不是** `.git/` 内部目录。

**为何放项目根，而非 resource 内 / `Devtools/` / `scripts/`**：

- `Devtools/`（前端小工具堆）、`scripts/`（changelog/git 脚本）语义都不搭 owm 的结构化工作流状态。
- owm 是**项目级 overlay 工作流基础设施**（决定整个 resource 怎么被开发），不是一次性脚本，配得上一级目录。
- 一级目录向 clone 者**宣告「本项目用 owm 管理 overlay」**——否则有人直接手改 resource 会与工作流冲突。

**为何在被管理项目、而非 owm 工具仓库**：base' 必须与它快照的 base 在**同一 git 仓库**才能多人共享。

**定位 + 不自动迁移**：`config.owm_dir` 从 `target.parent/.maaowm` 改为定位到项目根 `maaowm/.state/`（由 `overlay_config.json` 显式 `maaowm_dir`，否则用 `git -C <target> rev-parse --show-toplevel`，再否则 target 向上找含 `assets` 的目录）。**工具直接用新布局，不写迁移代码**：旧 `assets/resource/.maaowm/` 留给人手处理。理由：`.state/` 内容只在挂载期存在，卸载态升级无损，断层影响小。`baseline/` 为新增。

### 3.3 多分支：按当前分支隔离的本地护栏

base' 是「本分支本地护栏」，不是跨分支对齐账本（定性见 §8.1）。落地按当前 git 分支隔离到 `baseline/<branch-slug>/`。

**前提**：

- 集成 commit 锚点（§3.1）即默认依赖 git；分支名由 `git rev-parse --abbrev-ref HEAD` 取。
- **无 git** → 无多分支需求 → 退化为单一 flat baseline（不分分支目录），功能照常。
- **有 git 但环境找不到 git 可执行**（罕见）→ 退化为 flat + **一次性提示，不阻断挂载**。baseline 是护栏，不该因找不到 git 卡死主流程。
- **detached HEAD / 无分支名** → 同退化为 flat（或以短 SHA 命名），不报错。

**分支名落地为目录名须消毒**：`feature/x`、`origin/feature/x`（远程/PR）含 `/`，Windows 还禁 `: * ?` 等。→ 分支名 **slug 化**（可逆编码，`/`→`__`）作目录名，**原始分支名存入 `_anchor.json`**，隔离文件系统差异。

**生命周期形态**：

1. **单分支续开发**：护栏名 = 当前分支名，直接命中。
2. **fork 子分支**：子分支只继承到父分支的护栏目录（fork 点 base 未变，该副本**有效**）→ **提示后改名 adopt**（rename 父→子）。**改名而非重建**——重建会丢掉一份有效基准。提示，因为在动 git 共享文件。
3. **反向合并后清理**：合并会把对方分支的护栏目录一并带进当前分支，目录下出现「非当前分支名」的护栏 → 视为冗余清理，抵消 per-branch 的堆积代价。
   ⚠ **但 baseline 是 git 共享文件，删除可能波及仍存活的分支**（下次合并把删除传过去）。故清理须带**安全栏**：仅当该分支在 `git branch -a` 已不存在（孤儿/已删）时才删；**存活的并行分支护栏不碰；删除前提示**。绝不静默删共享文件——同 §6「方便之门」铁律。

---

## 4. 生命周期：初始化 = 首次复位，统一入口 [R]，按文件粒度复位

base' 的产生**显式、可追溯**，不在挂载时偷偷自动生成（否则首次挂载的人会莫名其妙多出一坨进 git 的文件）。

- **无 base'**：挂载 / 进入 [R] 时检测到没有基准 → 提示「尚未建立基准」，首次 [R] 建立，**不自动建**。
- **[R] 漂移面板 + 复位**：见 §6。复位 = 把当前 base 某文件的原档写入 base'，声明「该文件的漂移我已确认处理」。
- **按文件粒度复位（关键）**：base' 是按文件存的原档，复位只覆盖对应文件，**不要求一次清空所有漂移**——否则差异越堆越高、逼人一次处理完才能消警，反而越堆越没人处理。处理一个文件就复位一个，警报按文件逐步收敛。
- **挂载**：若有 base'，即时双 canonicalize 比对，产出漂移报告（§5、§6）。

→ 初始化 = 首次复位，与复位同一入口；复位粒度 = 文件。

---

## 5. 检测算法

```
1. 加载 base'（原档, 各层） 与 当前 base（原档, 各层）
2. 用当前 maa 分别 canonicalize 多层合并 → cano_baseline, cano_now   ← 消除版本假阳性
3a. 文件级 diff: 比对两侧源文件相对路径集 →
     - 文件增: 仅当前 base 有
     - 文件删: 仅 base' 有
3b. 节点级 diff(cano_baseline, cano_now) → base 漂移集:
     - 改: 节点存在两侧, 某字段值变了
     - 增: 节点仅在 cano_now
     - 删: 节点仅在 cano_baseline
4. 加载 mod, 求出 mod 实际 override 的「节点.字段」集合
5. 标注每条漂移与 mod override 的关系:
     - 改 ∩ mod override 了同字段 → ★需 review（疑似过时残留 / 或有意特化）
     - 改 但 mod 没 override 该字段 → 自动跟随（挂载后工作区已是新值），仅info
     - 增 → 工作区自动可见，info
     - 删 ∩ mod 仍 override → ★悬空 override（base 已无此节点）
```

复用现有 `oracle` / `deep_diff` 基础设施，不重造比对逻辑。文件级（3a）与节点级（3b）漂移用同一套报告呈现（§6）。

**锚点不参与判定**：检测全程**不读** `_anchor.json`——漂移只看 canonical 后的 pipeline 内容。`_anchor.json` 仅供报告展示「上次基准推进到哪个 base 版本」与 §8.1 自证，与 diff 结果无关。

---

## 6. 报告 UI：[R] 漂移面板（只读，绝不动文件）

**铁律：报告纯只读，绝不修改任何文件。不做「一键跟随」之类的自动改写。** 理由：挂载后工作区本就乱，自动改写极易出错；一旦开「方便之门」，难保证用户不被带沟里。报告只给**参考**建议文本，实际修复一律回工作区 / 编辑器手动完成。

### 6.1 单一菜单：概览 → 下钻（GitHub PR 心智）

「轻度概览」与「按文件复位入口」是同一批信息的两种用途 → 合并成**一个菜单**，不用开关切换。层级对标 GitHub PR 的「文件列表 → 点开看 diff」：

```
[R] base 漂移面板
 ── 顶部：一句话说明为啥有这功能 + "详见文档"（见 6.3）──
 ── 文件列表（= 轻度概览）──
   ▸ Battle.json   3 项漂移 (2 需review)    [回车·详细diff]  [C 复位本文件]
   ▸ Map.json      1 项漂移 (1 悬空)        [回车·详细diff]  [C 复位本文件]
 共 N 文件有漂移
 ↓ 回车进入某文件详情
 ── 重度报告（PR diff 风格，见 6.2）──
   看完 → 就地 [C 复位本文件]
```

为何进入详情而非开关：用户心智是**导航**（先看全貌 → 对感兴趣的展开看细节 → 就地处理），不是模式切换。进入详情天然把「按文件复位」放在它该在的位置，选择权已细粒度交给用户，无需额外全局开关。

### 6.2 重度报告：PR diff 风格（文件 → 节点 → 节点内 +/-）

```
Battle.json
  ▾ QuickHunt_NoAP_Rice            [改 · mod override 了 threshold → 需 review]
      recognition.param.threshold
        - 0.5     (base' 基准)
        + 0.3     (当前 base，已改)
        · 0.5     (mod override 现值 = 旧 base 值 → 疑似过时残留)
      参考：base 已下调阈值；若 pc 无特殊理由，考虑跟随（回工作区改 mod / 删该 override）
  ▾ OldNode                        [删 · mod 仍 override → 悬空]
        - 整节点已从 base 移除
      参考：确认 pc 是否还需要它；不需要则在工作区删除该 mod 节点
```

要素：`-` base' 旧值 / `+` 当前 base 新值 / `·` mod override 现值 + 一句**参考**建议。**配色对标 PR diff：`-` 红、`+` 绿，`·` 中性色**，让人一眼分清「基准/新值/我的覆盖」。字段多时可用表格呈现。**只显示，不操作文件。**

### 6.3 [R] 顶部引导文案（草案）

> base 可能已偏离你做这份 mod 时的基准。下面按文件列出 base 的变动，帮你判断 pc 是否需要跟随；标「需 review」的是你 mod 改过、base 也改过的字段。
> 「复位本文件」= 你已确认处理完该文件，将其基准推进到当前 base。
> 原理与完整设计见 `DESIGN-base-baseline.md`。

---

## 7. 「变更」：纯手动，工作区是天然场所

不做自动 apply / rebase。理由：

- base **改**字段：跟不跟随是 case-by-case 意图，只能人定。
- base **增**节点：挂载后工作区（= base⊕mod 合并视图）里它自动就出现了，无需「合并」。
- base **删**节点：挂载后那个 mod 节点变 MOD_ONLY 悬空，开发者决定删不删。

检测负责「指出哪里要看」，开发者在工作区改，卸载得到新 mod，[R] 按文件复位 base' 确认完成。闭环不需要独立的「自动变更」机制。

---

## 8. 边界与风险

- **maa 版本假阳性**：由 §5 双 canonicalize（统一用当前 maa）消除。
- **base' 误加载**：由 §3.2 存放约束（独立于 pipeline 加载路径）规避。
- **首次无基准**：§4，提示建立，不自动生成。
- **多人改 base' 冲突**：base' 是普通 git 文件，并发修改走正常 git 合并；语义上「谁 ack 谁 commit」，冲突即两人对基准推进有分歧，需人工对齐——这是期望行为，不是 bug。
- **base' 与 base 大小**：base' 是 base/pipeline 副本，git delta 压缩友好，体积可接受。
- **找不到 git 可执行**：§3.3，降级为 flat baseline + 一次性提示，不阻断挂载。
- **分支名含文件系统非法字符**：§3.3，slug 化作目录名，原名存 `_anchor.json`。
- **跨分支误删 baseline**：§3.3 清理须经「分支已不存在」判定 + 提示，不静默删共享文件。

### 8.1 多分支 / 合并下的完整性：根本局限与职责边界

**辨析（保留以备后人）**：

- 招 1（commit 锚点自证）：自证只比「副本 vs 它声称的 commit」，副本没被动就自洽——**合并使 base 前进、baseline 没动时，自证通过却已脱节，漏报**。
- 招 2（per-branch 路径隔离）：堵住「合并互相覆盖 baseline」，但合并只把 base 的 json 带进来时，base 前进、本分支 baseline 不动 → **仍脱节**，且喷出分不清来源的噪音。

**定性**：`base/pipeline` 是多分支共同推进的共享路径；baseline 是单点 per-branch 快照。「mod 对齐到 base 哪个状态」在多分支并发下**不是一个点，是「哪些 base 变更已被哪个分支 ack」的偏序**——这正是 git 用 merge-base / 三方合并解决的问题。在 baseline 上继续加固 = **重新发明 git 的合并追踪，且更差**，撞 `ARCHITECTURE.md` 红线「不要重新实现已有系统的语义」（V2 死因）。

**职责边界（结论，收手不再加防御）**：

1. baseline = **本分支本地护栏**，只检测「本分支自复位以来 base 变没变」。**不做**跨分支全局对齐账本；跨分支正确性交还 git + code review + 人。
2. 优先级 **不漏报 > 不噪音**：脱节宁可多报（按文件复位快速消化），绝不悄悄漏。
3. **不为多分支追踪无限加固**：per-branch 隔离是「愿付堆积代价换合并不互相覆盖」的形态，§3.3 self-clean 抵消堆积；二者都**不假装**能解决合并追踪。
4. **合并与修改一视同仁**：不特判 merge。合并让 base 变了，就当 base 变了——让护栏报漂移、人 review、复位。这是机制**正常工作**，而非要被特殊处理的难题。

---

## 9. 不做什么

- 不自动判断「该不该跟随」（意图，非数据）。
- 不自动 apply base 变更到 mod。
- **报告绝不修改任何文件**——不做「一键跟随」之类自动改写（§6 铁律）。
- 不持久化 canonical（运行时即时产物）。
- 不把 base' 放进 pipeline 加载路径或本地 `maaowm/.state/`（base' 属共享的 `maaowm/baseline/`）。
