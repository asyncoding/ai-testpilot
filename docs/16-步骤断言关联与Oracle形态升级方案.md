# 16 - 步骤断言关联与 Oracle 形态升级方案（C3 设计稿）

> 状态：**P0+P1 已落地（2026-11-15，schema v15 零 DDL）**。触发来源：串案修复（提交 `6c93b38`）
> 活库重灌验收后的四个追问——oracle 与步骤无锚点、审批页看不到步骤、知识型 bullet 混入
> 断言、外部 Agent 拿到平行罗列的步骤/断言后只能猜对应关系。用户拍板：**先出设计文档再排实现**，
> 随后拍板 P0+P1 合并一次到位、越界步骤引用走死信 fail-loud（见 §8 决策落定）。
> 本稿承接 docs/08 FR-TD-003/004、docs/11 审批页、docs/12 导出闭环 v1.4，不改其已定边界。
>
> **交付注记（2026-11-15 P0+P1 切片）**：`Oracle/MinedOracle.step_id` 模型字段、解析器确定性
> 锚定（§4.2 全部规则，含歧义多引用死信）、回写标签 `【类型｜步骤N】` round-trip（§4.6）、
> 挖掘透传与模型 step_id 白名单（§4.4）、审批载荷 steps/preconditions 与 Web 步骤分组（§4.5）
> 已全部实现；红证 22 条（`tests/test_step_anchor.py`，五组定点变异回退必红已取证），
> 全量 1472 通过、import-lint 通过、build_web 9 路由通过。P2（§4.3 知识分流）与
> P3（§4.7 导出 v2/§4.8 日志链拆分/§4.9 语义去重）未动；v1 导出为显式白名单 dict，
> step_id 不外泄，v1 契约形状本切片零变化。
>
> **交付注记（2026-11-16 C2/P2 知识分流切片）**：`TestCase.context_notes`（零 DDL）、
> `classify_bullet_role` 保守词表与两阶段分流（入口段首标记 + build 期全文复核）、
> 审批载荷 context_notes 与 Web「背景与平台约束」只读区块已落地（实现细节见 §4.3 注记）；
> 红证 16 条（`tests/test_context_notes.py`，五组定点变异回退必红已取证），全量
> **1488 通过**、import-lint/build_web 通过。真实 V1 语料实测：66 断言→63 断言 + 3 背景
> （TC-A1 两条、TC-A1b 一条机制夹注），零死信。导出 v2 携出仍随 P3 的 v2 契约一起做，
> v1 白名单继续零泄漏（有红证锁死）。

## 1. 问题与证据

| # | 现象（活库 ios-local-poc-xcode，2026-11-15 只读取证） | 根因层 |
|---|---|---|
| P1 | TC-A1 名下 9 条建议扁平罗列，其中「真机实锤两条平台约束」「iOS 26.4 不投递 LA .ended」是背景知识而非可判真假的用例断言 | 解析器把「预期」段全部 bullet 收为 oracle，无断言/知识分流 |
| P2 | 仅 1 条 bullet 自带「步骤 3 后」字样，其余断言无法对应到 4 个步骤中的哪一步；`Oracle` 模型只有 type/expectation/source_refs/confidence，**无步骤字段** | 模型层与 V1 源文档都没有步骤锚点（源文档「预期」本身是扁平列表） |
| P3 | 一条 log_assertion 的 expectation 内嵌约 10 个有序日志检查点（KEEPALIVE→SYS-LA→TAKEOVER→…→120s hard cap），粒度过粗，外部 Agent 无法逐条落断言 | 解析期嵌套子弹全部拼接续行，无检查点拆分 |
| P4 | 审批 API `ReviewQueueItem` 不携 steps/preconditions（test_cases.raw_json 里有数据），审批页无步骤区 | API 载荷缺口，零 DDL 可补 |
| P5 | ollama 活模型为 TC-A1b 两次运行产出 6/8 条不同草稿，含语义近重复对（「解锁后…可见且可点击」vs「解锁屏幕后…依然可见且可点击」），文本归一化去重抓不住 | 模型侧无语义去重 |

## 2. 目标与非目标

**目标**

1. oracle 可携带 1-based 步骤锚点（`step_id`），全链路（解析→挖掘→审批→回写→导出→外部 Agent prompt）无损传递；
2. 知识型材料与可判定断言分流，断言列表保持纯净；
3. 审批页展示步骤/前置，并按步骤分组呈现断言；
4. 导出契约升 v2，外部 Agent 拿到「步骤 N → 断言集」的对齐结构。

**非目标（本期不做）**

