# Myink — 长篇网文多 Agent 创作系统

面向长篇网络小说连续创作的多 Agent 智能创作系统。通过多 Agent 编排、审核路由、复盘沉淀与多级记忆召回，旨在减少长文中的人设不一致、战力崩坏、剧情前后矛盾、长上下文过载等问题，实现「规划 → 生成 → 校验 → 记忆沉淀」的完整创作闭环。

当前定位：**本地演示与小范围试用的 MVP**。演示登录无密码，不能作为公网账户认证；Compose 默认只绑定本机。

## 核心能力

### 多 Agent 编排 + 审核中枢路由

- 基于 LangGraph 的两级图结构（批次循环图 + 单章子图），一次规划 N 章统一推进主线 / 支线 / 伏笔，批次失败断点续跑，不重跑已完成章节
- 单章支持自动/手动两种模式：Plan 与正文是中栏内可切换的两个全幅页面；手动模式允许作者编辑确认后再写，确认后自动翻到正文，审核触发 replan 时再次显示新版 Plan 并折叠保留旧版。Plan 与正文直接呈现 Redis Stream + SSE 的模型增量，不在全文完成后补播打字动画；刷新后仍可从 checkpoint 和任务记录恢复
- 审核中枢 Agent 输出 `pass / rewrite / replan` 三级路由：确定性规则层优先约束硬性指标（图谱、数值、时间线拓扑）并守住轮次预算，LLM 仅兜底语义判断；超预算自动转人工 `needs_review`
- Reflexion 复盘范式：批次收尾由 LLM 复盘生成经验（冲突原因 + 规避策略），分级沉淀后在后续章节主动注入，同类冲突自动累计规避，将发现沉淀为后续写作约束；减少错误的幅度仍需真实模型对照评测
- 整书大纲（建书第 ③ 步）：按梗概 + 大致章节数 + 大致故事线由 Planner 规划主线弧与逐章目标，落库后注入每一章的规划与写作——逐章推进主线、防止章节开头雷同；可编辑确认或暂不规划

### 三层记忆 + 混合召回

- 短期（上一章摘要）/ 中期（事件）/ 长期（事实）三层记忆分层组织
- PostgreSQL + pgvector 向量腿与人物名关键词腿（ILIKE）RRF 融合召回，硬约束恒在 Top-K、不参与相似度截断
- 章节提示词按完整消息执行估算预算（完整请求默认 64k，`REQUEST_TOKEN_BUDGET` 可调；可选记忆独立使用 `RECALL_TOKEN_BUDGET=12000`）：优先移除可选记忆，硬约束/正文不截断；必需输入或工具结果超限则停止。估算采用中文约 1.4 token/字，实际用量另由模型 usage 记录

### 双层一致性校验

- L1 确定性校验（图谱 / 数值 / 时间线，零模型成本）+ L2 语义校验（LLM-as-judge，引用证据 + 置信度）
- 自建 40 条冲突样例机制回归：基线检出率 80.8%（21/26）、误报率 0%（0/14）。LLM 判定与 embedding 使用测试替身；5 类已知漏检、3 条阴性属于机制缺失下的真空通过，不能作为真实模型质量指标

### 数据流边界

Agent 只输出候选，由编排层处理落库。低风险候选可自动确认；新人物卡片、严重冲突等进入人工确认流程。批次支持暂停和确认后续跑。

### 工程化

- Go(Gin) 网关承载高并发面（限流 / 鉴权 / SSE），与 Python(FastAPI/LangGraph) 推理层通过 HTTP 解耦、独立扩缩容；RabbitMQ 承载异步任务/延迟重投/死信，Redis 承载限流/锁/SSE；Last-Event-ID 在事件保留窗口内追平
- 多租户隔离默认拒绝：PostgreSQL RLS + 事务级 `SET LOCAL` 杜绝连接池跨租户串数据，向量检索显式按项目过滤；过滤场景的召回质量需通过数据集验证，网关 JWT 验签 + 应用层归属断言双保险
- 已落地 MCP Client 标准协议接入起点 DaoSearch 外部榜单（Streamable HTTP，白名单 sanitize + 优雅降级样例），榜单仅作建书前的题材风向灵感工具、不进入记忆 / 事实层、不注入任何生成节点

## 技术栈

| 层 | 技术 |
|---|---|
| 推理编排 | Python 3.12 / FastAPI / LangGraph / SQLAlchemy |
| 网关 | Go / Gin |
| 存储与队列 | PostgreSQL 16 + pgvector / Redis 7 / RabbitMQ 3.13 |
| 前端 | React 19 / TypeScript / Vite / CSS Modules |
| 部署 | Docker Compose 一键启动（单端口 8080） |

## 快速开始

```bash
# 首次复制配置（Windows PowerShell 可用 Copy-Item）
cp .env.example .env
# 启动全部服务（PostgreSQL+pgvector / Redis / RabbitMQ / Python API / Worker / Go 网关）
docker compose up -d --build
```

访问 127.0.0.1:8080（网关静态托管前端，同源提供页面 + API + SSE）。

- 复制配置后即可浏览界面；生成章节前在「环境配置」添加模型连接并指定角色
- 支持 OpenAI 兼容或 Anthropic 原生接口；未配置连接时不会回落内置模型
- 向量召回默认关闭（`EMBED_ENABLED=0`），开启与模型下载见 [docs/DEPLOY.md](docs/DEPLOY.md)
- 首次启动自动建表 + RLS + 示例数据，幂等可重复执行

## 项目结构

```
src/myink/        Python 推理层（workflow 编排 / api / models / providers）
gateway/          Go 网关（转发 / 鉴权 / SSE / 静态托管）
web/              React + TypeScript 前端
tests/            Python 测试（含冲突样例评测集）
spec/             API 契约（OpenAPI）与状态流设计
docs/             部署文档
docker/           initdb 脚本
```

## 编辑可靠性

- 章节草稿按账号/项目/章节保存在当前标签页，切章和刷新后恢复；关闭标签页前仍应保存到服务器。
- 保存携带 `expected_version`，旧版本提交返回 409，保留草稿并显示服务器正文供比较；历史版本保留被覆盖内容。
- 保存期间继续输入不会被误标为已保存；未保存时禁止校正记忆和回退版本。

## 测试与演示

- Python：编排、校验、租户隔离、版本并发冲突、上下文预算及 OpenAPI 契约回归。
- 前端：工具函数与 jsdom 编辑器交互回归，另运行 TypeScript / Vite 构建。
- Go：网关契约、转发、Redis 闸门与 RabbitMQ 集成测试；CI 拒绝静默跳过。
- 契约：`myink contract export` 导出后提交 `spec/api-openapi.json`；CI 检查漂移。
- `bash scripts/ci-local.sh` 使用独立临时基础设施，不改动开发作品库。设置 `SKIP_IMAGES=1` 可跳过镜像构建。详细环境见 [部署文档](docs/DEPLOY.md)。

项目演示步骤、证据边界和下一阶段真实评测方案见 [项目演示与验证](docs/DEMO.md)。测试数量以实际运行输出为准，不将机制测试通过率表述为真实小说质量。
