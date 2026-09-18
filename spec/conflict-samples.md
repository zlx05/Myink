# Myink 冲突样例集 — Phase 0（39 例）

> **用途**：评测集（plan.md §16）。点级 10 例测 L1/L2 点校验（plan.md §8.4），长线级 5 例测长线一致性治理（plan.md §8.6），§7.7-7.9 记忆/图谱/伏笔线 12 例（样例 16-27）测人物状态台账 / 关系动态 / 伏笔 / 剧情线的一致性（§19.1 项 2），点/长线阴性对照 5 例（样例 28-32）测误报边界，§7.8 关系台账自洽 3 例（样例 33-35，2026-08-12 新增）测关系台账结构不变量，§8.6 人设漂移误报边界 1 例（样例 36，2026-08-12 新增，样例 12 的阴性对照）、桥段重复 L2 呼应判定 1 例（样例 37，2026-08-13 新增，样例 32 的 L2 阴性对照——无词表标记的刻意呼应）、文风漂移 L2 抽样比对 2 例（样例 38/39，2026-08-13 新增：样例 38 全书风格漂移阳性 + 样例 39 场景节奏合法变化阴性对照）。任何优化跑同一套样例 + 同一 seed / prompt 模板 / 模型，用**检出率 / 误报率**度量，不挑成功案例。每例标注**检测现状**：已落地（代码可检出，见 test_flow.py / test_debt_checks.py / test_state_relation_checks.py）/ 待落地（机制已建模或已规划，阶段 3 长线治理主体落地）。新增样例统一标注 **阳性（应检出）/ 阴性（应不检出，误报控制）**。
>
> **世界观基线**：附录 A《九州问天》——林砚（主角，筑基后期，叛出天衡宗）、五方势力、境界 炼气→筑基→金丹→元婴→化神→大乘→渡劫、战力 1–100（筑基上限 20）、地域禁制不可瞬移。
>
> **格式**：前置（前情）→ 冲突片段 → 预期检出（L1/L2 / 机制）→ 预期 Finding → 误报控制要点。阴性样例：前置 → 合法片段 → 预期**不检出** → 误报控制要点。**度量口径**：阳性算检出率分子、阴性算误报率分子（阴性被误报 = 误报 +1），两组都跑才算有效指标。

---

## A. 点级样例（10 例）

### 样例 1 · 战力越界
- **前置**：林砚 = 筑基后期，战力上限 20；对手 = 元婴初期。
- **冲突片段**：`林砚一掌拍出，筑基后期的修为猛然爆发，竟将眼前那位元婴初期的黑脸修士打退三步。那修士捂着胸口，惊骇道："你……你是元婴不成？！"林砚收掌，冷冷道："筑基又如何。"`
- **预期检出**：L1 → `power`（战力越界，超境界上限 20）
- **预期 Finding**：`conflict_type=power, severity=critical, scope=structural`
- **误报控制**：若剧情含"秘法越级输出"，需有"燃烧寿命/反噬"代价说明（附录 A.1）才算合法——无代价说明即冲突。

### 样例 2 · 阵营敌对
- **前置**：林砚与天衡宗敌对（叛出弟子）；relations 无临时盟约记录。
- **冲突片段**：`夜色中，林砚与天衡宗执法弟子并肩而立，共抗万魔渊魔修。那执法弟子拍了拍他肩头："林师弟，你我本是同门。"`
- **预期检出**：L1 → `faction`（敌我关系矛盾，无解除/盟约记录）
- **预期 Finding**：`conflict_type=faction, severity=critical, scope=structural`
- **误报控制**：若剧情有意"暂时联手"，必须先在 relations 落一条临时盟约（带 `valid_to`），否则按无记录即冲突处理。
- **检测现状**：✅ **已落地（2026-08-13，L1 确定性，补漏切片）**——活跃 hostile 关系双名（含别名）共现于本章 draft + 任一协作标记（`并肩而立/并肩作战/共抗/携手/联手/化敌为友/握手言和/把酒言和`）→ `faction/critical/structural`；守卫：临时盟约覆盖本章（非 hostile 行带 `valid_to` 落在本章，或永久非敌对行）→ 跳过（0 误报，样例 40 阴性）；每章至多 1 条。见 [l1.py](../src/myink/validation/l1.py) `faction_check` / [test_conflict_sample_suite.py](../tests/test_conflict_sample_suite.py)。

### 样例 3 · 时间线（死而复生）
- **前置**：第 8 章事件记录"北境城守将秦虎战死"；无复活机制设定。
- **冲突片段**：第 12 章 `秦虎的身影出现在灵兽谷入口，拱手道："林小友，别来无恙。"`
- **预期检出**：L1 → `timeline` / `character`（生死状态冲突，`alive` 台账被违）
- **预期 Finding**：`conflict_type=character, severity=critical, scope=structural`
- **误报控制**：若引入"还魂丹"等复活机制，需先落 facts 规则，否则冲突成立。

### 样例 4 · 地点移动
- **前置**：东海水宫与荒原部隔全九州，禁制结界不可瞬移、需传送阵。
- **冲突片段**：`清晨林砚还在东海水宫与蛟王对饮，黄昏已站在荒原部的图腾祭坛前，衣袍猎猎，不见传送阵的痕迹。`
- **预期检出**：L1 → `location`（两地路径不可能性）
- **预期 Finding**：`conflict_type=location, severity=major, scope=local`
- **误报控制**：若写明"借传送阵/灵舟"，则合法；只查"无传送说明却跨域"。

### 样例 5 · 人物身份（首次来访矛盾）
- **前置**：第 8 章事件记录"林砚在北境城完成交易"。
- **冲突片段**：`林砚抬眸，望着眼前高耸的城墙，心道：这就是北境城，第一次来。`
- **预期检出**：L1 → `character`（经历冲突，事件表已有记录）
- **预期 Finding**：`conflict_type=character, severity=major, scope=local`
- **误报控制**：仅当事件表确有此记录且无"失忆/重游"说明时冲突。

### 样例 6 · 物品/规则（已毁再现）
- **前置**：第 10 章事件记录"定水珠被天劫劈碎"。
- **冲突片段**：`林砚从怀中摸出定水珠，幽蓝光晕流转，映得满室皆蓝。`
- **预期检出**：L1 → `item_rule`（已毁物品再现，无修复说明）
- **预期 Finding**：`conflict_type=item_rule, severity=critical, scope=local`
- **误报控制**：若写明"寻回残片、以重宝重铸"，则需 facts 记录，否则冲突。

