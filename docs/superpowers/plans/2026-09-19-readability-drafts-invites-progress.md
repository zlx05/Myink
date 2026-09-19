# SDD ledger — plan: docs/superpowers/plans/2026-09-19-readability-drafts-invites.md

原始分支 main，已有认证实现与用户文档未提交；全部保留。

Ruling: 沿用用户要求自主执行、无需提问，按书面方案实施。已有账号保留密码登录，新用户注册必须邀请码；若需要每次登录都填邀请码，会降低可用性而不增加人数控制。
Ruling: 在现有工作目录增量修改，不创建会丢失未提交认证实现的 worktree，不提交、不删除技能工作文件。报告保存在项目 docs。

Pre-flight: Task 1 与 Task 3 共享 cli.py，先由邀请码任务修改，生命周期稍后顺序合入；邀请模型用独立文件，不与 Project 模型冲突。前端 api.ts/AuthContext 属 Task 1，Task 3 仅在它完成后增加草稿接口。Task 2 只修改主题和外观。

观察：浏览器 DPR=1，body 14px Georgia/Songti SC/SimSun；applyTypography 将正文宋体覆盖到 --font-ui。新建流程先创建 Project 再调用两个草稿接口，无持久状态，提供跳过规划入口；子调用各自清 busy，Promise.all 后无条件成功提示。

Task 2: 主题回归先出现4个预期失败（界面字体被正文覆盖），分离字体后 theme 14/14通过；Appearance 旧断言适配独立字体与新默认。浏览器宽1004时原工作台min-width1100导致横向裁切，增加窄窗口下把工具栏放到正文下方的布局，不缩小整页字体。

Task 3: tests/test_project_creation.py 六个测试首先全部失败，分别复现状态缺失、设定后仍放行、空大纲确认、未确认设定确认大纲、无法恢复、worker不拦草稿。实现后隔离库增量迁移成功，6/6通过。网关带 write=true 在配额扣减/入队前验证；worker二次检查。

Task 3: 前端 NewProjectPage 两个失败测试复现无法恢复、并行提案中提前解锁和错误成功提示；实现持久上下文恢复与父流程统一busy后正在验证。正式书架与草稿分区测试已补充。

Ruling: 旧有正文与大纲项目标记 legacy_ready 保持兼容；只在首次字段迁移时把无任何章节且无大纲的旧项目标记 draft。旧记录不删除，若旧书实际准备完成但从未存大纲，需补充确认后继续。

Final: 注册事务构造响应失败回滚测试先RED后GREEN；完整后端最终752 passed/5 xfailed。Web最终219 passed，lint/build exit0；Go vet/test通过且无SKIP。隔离HTTP39项通过。增量迁移、并发迟到提案、ready非法更新和创建幂等的终审发现均已补测修复。

Final: 备份成功后仅更新本项目API/gateway/worker；3账号/8项目/45章及全部正文摘要不变，5 legacy_ready/3 draft。邀请码签发3枚，每枚一次7天，本地私有文件保存。浏览器确认实际字体、分区及窗口布局。风险与验收详见 docs/ACCEPTANCE-2026-09-19.md。

Ruling: 使用verification-before-completion与finishing-a-development-branch收尾，遵从用户不提问、不破坏约束，保留main工作树现状，不提交/合并/推送/删除任何分支或工作文件。