- 不改 schema 版本号（**v15 零 DDL 红线不变**，新字段全部走 pydantic 模型 + raw_json 投影）；
- 不用 LLM 推断步骤锚点（锚定只走确定性规则，拿不准恒为用例级，杜绝猜测型错锚）；
- 不在系统内生成 Swift（docs/12 v1.4 边界不变）；
- 不重算 docs/10 盲评冻结封存 seal，新指标按既定纪律另存双列。

## 3. 现状链路与改动触点

```
V1 Markdown
  └─ testcase.py 解析（叙事体 _NarrativeBuilder / 表格体 _parse_table_block）
       TestCase{steps[], oracles[Oracle]}            ← 触点 A：模型 + 解析
       upsert_test_case：投影列 + raw_json 全量 dump  ← 零 DDL 依据（新字段自动入库）
  └─ oracle_mining（core 四源 + workers 检索）         ← 触点 B：URN 决策、模型 prompt/白名单
       oracle_suggestions（raw_json 存 MinedOracle）
  └─ approval.py build_review_queue → 审批 API → Web  ← 触点 C：载荷 + 页面分组（吸收 C1）
  └─ render_case_markdown 回写 → case_repo 落 Markdown ← 触点 D：锚点 round-trip 语法
  └─ case_export.py（case-export-v1）→ 外部 Agent      ← 触点 E：契约 v2 + 附录 A prompt
```

## 4. 核心设计

### 4.1 模型：Oracle 增 step_id，TestCase 增 context_notes（零 DDL）

```python
class Oracle(BaseModel):
    type: OracleType
    expectation: str
    source_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    step_id: int | None = None        # 新增：1-based，对齐 steps 序号；None=用例级（全程）断言
```

```python
class TestCase(BaseModel):
    ...
    context_notes: list[str] = Field(default_factory=list)  # 新增：知识/平台约束，非判定性材料
```

- 持久化无需迁移：`upsert_test_case` 以 `model_dump_json()` 全量写 raw_json，投影列
  （step_count/oracle_count 等）不变；老数据反序列化新字段取默认值 None/[]。
- **step_id 取值域**：`None` 或 `1..len(steps)`；越界即非法载荷（见 4.4 白名单）。
- 选型说明：知识材料独立成 `context_notes` 而不是给 Oracle 加 role 枚举——断言列表保持
  单一职责，exploratory 判定、导出分流、外部 Agent 资格判断都不必再过滤「伪 oracle」。

### 4.2 解析器锚定规则（确定性、保守，零模型参与）

叙事体「预期」段每个顶层 bullet 产出一条 oracle，锚点按以下优先级：

1. **显式步骤引用**：文本匹配 `步骤\s*N` / `Step\s*N`（N 为阿拉伯数字）→ `step_id=N`。
   - `1 ≤ N ≤ len(steps)` 正常锚定；**越界不静默**：step_id 置 None 并产 `CaseParseError`
     死信（沿用单条失败整批继续的 AC2 纪律），死信原因写明越界值与步骤总数。
2. **嵌套子弹**：继承所属顶层 bullet 的 step_id。
3. **事件相对描述**（T0、侧边键后、解锁后、亮屏瞬间…）：一律 `None`。事件不是稳定序号，
   硬映射就是猜测；用例级断言在导出时翻译为步骤间注释，不丢失语义。
4. **表格体**：单预期格无步骤结构，恒 `None`（表格 steps 本身也只有 0/1 条）。
5. 历史无锚点文档：行为与今天完全一致（全 None），解析器现有 13 条测试保持全绿。

### 4.3 知识型 bullet 分流（吸收 C2）——✅ 2026-11-16 已落地

新增纯函数 `classify_bullet_role(text) -> Literal["assertion","context"]`，**默认 assertion**，
仅命中确定性保守词表时归 context（宁可漏分流，不可误杀判定性断言）：

- 词表候选（首版，红证夹具驱动增减）：段首「平台约束」「真机实锤」「历史」「实测记录」，
  以及纯机制说明句式（「iOS x.y 不投递…」「仅为其他版本/出口保留」）。
- `context_notes` 不进 oracle_suggestions、不参与 exploratory 判定、不进导出 oracles[]，
  但随 case-export-v2 的 `context_notes[]` 携出，供外部 Agent 理解约束（明确标注不可作为断言依据）。
- 红证：TC-A1 同形夹具中两条知识 bullet 必须入 context_notes，7 条真断言数量与类型不减少；
  词表外 bullet 在词表收紧/放宽对照下不得漂移。