### 样例 7 · 伏笔/承诺（未兑现当已兑现）
- **前置**：第 3 章伏笔"林砚承诺三年之内必取万魔渊魔主首级"，状态 `planted`，`last_touched=3`。
- **冲突片段**：第 10 章 `"魔主之仇，我已报了。"林砚淡淡开口，仿佛这是一件早已了结的小事。`
- **预期检出**：L1 → `foreshadow`（承诺未兑现却被当作已兑现；正文无复仇情节，伏笔仍 `planted`）
- **预期 Finding**：`conflict_type=foreshadow, severity=major, scope=local`
- **误报控制**：若正文确有复仇情节并落 `resolved`，则不冲突——这是"收伏笔"校验（plan.md §7.9）。

### 样例 8 · 能力规则（越级用秘法）
- **前置**：境界体系"化神期才可动用碎星诀"（facts 规则）；林砚 = 金丹期。
- **冲突片段**：`他不过金丹期，竟施展出化神期才可动用的"碎星诀"，满天星辉坠落，而他却毫发无伤，无半分反噬。`
- **预期检出**：L1 → `power`（能力前置条件未满足 + 无代价说明）
- **预期 Finding**：`conflict_type=power, severity=critical, scope=local`
- **误报控制**：规则类能力使用校验（plan.md §8.4 第 8 类）。

### 样例 9 · 势力归属
- **前置**：林砚叛出天衡宗（faction 关系：敌）。
- **冲突片段**：`林砚拱手："在下天衡宗执法弟子，奉命巡游各州。"`
- **预期检出**：L1 → `faction`（势力归属矛盾）
- **预期 Finding**：`conflict_type=faction, severity=major, scope=local`
- **误报控制**：若剧情含"伪装身份"（身份伪装是 `character_states` 的 `identity` 字段），需台账有伪装记录，否则冲突。

### 样例 10 · 境界突破（跳级）
- **前置**：境界体系"不可越级突破，需逐境晋升"（附录 A.1 最大硬约束）。
- **冲突片段**：`一道灵光冲天，林砚直接从筑基跃入元婴，省去了金丹的层层淬炼。`
- **预期检出**：L1 → `power`（跳过金丹，违反逐境晋升）
- **预期 Finding**：`conflict_type=power, severity=critical, scope=structural`

---

## B. 长线级样例（5 例，plan.md §8.6）

> 长线样例测**跨章累积**问题，无法靠单章点校验检出，靠 per-角色战力曲线 / 抽样 L2 审计 / 偏差比对 / 向量近邻。

### 样例 11 · 战力通胀
- **前置**：第 1 章林砚 = 筑基中期；第 2 章金丹；第 3 章直接元婴——每章跳一大境，无突破契机（金丹需丹方、元婴需心魔关）、无修炼铺垫。
- **冲突片段**：`第1章：筑基中期。第2章：金丹初成。第3章：元婴降临，天雷滚滚。`（境界序列）
- **预期检出**：L1 战力曲线（**斜率异常**：数章内实力翻数番无铺垫 + 配角集体通胀监测）
- **预期 Finding**：`conflict_type=power, severity=major, scope=structural`（走周期审计）
- **度量指标**：通胀检出率 / 误报率 / 增长分布。
- **误报控制**：单个大跳若伴随"奇遇/天材地宝 + 关卡代价"说明，判定合法；曲线检测的是"无铺垫连续通胀"。

### 样例 12 · 人设漂移
- **前置**：`characters.personality` 基线档案："林砚谨慎隐忍、谋定后动"。第 1–10 章言行与基线一致。
- **冲突片段**：第 12 章 `林砚一脚踹开城主府大门，指着城主鼻子喝道："给我滚出来！今日老子就要你的命！"——毫无变故铺垫。`
- **预期检出**：L2 抽样审计（每 K 章，跨章比对性格基线）→ `persona` 漂移
- **预期 Finding**：`conflict_type=persona, severity=hint, scope=local`
- **度量指标**：采样覆盖率、漂移检出率 / 误报率。
- **误报控制**：漂移属软冲突，只标 hint 不阻塞；若前文有"血海深仇触发黑化"的情节铺垫，则不漂移。
- **检测现状**：✅ **已落地（2026-08-12，全局审计抽样 L2）**——`characters.personality` 作基线，每 K 章（默认 10）批次触发 / 手动端点，确定性名提及抽样（cap 3）→ 1 次 LLM 判定（json_mode）→ `normalize_and_verify_findings` 确定性证据核验（引文逐字子串 + 章在窗口 + 角色在采样集 + 置信度 ≥0.6）→ `persona/hint/local/L2`；见 [global_audit.py](../src/myink/validation/global_audit.py) / [test_global_audit.py](../tests/test_global_audit.py)。

### 样例 13 · 大纲偏差（预期事件未发生）
- **前置**：ChapterPlan.`expected_events` = ["林砚收回'玉佩真相'伏笔"]；`hooks_to_resolve` 命中池中伏笔"玉佩真相"。
- **冲突片段**：整章正文未出现玉佩，也无任何提及/交代；伏笔仍 `planted`。
- **预期检出**：偏差比对（expected_events vs extract 实际事件）→ 偏差报告 → **作者决策**（改正文拉回 / 改大纲顺水推舟）
- **预期 Finding**：`conflict_type=foreshadow, severity=major` + 偏差报告条目
- **度量指标**：偏差率、大纲修订次数、修订采纳率。

### 样例 14 · 桥段重复
- **前置**：第 5 章事件摘要向量已入库："拍卖会上林砚被嘲讽、亮出身份打脸"。
- **冲突片段**：第 15 章 `同样的拍卖厅，同样的叫价，同样的嘲笑，林砚再次亮出令全场寂静的底牌。`
- **预期检出**：事件向量近邻命中（章节事件摘要相似度高）→ L2 判定"刻意呼应（call-back）" vs "偷懒重复"
- **预期 Finding**：`conflict_type=style, severity=hint, scope=local`
- **度量指标**：重复检出率 / 误报率、随章节数重复率曲线（应不随章节数增长）。
- **误报控制**：这是难点——刻意呼应属正常手法。L2 必须给证据（两处桥段的差异度、是否有呼应标记），低置信度标 hint。
- **检测现状**：✅ **已落地（2026-08-12，L1 事件层近邻）**——当前章事件候选摘要 vs 历史事件向量近邻（cosine distance < 0.3 且章距 ≥ 10 → `hint`/style/local，每章至多 1 条，draft 含呼应标记词则整章豁免）；见 [l1.py](../src/myink/validation/l1.py) `bridge_repeat_check` / [test_bridge_repeat.py](../tests/test_bridge_repeat.py)。**L2「呼应 vs 重复」判定已落地（2026-08-13，全局审计桥段维度，见样例 37）**。

