# 个人科研整体验收（2026-10-23）

范围按用户“个人科研自用，不对外卖；重点AI精读、多文献对比和知识图谱，及时清理临时件”执行。基础阅读、数据保全和现有正式备份一并保留。

| 要求 | 当前证据与结果 |
|---|---|
| 基础阅读、双栏及左右批注/助手跟随 | 通过；现有阅读流程及迟到响应/错误文献保护；原始阅读器文件与已验版本一致。 证据：`web/src/app/Workbench.test.tsx`, `web/src/features/reader/ComparisonPane.test.tsx` |
| AI选区精读与全文研读附原文依据 | 通过；核验选区、所有解析页、引句匹配、空白/未解析/超限拒绝；纯解析文字，不承诺图像理解或科学推论正确。 证据：`backend/tests/api/test_research.py`, `backend/tests/research/test_library_service.py` |
| 逐篇缓存、加篇增量、顺序语义与费用透明 | 通过；新增或变化篇全文，旧篇复用核验引句；精确缓存不调用API，新问题/新的合并回答可能计费。 证据：`backend/tests/research/test_ordered_study.py`, `backend/tests/research/test_library_service.py` |
| 研读状态、旧分析/依据查看、手动重读和研究笔记 | 通过；查看/导出不调用模型，先检查后明确重读；笔记草稿/自动保存/多窗口冲突保护。 证据：`web/src/features/research/StudyHistory.test.tsx`, `web/src/features/research/ResearchNotebook.test.tsx` |
| 多文献对比与完整证据矩阵 | 通过；最多8篇有序范围、跨历史查询、分页和完整筛选导出；历史和未覆盖内容保持标识。 证据：`backend/tests/api/test_evidence_matrix.py`, `web/src/features/research/EvidenceMatrix.test.tsx` |
| AI支持/分歧/方法候选先审核再入图 | 通过；双侧当前原文及本次提供范围验证，原组第三篇变化也阻断，未知请求不自动付费重试；真实PDF闭环沿用已绑定阶段证据。 证据：`backend/tests/research/test_full_text_relations.py`, `backend/tests/research/test_study_relations.py` |
| 知识图谱联动、检索、筛选和带依据导出 | 通过；按引句/问题/说明检索，一跳上下文、组外隔离、关系/来源状态筛选和版本保留；来源变化跳转确认。 证据：`web/src/features/research/KnowledgeGraph.test.tsx`, `web/src/features/research/graphModel.test.ts` |
| 本地个人使用、数据保全、稳定入口 | 通过；只绑定回环，桌面入口和书本图标存在；已有正式恢复演练证据，当前源文献/缓存/备份独立保护。 证据：`docs/stable-address-2026-10-23-candidate.json`, `docs/backup-import-2026-10-23-candidate.json` |
| 清理确认无用测试件与可再生缓存，保留链接原件 | 通过；历史批量清理已完成；固定测试根、已解除占用的空日志和Vite开发缓存已独立核验并清理。 证据：`docs/cleanup-2026-10-23.json`, `docs/full-text-relations-2026-10-23-candidate.json` |

最终后端845通过、2项按项目配置排除；前端沿用最新288项全回归和真实Chrome交互记录，无前端代码继续变更。Ruff通过、Mypy91源文件通过。清除了一条过时类型忽略注释，前后Python AST完全一致，无运行逻辑变化。102个阶段文件精确匹配最新阶段证据，另1个仅上述注释变化；当前212部署资源与实际首页资源一致。

正式数据库15张保护业务表、源PDF、批注恢复文件、翻译缓存与正式备份已核对；最终删除后结果见伴生JSON。当前219个PDF登记路径，其中191个实际存在；既有缺失路径保留，不把登记数量冒称可阅读PDF数量。

查看与导出、免费准备检查不调用模型；缓存命中不新增请求，复用分析生成新回答仍可能计费。解析全文不是图片/表格视觉理解，引句匹配和用户确认均不代表科学推论已经验证。真实付费模型推断质量、长期运行和全新机器分发没有由本次流程测试证明。

使用入口：[个人科研使用指南](personal-research-guide.md)，网站 http://127.0.0.1:59978/，桌面“文献工作台”书本图标。全部原始证据、阶段引用与保护文件清单见同名JSON。

最终清理已执行，独立首审4次＋定向复验4次，GO，未扩大共享10分钟窗口。三个指定根均不存在：D:/_scratch/2026-10-23-study-review-bridge、D:/LiteratureWorkbench/web/node_modules/.vite、D:/_scratch/2026-10-23-personal-final-audit。仅解除链接本身，未进入链接目标。删除实际计数因首次核对失败未保留，不使用Audit估计值冒充释放量。

清理后219个登记PDF路径（191个实际存在）、33个正式备份文件、19个批注备份文件、212个网站资源和3个图标均按基线完整哈希核对；已登记但原本缺失的路径保持缺失。15张业务表主键集合和非时间字段全部一致，979处基线时间字符串被PowerShell JSON往返改写了时区，独立核对均为同一时刻，其他差异0；不能声称原始时间字符串全等。数据库完整性/外键检查通过，文献、图谱、研究记录、研读缓存和健康5个接口200，未调用模型。

本轮个人科研升级与及时清理目标已完成。AI精读、逐篇缓存与增量比较、带原句的关系审核和图谱检索流程已安装；真实模型推理的科学正确性仍需你在使用中核对。对外售卖、新机器分发与商业许可不属于用户指定的本轮自用范围。