> **实现注记（2026-11-16，活库取证驱动的三处增补）**
> 1. 「历史」段首加了护栏：必须冒号或四位年份紧随（`历史：`/`历史 2026…`），
>    防「历史告警记录页面应展示…」这类真断言被误分流。
> 2. 新词形——句首括号机制夹注：`（2026-11-14 按代码现文案核对；机制见 X.y(…)）：`
>    （TC-A1b 活样本，含 `setHaptics(active:false)` 等嵌套半角括号）。三端齐备才命中：
>    句首括号 + 关键词（机制见/按代码现文案核对/代码现文案）+ 句尾括号（可选冒号）；
>    「日志（…按代码现文案核对…」以日志开头且不以括号收尾，仍是真断言。
> 3. **两阶段分流**：机制夹注在真档里跨三个物理行，入口期（`_is_context_entry`）
>    只按段首标记分桶以保证嵌套子项随父汇聚，`build()` 对拼接完整文本再跑全量
>    `classify_bullet_role` 复核，命中者从断言桶移到背景桶，其步骤锚点与锚点死信一并撤销。
> 4. 人审回写显式标签 `【类型…】` 是权威断言，任何词表不得覆盖（有红证）。
> 5. Web 区块明示「仅供理解，不可作为断言依据，不进自动化导出」；v1 导出白名单
>    不含 context_notes 键，泄漏即红（`test_v1_export_whitelist_never_leaks_context_notes`）。

### 4.4 挖掘链路：URN 稳定、模型白名单加一步校验

- **suggestion_urn 保持 `hash(type|expectation)` 不含 step_id**（明确决策）：锚点修订不换
  URN，已审批/已回写记录不产生孤儿，重灌幂等性不受锚点迭代影响。
- 确定性三源：自有断言携带解析期 step_id；requirement_ac 与 incident 源默认 `step_id=None`
  （AC/事故句子没有步骤序号语义）。
- 模型补点（仅 oracle_missing 用例）：
  - `oracle_json_schema()` 的 items 增可选 `step_id`（null 或整数）；
  - prompt 铁律加一条：**仅当白名单用例步骤文本与断言存在显式对应时给 step_id，否则 null，
    禁止按事件描述猜测序号**；
  - `apply_model_oracles` 白名单拒入增校验：非 null 且越界 → `RejectedOracle(reason="bad_shape")`
    进 dead_letters（source=oracle-mine），与现有幻觉台账同路径。
- R2/R3 既有纪律不变：平移仍仅对 oracle_missing 开放，来源用例 URN 强制追加。

### 4.5 审批 API 与 Web（吸收 C1）

- `ReviewQueueItem` 增 `steps: list[str]`、`preconditions: list[str]`（store 行转视图时
  从 test_cases.raw_json 取，零 SQL 改动）；`SuggestionView.oracle.step_id` 随 MinedOracle 自然带出。
- 卡片渲染：顶部新增「前置/步骤」区块；建议列表按 `step_id` 分组——
  `用例级（全程）` 组 + `步骤 1…N` 组，组内沿用现有来源/置信度/「平移自」徽章。
- 前端无单测的现状不变，门禁仍是 `scripts/build_web.sh`；分组逻辑抽纯函数（同
  `transferCaseIds` 模式），便于人工走查。

### 4.6 Markdown 回写 round-trip

扩展显式标签语法（`split_explicit_type` 同源解析，无标签旧文档行为不变）：

```markdown
- 【UI 断言｜步骤3】系统全屏闹钟消失，锁屏上没有补救通知
- 【日志断言】T0 见 KEEPALIVE armed ……        ← 无锚点=用例级，写法与今天兼容
```

- 全角 `｜` 为分隔符（与现有中文标签风格一致），半角 `|` 同步接受；
- `render_case_markdown` 仅在 `step_id is not None` 时输出锚点段，保证旧字节风格稳定；
- 红证：渲染→解析 round-trip 后 step_id/类型/文本三字段无损（对齐现有
  `test_render_round_trips…` 矩阵扩展）。

### 4.7 导出契约 case-export-v2 与外部 Agent prompt

- 新增 `CASE_EXPORT_SCHEMA_VERSION = "case-export-v2"`：oracle 对象增 `step_id`，
  case 对象增 `context_notes[]`；v1 保留一个发布周期，worker 增 `--contract v1|v2`
  （默认 v2），v1 输出剥离新字段，旧消费方不破。
- 导出内容 hash 随字段变化是预期行为（内容寻址文件名本就不覆盖旧文件）。
- docs/12 附录 A prompt 模板升 v2 版（新模板与 v1 并存留档）：
  - 通用规则 3/4 改为「断言按 `step_id` 分组，方法内 `// Step N` 注释下只写该组断言；
    step_id=null 的写入 `// Case-level` 段」；
  - 新增铁律：`context_notes` 只能进生成报告的 constraints 段，**禁止翻译成 XCTAssert**；
  - 资格/分流/静态红线（GS-001/002/003）规则不变。

### 4.8 长日志链拆分（P3，可选）