### 样例 15 · AI 味复发
- **前置**：写作 Prompt 已注入表述禁忌清单（禁"不是…而是…"转折堆砌、段落结尾总结式收束、连续排比）。
- **冲突片段**：连续 3 章结尾 `他不是不知道前路凶险，而是早已没有退路。……他不是畏惧强敌，而是害怕辜负。`
- **预期检出**：L1 高频句式统计（"不是…而是…"频次超阈值）→ 触发风格偏离报告；抽样 L2 比对风格档案
- **预期 Finding**：`conflict_type=style, severity=hint, scope=local`
- **度量指标**：AI 味复发率、高频句式频次曲线（随章节数应不增长）。
- **误报控制**：文风是作者自由，只暴露"复发趋势"不阻塞；前端可"忽略"回流标注。
- **检测现状**：✅ **已落地（2026-08-12，L1 高频句式/用词统计）**——`style_profile.fatigue_patterns`（句式 regex findall）单句式单章 ≥2 次（本片段单章内 2 处「不是…而是…」恰在边界）→ `style/hint/local`，每章至多 1 条；`fatigue_words`（词级 count）单词 ≥3 次亦触发；写章 Prompt 同步注入「高频词节制」。见 [l1.py](../src/myink/validation/l1.py) `style_repeat_check` / [test_style_repeat.py](../tests/test_style_repeat.py)。**L2 部分**：✅ **已落地（2026-08-13，全局审计文风维度抽样比对）**——每 K 章对窗口章节抽样摘录 vs 窗口前已确认章节基线（锚定作者自身风格、相对漂移）+ 文风档案，LLM 判 drift|ok，确定性守卫 0 误报（样例 38/39，见 [global_audit.py](../src/myink/validation/global_audit.py) 文风维度 / [test_style_audit.py](../tests/test_style_audit.py)）。**仍属后续**：句长分布、档案参考样本摘录等基线字段。

---

## C. §7.7-7.9 记忆/图谱/伏笔线样例（12 例，plan.md §19.1 项 2）

> 8 例阳性 + 4 例阴性，补"台账/状态机 vs 正文/候选"的一致性：人物状态台账（样例 16/20/21，阴性 24）、关系动态（样例 17/22，阴性 25）、伏笔状态机（样例 18/23，阴性 26）、剧情线线程债务（样例 19，阴性 27）。**检测现状（2026-08-13）**：样例 18/19/23 纯确定性债务检查 + 阴性 26/27 **已落地**（0 误报，见 [l1.py](../src/myink/validation/l1.py) / [test_debt_checks.py](../tests/test_debt_checks.py)）；样例 16/17/20/21/22 + 阴性 24/25 **台账侧已落地**——通用候选 old_value-vs-台账 L1（minor，样例 16/20/21 确定性窄脚印）+ persist 关系关闭修复 + L1 关系台账自洽（major，样例 17/22 台账侧），见 [l1.py](../src/myink/validation/l1.py) `_old_value_ledger_check` / `relation_ledger_check`、[nodes.py](../src/myink/workflow/nodes.py) `_close_active_relations`、[test_state_relation_checks.py](../tests/test_state_relation_checks.py)；**正文-台账语义比对（"无过渡推翻" / "无身份来源却示人" / "无变更却表现相反"）已落地（2026-08-13，L2 语义比对）**——L1 窄脚印管「extract 误读」（old≠台账），L2 语义比对管「正文直接表现新值却无建立交代」（old==台账，判定集预滤 + validator_l2 LLM + 本地守卫，见 [ledger_l2.py](../src/myink/validation/ledger_l2.py) / [test_ledger_l2.py](../tests/test_ledger_l2.py)）；**阴性样例即误报边界，已落地部分必须 0 误报**。登记为评测集基线，检测落地后按同一协议跑检出率 / 误报率。

> **正文-台账语义比对 L2 机制（2026-08-13 落地）**：extract 候选（`character_state` / `relation_change`）进 `ledger_l2.build_judgment_set` 预滤——只有「候选 `old_value` == 台账当前值 且 `new_value` ≠ 台账当前值」进判定集（realm/alive 有专属 L1 检查，不进判定集防双报）；判定集空 → 零 LLM 调用（成本短路，写作路径每章至多 1 次 L2 且可 0 次）。LLM（`validator_l2`）只判 `valid | invalid`：`valid` = 当前章正文**明确建立了该变更**（过渡 / 来源 / 变更记录，样例 24/25 即此形态）；`invalid` = 直接表现新值却无任何建立交代。本地守卫逐字核验 evidence ∈ 内存 draft（L2 跑在 persist 前，不能读库）、`key ∈ 判定集`、`confidence ≥ 0.6` 才出 finding；严重度由字段**确定性映射**（identity/injury → major，location/goal/power/item/knowledge → minor，relation_change → major/structural），LLM 不参与评级。**边界**：extract 漏产候选即漏检（证据链必须从候选进入，与 L1 窄脚印同哲学）；台账无当前值（fresh 书首写）→ 不进判定集；L1（old≠台账，抽取误读）与 L2（old==台账，语义推翻）互斥，conflict_key 不撞。

### 样例 16 · 状态失真·伤/位置（§7.7 人物状态台账）— 点级·阳性
- **前置**：`character_states` 台账 ch8：林砚 `injury=濒死`、`location=东海水宫`（valid_to 未关）；此后无任何治疗 / 移动记录。
- **冲突片段**：ch11 `林砚负手立于北境城楼，气色如常，与守将谈笑风生。`——正文无养伤、无离开水宫的中间状态演化。
- **预期检出**：L2 → `character_state`（台账显式状态被无过渡推翻：`injury` 濒死→如常、`location` 水宫→北境，均无中间行）
- **预期 Finding**：`conflict_type=character_state, severity=major, scope=local`
- **检测现状**：✅ **已落地（2026-08-12 台账侧 L1 窄脚印 / 2026-08-13 正文侧 L2 语义比对）**——通用候选 old_value-vs-台账 L1（`minor/local`，见 [l1.py](../src/myink/validation/l1.py) `_old_value_ledger_check` / [test_state_relation_checks.py](../tests/test_state_relation_checks.py)）；「无过渡推翻」的正文-台账语义比对 L2 已落地（`character_state/major/local`，见 [ledger_l2.py](../src/myink/validation/ledger_l2.py) / [test_ledger_l2.py](../tests/test_ledger_l2.py)）。
- **误报控制**：台账是"最近有效行"（§7.7 只追加）；若正文先有"服回春丹闭关"且 extract 落 `injury=痊愈` 候选 → 合法（样例 24 即此形态）。只查"显式状态被无过渡推翻"。
- **度量指标**：状态失真检出率 / 误报率（§16 人物属性保持率）。

### 样例 17 · 关系演变矛盾·敌对→结盟（§7.8 人物关系动态）— 点级·阳性
- **前置**：`relations` 台账：林砚-蛟王 = `hostile`（第 6 章，valid_to 未关）；无 `ally` / 临时盟约记录。
- **冲突片段**：ch14 `蛟王与林砚并肩立于水宫殿前，道："林兄，今日之事有劳了。"`——无解仇 / 结盟情节铺垫。
- **预期检出**：L2 → `relation`（正文-台账语义比对：extract `relation_change` 候选 old==台账，无变更记录却表现相反）
- **预期 Finding**：`conflict_type=relation, severity=major, scope=structural`
- **检测现状**：✅ **已落地（2026-08-12 台账侧 / 2026-08-13 正文侧 L2）**——persist 关闭修复（relation_change 落库先关同 (source,target) 有序对全部活跃旧行 + 透传 valid_to，[nodes.py](../src/myink/workflow/nodes.py) `_close_active_relations`）+ L1 关系台账自洽（重复/矛盾活跃行 → `major/structural`，[l1.py](../src/myink/validation/l1.py) `relation_ledger_check`）+ extract 第 6 类 `relation_change` 产出（[prompts.py](../src/myink/workflow/prompts.py) `SYSTEM_EXTRACT`，快照注入校准 old_value）+ L1 窄脚印（old≠台账 → `minor/local`）。正文「无变更却表现相反」语义比对 L2 已落地（`relation/major/structural`，[ledger_l2.py](../src/myink/validation/ledger_l2.py) / [test_ledger_l2.py](../tests/test_ledger_l2.py)）。
- **误报控制**：若正文先有"一杯酒泯恩仇"且 extract 落 `relation_change`（hostile→ally）→ 合法（样例 25 即此形态）；只查"无变更记录却表现相反"。
- **度量指标**：关系矛盾检出率 / 误报率（§16 关系和阵营冲突召回率）。

### 样例 18 · 伏笔烂尾·planted 无触碰（§7.9 伏笔治理）— 长线级·阳性
- **前置**：第 3 章伏笔"青玉匣中封印之物"：`foreshadows.status=planted, planted_chapter=3, last_touched=3`；ch4–18 共 15 章无任何触碰（无 developing / resolved 动作）。
- **冲突片段**：ch19 全书推进到"黑市玉佩线"，青玉匣线被完全搁置，主角再未提及，伏笔仍 `planted`。
- **预期检出**：L1 债务（planted 且空 trigger 且 `last_touched` 距当前章 > 阈值 → 伏笔烂尾告警）
- **预期 Finding**：`conflict_type=foreshadow, severity=hint, scope=local`
- **检测现状**：✅ **已落地**（2026-08-12，L1 确定性，见 [l1.py](../src/myink/validation/l1.py) `foreshadow_debt_check` / [test_debt_checks.py](../tests/test_debt_checks.py)）。阈值 15 章；`trigger` 非空即"刻意长沉"跳过（样例 26 阴性 0 误报）。
- **误报控制**：长线伏笔可合法沉 20+ 章（回收条件在 `trigger`，样例 26 即此形态）；按优先级 / 回收条件区分，只对"低优先级且长期无触碰"告警，且只标 hint 不阻塞（作者决策：收 / 弃）。
- **度量指标**：伏笔烂尾检出率、开放伏笔年龄分布（§16 伏笔回收率）。

### 样例 19 · 剧情线停滞·主线（§7.9 / §8.6 线程债务）— 长线级·阳性
- **前置**：`plot_threads` "万魔渊线"：`kind=main, status=active, last_progress_chapter=5`；ch6–24 共 19 章该线零推进（progress 未更新）。
- **冲突片段**：ch25 主线全部围绕黑市玉佩线，万魔渊线开放未推进 20 章、无任何侧面提及。
- **预期检出**：L1 债务（active 且 `kind=main` 且距 `last_progress_chapter` > 阈值 → 停滞告警）
- **预期 Finding**：`conflict_type=plotline, severity=hint, scope=local`
- **检测现状**：✅ **已落地**（2026-08-12，L1 确定性，见 [l1.py](../src/myink/validation/l1.py) `plot_thread_debt_check` / [test_debt_checks.py](../tests/test_debt_checks.py)）。阈值 15 章；`kind=side` 直接跳过（样例 27 阴性 0 误报）。闲置时长按 `last_progress_chapter` 与当前章现算，不落列（2026-09-18 删除从未写入的 `open_duration`）。
- **误报控制**：支线（`kind=side`）可长期休眠，阈值放宽（样例 27 即此形态）；主线（`kind=main`）停滞才告警；只标 hint，作者决策（推进 / 收线）。
- **度量指标**：主线停滞检出率、开放线程数曲线（随章节数应不增长，§16）。

### 样例 20 · 状态失真·目标（§7.7 人物状态台账）— 点级·阳性
- **前置**：`character_states` 台账 ch6：林砚 `goal="查明玉佩真相"`；ch7–13 无目标变更记录、无转折事件（无"放弃追查"剧情）。
- **冲突片段**：ch14 `林砚已彻底放下玉佩一事，将全副心思投在黑市商号上，与旧敌把酒言欢。`——正文无转折铺垫，台账 `goal` 未变。
- **预期检出**：L2 → `character_state`（`goal` 无过渡推翻）
- **预期 Finding**：`conflict_type=character_state, severity=minor, scope=local`
- **检测现状**：✅ **已落地（2026-08-12 台账侧 L1 窄脚印 / 2026-08-13 正文侧 L2 语义比对）**——通用候选 old_value-vs-台账 L1（`minor/local`，见 [l1.py](../src/myink/validation/l1.py) `_old_value_ledger_check` / [test_state_relation_checks.py](../tests/test_state_relation_checks.py)）；「无过渡推翻」的正文-台账语义比对 L2 已落地（`character_state/minor/local`，见 [ledger_l2.py](../src/myink/validation/ledger_l2.py) / [test_ledger_l2.py](../tests/test_ledger_l2.py)）。
- **误报控制**：若正文先有"玉佩真相揭露"转折且 extract 落 `goal` 变更候选 → 合法；目标切换是作者自由，只查"无过渡推翻 + 无任何交代"。
- **度量指标**：状态失真检出率 / 误报率（§16 人物属性保持率）。