TC-A1 型超长 log_assertion 拆为逐检查点 oracle 的规则留待三期：仅对「反引号大写日志 token
序列 + 箭头连接」的强结构句式机械拆分，自由散文不拆；拆分后共享同一 step_id、source_refs
追加同一文档 URN。需要先在 V1 全量日志型断言上做拆分前后盲评对照再定阈值，本稿只占位。

### 4.9 模型语义去重（C4，P3）

同用例同类型的模型草稿，过一遍既有 embed 适配器（dmeta-embedding-zh）余弦相似度，
阈值（建议 0.92 起步）必须在冻结夹具上标定后写入 params 版本；超阈保留先产出条目、
被合并条进审计台账不静默丢弃。无 embed 适配器的 profile 自动跳过（降级不阻断）。

## 5. 兼容性、迁移与评测纪律

1. **零 DDL**：模型新字段随 raw_json 落库；schema 版本仍 v15，老库直接打开不报错。
2. **重灌路径**：`testpilot-ingest-cases` 重解析 → 文档 content hash 变化 → oracle 挖掘
   增量机制自然触发整文档重建（既有 doc-hash 纪律，无需新机制）；审批库当前全 pending，
   无决策损失面，未来有 approved 数据后 step_id 回填应走兼容重灌而非 --full 清扫。
   > **2026-11-16 实测注记**：解析规则变更而源文档未变时，content hash 不变，增量模式
   > 整文档跳过（`case_ingestion.py` hash 对账 `skipped_unchanged`），新规则不会执行——
   > 活库曾因此出现 59 条断言 step_id 全 NULL。此类「规则升级重灌」必须显式
   > `testpilot-ingest-cases --full` 强制整组重建，再 `testpilot-mine-oracles --full`。
3. **盲评封存**：docs/10 三个 seal 与冻结指标永不重算；新解析规则跑 W1/W2 冻结夹具的结果
   按「冻结列 / 新规则列」双列入新报告（同 L3 回流先例）。
4. **回写兼容**：旧回写 Markdown（无锚点标签）重新摄入得全 None，行为等同今天。

## 6. 红证测试矩阵（hostless，全部要求回退必红）

| 层 | 用例要点 |
|---|---|
| 解析器 | 显式「步骤 N」锚定/嵌套继承/越界死信/事件描述恒 None/表格体恒 None/旧文档全 None 不回归 |
| 知识分流 | TC-A1 同形夹具：2 条 context、7 条断言不减少；词表外文本不漂移（✅ 2026-11-16 落地，16 红证） |
| round-trip | 带锚点标签渲染→解析三字段无损；无锚点旧字节稳定 |
| 挖掘 | step_id 随自有断言进 suggestion；URN 不随锚点变化；模型越界 step_id 进 dead_letters |
| 审批视图 | 载荷含 steps/preconditions；分组纯函数对 None/越界/乱序的确定性输出 |
| 导出 | v2 字段齐全且 hash 变化；`--contract v1` 输出无新字段；context_notes 不进 oracles[] |
| Web | build_web.sh 通过；页面人工走查清单（无前端单测现状不变） |

## 7. 分期切片建议

| 期 | 范围 | 预估 | 依赖 |
|---|---|---|---|
| P0（=C1） | 审批载荷+页面 steps/preconditions 展示（不依赖 step_id，先解决「看不见」） | 小，零 DDL | 无 |
| P1（C3 主体） | Oracle.step_id 模型字段 + 解析锚定 + 回写 round-trip + 挖掘透传 + 模型白名单 | 中 | P0 后分组渲染顺势接上 |
| P2（=C2） | context_notes 分流 + 导出 v2 携出 + Web/审批断言列表净化（✅ 分流与 Web 净化 2026-11-16 落地；导出 v2 携出随 P3 v2 契约） | 中 | P1 |
| P3 | case-export-v2 切换 + 附录 A prompt v2；语义去重（C4）；长日志链拆分评审 | 中 | P2；日志链拆分需先做盲评对照 |

每期独立提交、显式路径、全量 pytest + import-lint + build_web 三门禁；schema 全程 v15。

## 8. 待用户决策的开放问题（2026-11-15 已全部拍板）

1. 越界步骤引用的处置：**死信 fail-loud（已采纳推荐）**——`CaseParseError` 留痕、step_id
   置 None、整批继续；模型侧越界走 `RejectedOracle(bad_shape)` 同路径台账。实现中另把
   「同条断言引用多个不同步骤号」按同口径保守处理为歧义死信。
2. 回写标签分隔符：全角 `｜` 为规范写法，**半角 `|` 同步接受**（解析器正则两者兼容），
   渲染输出只产生全角。
3. P 期顺序：**P0+P1 合并为一个切片一次到位（已交付）**。
4. C4 语义去重的阈值标定是否接受引入一次 embed 离线实验（仅跑冻结夹具，不碰活库）？
   ——随 P3 启动时再确认，本期不实施。