### 样例 21 · 状态失真·身份（§7.7 人物状态台账）— 点级·阳性
- **前置**：`character_states` 台账 ch7：林砚 `identity="散修"`；ch8–9 无受封 / 夺权 / 伪装记录。
- **冲突片段**：ch11 `林砚以城主身份坐镇北境城，点将校尉，发号施令。`——台账 `identity` 无变化，正文无身份来源（受封 / 夺权）事件。
- **预期检出**：L2 → `character_state`（`identity` 无过渡切换）
- **预期 Finding**：`conflict_type=character_state, severity=major, scope=local`
- **检测现状**：✅ **已落地（2026-08-12 台账侧 L1 窄脚印 / 2026-08-13 正文侧 L2 语义比对）**——通用候选 old_value-vs-台账 L1（`minor/local`，见 [l1.py](../src/myink/validation/l1.py) `_old_value_ledger_check` / [test_state_relation_checks.py](../tests/test_state_relation_checks.py)）；「无身份来源却以新身份示人」的正文-台账语义比对 L2 已落地（`character_state/major/local`，见 [ledger_l2.py](../src/myink/validation/ledger_l2.py) / [test_ledger_l2.py](../tests/test_ledger_l2.py)）。与样例 9 区分：样例 9 是 `faction` 阵营归属矛盾，本条是台账身份字段一致性。
- **误报控制**：若正文先有受封 / 夺权事件且 extract 落 `identity` 变更候选 → 合法（样例 9 误报控制已登记伪装记录路径）；只查"无身份来源却以新身份示人"。
- **度量指标**：状态失真检出率 / 误报率（§16 人物属性保持率）。

### 样例 22 · 关系演变矛盾·师徒→敌对（§7.8 人物关系动态）— 点级·阳性
- **前置**：`relations` 台账：林砚-沈沧澜 = `master_student`（第 2 章，valid_to 未关）；无决裂事件、无关系变更记录。
- **冲突片段**：ch16 `沈沧澜立于林砚对面，冷笑："逆徒，今日便取你性命。"林砚亦拔剑相向。`——无欺师灭祖 / 逐出师门铺垫。
- **预期检出**：L2 → `relation`（正文-台账语义比对：正文表现敌对 vs 台账 `master_student`，无变更记录却表现相反）
- **预期 Finding**：`conflict_type=relation, severity=major, scope=structural`
- **检测现状**：✅ **已落地（2026-08-12 台账侧 / 2026-08-13 正文侧 L2）**——与样例 17 同：persist 关闭修复 + L1 关系台账自洽（重复/矛盾活跃行 → `major`，见 [l1.py](../src/myink/validation/l1.py) `relation_ledger_check` / [test_state_relation_checks.py](../tests/test_state_relation_checks.py)）+ extract 第 6 类 `relation_change` 产出；正文「无变更却表现相反」语义比对 L2 已落地（`relation/major/structural`，[ledger_l2.py](../src/myink/validation/ledger_l2.py) / [test_ledger_l2.py](../tests/test_ledger_l2.py)）。
- **误报控制**：若正文先有"逐出师门"且落 `relation_change`（master_student→hostile）→ 合法；只查"无变更却表现相反"。
- **度量指标**：关系矛盾检出率 / 误报率（§16 关系和阵营冲突召回率）。

### 样例 23 · 伏笔烂尾·developing 无推进（§7.9 伏笔治理）— 长线级·阳性
- **前置**：伏笔"玉佩与万魔渊的关联"：`status=developing, planted=5, last_touched=12`；触发条件已成熟（玉佩已到手）；ch13–22 共 10 章无推进。
- **冲突片段**：ch23 剧情转向新线，该伏笔既未 `resolved` 也未 `dropped`，再无触碰。
- **预期检出**：L1 债务（developing 且 `last_touched` 距当前章 > 阈值 → 烂尾告警；以 developing 状态作"作者已承诺"的确定性代理，不做 trigger 成熟度语义判断）
- **预期 Finding**：`conflict_type=foreshadow, severity=hint, scope=local`
- **检测现状**：✅ **已落地**（2026-08-12，L1 确定性，见 [l1.py](../src/myink/validation/l1.py) / [test_debt_checks.py](../tests/test_debt_checks.py)）。阈值 10 章；样例 18 是 `planted`+空 trigger 形态，本条是 `developing` 形态——捡起一半的伏笔长期不落地即告警。
- **误报控制**：只对 developing 且长期不落地告警（阈值 10 章）；trigger 成熟度语义判断属 L2/阶段 3 主体，确定性层以状态作代理、宁紧不松，误报靠阈值 + 阴性样例约束；标 hint，作者决策（收 / 弃）。
- **度量指标**：伏笔烂尾检出率、开放伏笔年龄分布（§16 伏笔回收率）。

### 样例 24 · 状态失真·合法演化（§7.7 人物状态台账）— 点级·阴性
- **前置**：台账 ch8：林砚 `injury=濒死`；ch9 正文"服回春丹闭关"且 extract 落 `injury=痊愈` 候选，台账更新；ch10 台账 `injury=痊愈`。
- **合法片段**：ch11 `林砚面色红润，与守将谈笑风生。`——伤情有中间行演化（ch9 治疗记录）。
- **预期**：**不检出**——有合法中间行，台账"最近有效行"语义（§7.7 只追加）
- **误报控制**：只查"显式状态被**无过渡**推翻"；有中间行即合法。
- **度量指标**：误报率（此例被误报 = 状态失真误报 +1）。

### 样例 25 · 关系演变矛盾·合法结盟（§7.8 人物关系动态）— 点级·阴性
- **前置**：台账：林砚-蛟王 = `hostile`（ch6）；ch13 正文"把酒言和、结下三月盟约"，extract 落 `relation_change`（hostile→ally，带 `valid_to`），台账更新。
- **合法片段**：ch14 `蛟王与林砚并肩而立，道："林兄，今日之事有劳了。"`——有盟约记录与铺垫。
- **预期**：**不检出**——临时盟约带 `valid_to`；到期后回到 hostile 属正常演化
- **误报控制**：只查"无变更记录却表现相反"；有 `relation_change` 即合法。（✅ 2026-08-12：persist 现透传候选 `valid_to`（临时盟约窗口），见 [nodes.py](../src/myink/workflow/nodes.py) `_close_active_relations`；带 `valid_to` 的行非活跃，`get_relations` 不返回 → 台账自洽检查天然不误报）
- **度量指标**：误报率（此例被误报 = 关系矛盾误报 +1）。

### 样例 26 · 伏笔·合法长沉（§7.9 伏笔治理）— 长线级·阴性
- **前置**：伏笔"青玉匣封印之物"：`planted=3`、**高优先级**、`trigger="主角修为突破元婴后方可开启"`；ch4–19 主角仍在金丹（条件未成熟）。
- **合法片段**：ch19 青玉匣线未触碰——触发条件未成熟，作者刻意压线。
- **预期**：**不检出**——触发条件在 `trigger`；条件未成熟的长沉不告警
- **误报控制**：与样例 18 区分：只对"低优先级且长期无触碰"告警；高优先级 + 有条件的长沉合法。（✅ 已落地：`trigger` 非空即跳过，样例 18 检查确认，0 误报，见 [l1.py](../src/myink/validation/l1.py)）
- **度量指标**：误报率（此例被误报 = 伏笔烂尾误报 +1）。

### 样例 27 · 剧情线·支线休眠（§7.9 / §8.6 线程债务）— 长线级·阴性
- **前置**：`plot_threads` "灵兽谷支线"：`kind=side, status=active, last_progress_chapter=3`；ch4–25 共 21 章无推进。
- **合法片段**：ch26 支线仍休眠——支线可合法长期沉睡。
- **预期**：**不检出**——停滞告警只针对 `kind=main`（样例 19 区分）
- **误报控制**：支线（side）阈值放宽或免检；主线（main）才告警。（✅ 已落地：`kind=side` 直接跳过，0 误报，见 [l1.py](../src/myink/validation/l1.py)）
- **度量指标**：误报率（此例被误报 = 剧情线停滞误报 +1）。

---

## D. 点级 / 长线级阴性对照（5 例）

> 前 15 例全为阳性（应检出），无一例测**误报边界**——单独跑它们 `误报率` 无法定义。本节把各例"误报控制"要点落成可复现样例：**检测落地后这些例必须 0 检出**，否则计入误报率。

### 样例 28 · 地点移动·合法（样例 4 对照）— 点级·阴性
- **前置**：东海水宫与荒原部隔全九州；正文写明借传送阵。
- **合法片段**：`清晨林砚还在东海水宫与蛟王对饮，黄昏已站在荒原部图腾祭坛前——一道传送阵的蓝光在他身后缓缓散去。`
- **预期**：**不检出**——写明传送即合法
- **误报控制**：只查"无传送说明却跨域"（样例 4）；有传送阵 / 灵舟即合法。
- **度量指标**：误报率（此例被误报 = 地点冲突误报 +1）。

### 样例 29 · 首次来访·合法（样例 5 对照）— 点级·阴性
- **前置**：事件表 ch8 有"林砚在北境城完成交易"；正文 ch12 前有"旧地重游"交代。
- **合法片段**：`林砚望着北境城城墙，心道：故地重游，上一次来还是三年前。`
- **预期**：**不检出**——重游 / 失忆说明即合法
- **误报控制**：只查"确有此经历却称首次"（样例 5）；有重游交代即合法。
- **度量指标**：误报率（此例被误报 = 身份冲突误报 +1）。

### 样例 30 · 物品·合法重铸（样例 6 对照）— 点级·阴性
- **前置**：事件表 ch10 记录"定水珠被天劫劈碎"；`facts` 有"以千年寒铁重铸"记录。
- **合法片段**：`林砚从怀中摸出定水珠，幽蓝光晕流转——那是他以千年寒铁重新淬炼之物。`
- **预期**：**不检出**——facts 有修复记录即合法
- **误报控制**：只查"已毁无修复再现"（样例 6）；有重铸 facts 记录即合法。
- **度量指标**：误报率（此例被误报 = 物品冲突误报 +1）。

### 样例 31 · 战力·合法大跳（样例 11 对照）— 长线级·阴性
- **前置**：金丹中期 → 元婴：正文有完整突破契机与代价。
- **合法片段**：`林砚吞下化婴丹，心魔幻象重重，他咬牙撑过三日夜，元婴终于凝聚。`——单次大跳伴奇遇 + 关卡代价。
- **预期**：**不检出**——曲线检测针对"无铺垫连续通胀"，单次有契机的大跳合法
- **误报控制**：样例 11 的误报控制落成样例；连续多章无铺垫才触发曲线告警。
- **度量指标**：误报率（此例被误报 = 战力通胀误报 +1）。

### 样例 32 · 桥段·刻意呼应（样例 14 对照）— 长线级·阴性
- **前置**：事件向量库有 ch5"拍卖会打脸"摘要；正文 ch15 前有呼应意图（目的不同）。
- **合法片段**：`同样的拍卖厅，同样的叫价——林砚暗想：这一幕与当年何其相似。他亮出底牌，这次却非争锋，只为引蛇出洞。`
- **预期**：**不检出**——刻意呼应（call-back）非偷懒重复
- **误报控制**：样例 14 的难点：呼应 vs 重复须 L2 证据（差异度、呼应标记、目的不同），低置信度宁不标。
- **度量指标**：误报率（此例被误报 = 桥段重复误报 +1）。
- **检测现状**：✅ **已落地（2026-08-12）**——draft/候选摘要含呼应标记词（当年/何其相似/重演/再现…）即豁免整章/该候选，实测 0 误报（即便 cosine distance=0.0 命中也豁免）；见 [l1.py](../src/myink/validation/l1.py) `_has_callback_marker` / [test_bridge_repeat.py](../tests/test_bridge_repeat.py)。**词表未命中但实为呼应 → L2 抽样判定已落地（2026-08-13，样例 37：无标记词 → 进 LLM 判呼应，0 误报）**。

---

## E. §7.8 关系台账自洽（新增样例 33–35，2026-08-12，评测集自生长 §16）

> 与样例 17/22 台账侧配套：persist 关闭修复后保证「每对至多一条活跃」不变量，L1 兜底存量脏数据。**阳性算检出率分子、阴性算误报率分子**。

### 样例 33 · 关系台账·重复活跃（§7.8）— 点级·阳性
- **前置**：`relations` 台账（存量脏数据）：林砚-蛟王 `hostile` 两条活跃行（ch3、ch7 各写入一次，valid_to 均未关）。
- **冲突片段**：ch8 校验运行——同一有序对同类型存在 2+ 条活跃行，「当前关系」不可判定。
- **预期检出**：L1 → `relation`（重复活跃，`major/structural`）
- **预期 Finding**：`conflict_type=relation, severity=major, scope=structural`
- **检测现状**：✅ **已落地**（2026-08-12，见 [l1.py](../src/myink/validation/l1.py) `relation_ledger_check` / [test_state_relation_checks.py](../tests/test_state_relation_checks.py)）。
- **误报控制**：post-修复 persist 新写入不会产生（先关后写）；只对存量/手工脏数据告警。
- **度量指标**：检出率（此例检出 = 关系台账自洽检出 +1）。

### 样例 34 · 关系台账·矛盾活跃（§7.8）— 点级·阳性
- **前置**：`relations` 台账：林砚-蛟王 `hostile` 与 `ally` 各一条活跃行（valid_to 均未关）。
- **冲突片段**：ch8 校验运行——同有序对敌/盟并存，语义互相矛盾。
- **预期检出**：L1 → `relation`（矛盾活跃，`major/structural`）
- **预期 Finding**：`conflict_type=relation, severity=major, scope=structural`
- **检测现状**：✅ **已落地**（2026-08-12，见 [l1.py](../src/myink/validation/l1.py) `relation_ledger_check` / [test_state_relation_checks.py](../tests/test_state_relation_checks.py)）。
- **误报控制**：只查同向有序对；反向对 (tgt,src) 独立关系（§9.3 成对落库语义），互不干扰。
- **度量指标**：检出率（此例检出 = 关系台账自洽检出 +1）。

### 样例 35 · 关系台账·单行活跃 / 临时盟约窗口（§7.8）— 点级·阴性
- **前置**：林砚-蛟王 `hostile` 一条活跃行（ch3，valid_to 未关）；另有一条 `ally` 带 `valid_to=ch13`（临时盟约，已到期关闭 / 未到期窗口内）。
- **合法片段**：ch8 校验运行——单行活跃 + 带 `valid_to` 的盟约行（非活跃）。
- **预期**：**不检出**——每对恰一条活跃行；带 `valid_to` 的行 `get_relations` 不返回（schema.md §6 当前关系定义）
- **误报控制**：只查活跃行（`valid_to IS NULL`）；临时盟约窗口天然排除（样例 25 同形态）。
- **度量指标**：误报率（此例被误报 = 关系台账自洽误报 +1）。

---

## F. §8.6 全局审计抽样 L2（新增样例 36/37/38/39，2026-08-12 人设漂移误报边界 + 2026-08-13 桥段重复呼应判定 + 2026-08-13 文风漂移抽样比对）

> 样例 12 的阴性对照：L2 判定只标「确有漂移且无变故铺垫 / 成长弧线」——转变有正当因果即不漂移（§8.6 误报控制：漏报优于误报）。**阴性算误报率分子**。

### 样例 36 · 人设·变故铺垫后的性格转变（样例 12 对照）— 长线级·阴性
- **前置**：`characters.personality` 基线档案："林砚谨慎隐忍、谋定后动"。第 1–11 章言行与基线一致；ch12 前有明确变故铺垫（师门血仇 / 至亲遇害）。
- **合法片段**：`得知师门被灭，林砚盛怒之下一脚踹开城主府大门，喝道："今日老子就要你的命！"`——行为模式与样例 12 相同，但**有正当因果触发**。
- **预期**：**不检出**——无变故铺垫才漂移；血海深仇触发黑化属成长弧线
- **误报控制**：全局审计判定 prompt 显式要求「无变故铺垫 / 成长弧线才标漂移」；证据不足 / 边界情形宁不输出（空数组 = 无漂移）；确定性证据核验只放行逐字引文子串的 finding。
- **度量指标**：误报率（此例被误报 = 人设漂移误报 +1）。语义负例由 LLM 判定承载，另有过标记负例（引文非子串等）由确定性守卫兜底 0 误报（[test_global_audit.py](../tests/test_global_audit.py)）。

### 样例 37 · 桥段·无词表标记的刻意呼应（样例 32 的 L2 对照）— 长线级·阴性
- **前置**：事件向量库有 ch5"拍卖会打脸"摘要；正文 ch15 事件摘要与历史向量近邻命中（雷同），但正文**无任何呼应标记词**。
- **合法片段**：`拍卖厅叫价声再起。林砚不动声色举牌，等那条藏在暗处的蛇自己上钩。`——目的不同（引蛇出洞非争锋），无"当年/何其相似/重演/再现"等词表标记。
- **预期**：**不检出**——刻意呼应（call-back）非偷懒重复；L2 判定要求 LLM 给差异点/目的，目的不同即呼应
- **误报控制**：样例 32 的词表预滤除救不了这类（无标记词 → L1 会误报）；L2 依赖 LLM 判「目的/差异」——prompt 显式要求「无新意无新目的才标偷懒重复」，verdict=echo / 证据不足宁不输出；确定性守卫只放行 verdict=repeat + 逐字引文的 finding。
- **度量指标**：误报率（此例被误报 = 桥段重复误报 +1）。与样例 14 成对构成 L2「区分呼应 vs 重复」的精度锚点（14 阳性应检出 / 37 阴性应不检出）。
- **检测现状**：✅ **已落地（2026-08-13，全局审计桥段维度 L2）**——样例 32 词表预滤除对已豁免的对不重审；本样例无标记词 → 进入 LLM 判定，判 echo → 0 检出（[test_bridge_audit.py](../tests/test_bridge_audit.py) `test_bridge_negative_sample37_unmarked_echo`）；守卫独立保证 LLM 过度标 repeat 也过不了逐字核验（`test_bridge_guard_drops_*` 三例）。

### 样例 38 · 文风·全书风格漂移（样例 15 的 L2 对照）— 长线级·阳性
- **前置**：本书早 1–10 章已确立文风（仙侠雅句、第三人称限知、长短句交错，与文风档案一致）；事件/人设无异常。
- **冲突片段**：第 12–15 章突然全用现代网络口语、对话失去角色区分：`林砚拍桌而起："卧槽，这也太离谱了吧，直接开干！"`——与早先章节系统性偏离，非单场景变化。
- **预期检出**：L2 抽样比对窗口摘录 vs 窗口前已确认章节基线（+ 文风档案）→ 判 drift → `style/hint/local/L2`
- **度量指标**：风格漂移检出率 / AI 味复发率。面级漂移由 L2 单独承担——漂移文本规避 L1 fatigue 阈值，companion 断言 [test_style_audit.py](../tests/test_style_audit.py) `test_style_l1_not_triggered_on_sample38` 证明 L1 不触发。
- **误报控制**：L2 只标「与既定文风系统性持续偏离」；单场景节奏/情感合法变化不标；证据不足/低置信度宁不输出；确定性守卫只放行 verdict=drift + 逐字引文 + 采样章。
- **检测现状**：✅ **已落地（2026-08-13，全局审计文风维度 L2）**——基线 = 窗口前已确认章节（even-spacing cap 4，锚定作者自身风格、相对漂移），窗口抽样 even-spacing cap 4 + 文风档案上下文，LLM 判 drift|ok，确定性守卫 0 误报（[test_style_audit.py](../tests/test_style_audit.py) `test_style_positive_sample38_drift`）。已知边界：首个窗口（无基线锚点）style 中性跳过、移动基线测不出慢速累积漂移（[global_audit.py](../src/myink/validation/global_audit.py) docstring）。

### 样例 39 · 文风·场景节奏合法变化（样例 38 对照）— 长线级·阴性
- **前置**：同样例 38，早 1–10 章既定文风。
- **合法片段**：第 13 章大战高潮用短句快节奏：`剑鸣刺耳。血溅三尺。林砚不退，剑锋再进。`——场景张力需要，非全书性漂移。
- **预期**：**不检出**——场景级节奏/情感合法变化不判漂移
- **误报控制**：L2 判定 prompt 显式要求「单场景节奏/情感合法变化不标」；LLM 判 ok / 空数组 → 0 检出（[test_style_audit.py](../tests/test_style_audit.py) `test_style_negative_sample39_scene_variation`）；守卫独立保证过度标记过不了逐字核验（`test_style_guard_drops_*` 四例）。
- **度量指标**：误报率（此例被误报 = 文风漂移误报 +1）。与样例 38 成对构成 L2「面级漂移 vs 场景变化」的精度锚点。

### 样例 40 · 阵营·临时联手合法（样例 2 对照）— 点级·阴性（2026-08-13 补漏切片新增，评测集自生长 §16）
- **前置**：林砚与天衡宗执法弟子敌对（relations `hostile` 活跃行）；宗门已传讯约定**暂时联手**（relations 落临时盟约，带 `valid_to`，覆盖联手各章）。
- **合法片段**：`夜色中，林砚与天衡宗执法弟子并肩而立，共抗万魔渊魔修。此前宗门已传讯约定：此役只是暂时联手，事毕各归其营。`
- **预期**：**不检出**——样例 2 误报控制成立（已落临时盟约 → 守卫跳过）
- **误报控制**：`faction_check` 守卫查「非 hostile 行（带 `valid_to` 落在本章，或永久非敌对行）」→ 跳过（[l1.py](../src/myink/validation/l1.py) `faction_check` / [test_conflict_sample_suite.py](../tests/test_conflict_sample_suite.py) `test_sample_40_...`）；永久非敌对（hostile+ally 双活跃）本由 `relation_ledger_check` 判矛盾兜底，faction 不双报。
- **度量指标**：误报率（此例被误报 = 阵营敌对误报 +1）。与样例 2 构成 L1「敌我矛盾 vs 合法联手」的精度锚点。

---

## 使用方式

1. **跑测**：阳性（样例 1–23、33–34、38）埋入章节样本或独立跑样例，跑 L1 + L2，统计**检出率**；阴性（样例 24–32、35、37、39、40）单独跑正常章节，统计**误报率**（阴性被检出 = 误报 +1）。两组缺一，指标不成立；**全集系统性首测套件已落地（2026-08-13）**——[test_conflict_sample_suite.py](../tests/test_conflict_sample_suite.py) 逐例直连真实机制（仅 mock LLM 判定），40 例全量矩阵一次跑完（`pytest tests/test_conflict_sample_suite.py -rx`）；首测结果 = 检出率 **76.9%**（20/26）、误报率 **0%**（0/13），**补漏切片复测（2026-08-13）**：样例 2 阵营敌对 L1 faction 机制落地（`faction_check`，新增阴性样例 40）→ 检出率 **80.8%**（21/26）、误报率 **0%**（0/14）；5 漏检（样例 5/6/7/8/9：timeline/item_rule/foreshadow-兑现/power-规则/归属机制未实现）标 `xfail(strict=False)` 记录缺口——实现后 XPASS 即取消标记，新增机制先在本套件补对应样例再验收；
2. **调阈值**：L1 阈值宁缺毋滥（plan.md §8.8），先保证阴性 0 误报，再抬阳性检出率；L2 低置信度标 hint 不耗修订预算；
3. **长线**：样例 18/19/23/26/27 债务检查已落地为 **L1 每章 hint**（确定性、零模型成本、不阻塞，见 l1.py / test_debt_checks.py），与周期审计口径一致可直接对账；**样例 14/32 桥段重复的 L1 部分也已并入每章检查**（事件向量近邻 + 呼应词豁免，hint 不阻塞，见 l1.py `bridge_repeat_check` / test_bridge_repeat.py）；其余样例 11–13、15、31 走周期审计（每 K 章一次），不并入每章点检——避免成本与误报不可控；桥段重复的 L2 呼应判定同走周期审计（全局审计桥段维度，2026-08-13 已落地，样例 14 阳性 / 样例 32、37 阴性成对）；文风漂移 L2 抽样比对同走周期审计（全局审计文风维度，2026-08-13 已落地，样例 38 阳性 / 样例 39 阴性成对，首轮审计窗口无基线锚点中性跳过、自第二次起生效）；
4. **度量**：检出率 = 检出阳性 / 阳性总数；误报率 = 误报阴性 / 阴性总数；新增样例按同一协议对账（固定 seed / prompt 模板 / 模型版本，不挑选成功案例，plan.md §16）。
