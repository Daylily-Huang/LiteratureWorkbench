# v3：全库问答 + 网络检索实施计划（Milestone 3 & 4）

> 状态：**M3 全库问答已实现并完成稳定性收尾**（2026-07-29 初版，2026-09-05 修复 R1–R5 审查缺陷）；**M4 网络检索待规划**
> 里程碑对照：统一以 `roadmap.md` 的 M1–M5 里程碑口径为准，本文档 v3.1 对应 M3，v3.2 对应 M4。
> 前置：M2 单文档 RAG 问答已上线（见 `v2-mvp-plan.md`）
>
> 验收记录：全量后端/前端测试、静态检查（Ruff、Mypy strict、tsc）、构建全部通过。
> 实证：英文问"鼷鹿重发现"命中《Camera-trap evidence…》并正确引用 [d1 p1][d1 p7]；中文问"红外相机应用"经查询扩展跨 3 篇文献作答；审计 `research_question` 以 `document_id=NULL` 落地。
> 修复：索引覆盖从 `valid_pdf` 放宽到 `candidate_pdf`，并在 2026-09-05 修复跨篇引用生命周期、高亮隔离及覆盖统计一致性。

## 一、目标与非目标

**v3.1 目标**：提问不再限定当前文档——检索整个文献库，回答标注"哪篇文献、第几页"，点击引用可跨文档跳转并高亮来源段落。

**v3.2 目标**：同一提问入口可同时检索网络，网页来源与文献库来源分栏标注、可区分、可溯源（URL + 抓取时间）。

**非目标**：
- 不做多轮对话/会话记忆（每次提问独立；追问留给 v3.x）
- 不做向量检索升级（trigram 词法检索继续服役，向量仍留 v3.x 评估）
- 不动 worker 围栏与解析流水线结构（索引触发方式见三.2 的决策）
- v3.2 不做网页全文入库（网页片段只做当次回答的上下文，不写 `rag_chunks`）

## 二、现状基础（v2 已验证，直接复用）

| 能力 | 位置 | v3 复用方式 |
|------|------|------------|
| trigram FTS5 全库索引 | `rag_chunks` + `rag_chunks_fts`（一张表装全部文档） | 跨库检索就是一次 JOIN，天然具备 |
| 块级坐标 | `rag_chunks.rects_json`（bbox_1000） | 跨文档引用高亮不变 |
| 查询扩展 | `research/expansion.py`（OVERVIEW / 关键词翻译） | 全库问题照搬；OVERVIEW 语义改为"最近打开/全部文献的总览"（见风险节） |
| 问答编排 | `research/service.py`（重试一次、qwen 回退、罐装回答） | `ask_library` 沿用同一套语义 |
| 出口审计 | `egress/gateway.py`（purpose/payload_kind 自由文本） | 新增 purpose 即可，无 schema 变更 |
| 前端问答面板 | `AssistantPanel.tsx` + 引用芯片 + 跳页高亮 | 加作用域切换与跨文档引用芯片 |
| 跨文档打开 | `Workbench.openDocument`（URL 驱动） | 配合"待跳转引用"状态实现跨文档高亮 |

## 三、v3.1 全库问答设计

### 1. 检索

`RagChunkRepository` 新增：

```sql
SELECT c.id AS chunk_id, c.physical_page, c.text, c.rects_json,
       d.id AS document_id, d.display_name,
       bm25(rag_chunks_fts) AS score
FROM rag_chunks_fts
JOIN rag_chunks AS c ON c.id = rag_chunks_fts.chunk_id
JOIN documents AS d ON d.content_sha256 = c.content_sha256 AND d.status = 'valid_pdf'
WHERE rag_chunks_fts MATCH ?
ORDER BY score
LIMIT ?
```

- BM25 基于全表统计量，IDF 天然是全库口径，跨文档可比
- 同一 content 被多个文档引用时按 `documents.id` 去重（取任一注册文档）
- 取 top-k 后**每篇文档最多保留 3 个片段**，避免一篇长文挤掉其他文献；总片段 ≤ 8

### 2. 索引覆盖（关键决策，推荐 B 方案）

全库检索的前提是"每篇已解析文档都有 chunk"。当前索引是**提问时按文档惰性构建**的，直接做全库检索会漏掉从未提问过的文档。两个方案：

- **A. 解析完成即索引**：解析流水线收尾时同步分块。最及时，但动 worker 围栏，解析失败的回滚路径要跟着改
- **B. 惰性全库扫描（推荐）**：首次全库提问时，扫描所有 `current_parse_run_id` 非空且索引陈旧/缺失的文档，逐篇同步重建（每篇 <1s）；之后每次全库提问只处理增量（新解析/重解析的文档）。50 篇文献首次约 10–30s，前端显示"正在为 N 篇文献建立检索索引…"进度；不动 worker，失败按篇跳过并计数

选 B：与 v2 惰性哲学一致，围栏不动，可观测（进度可报）。若将来文献量 >200 篇再评估 A。

### 3. 服务与 API

- `LibraryResearchService.ask_library(question)`（或 `ResearchService` 加方法）：校验 → 增量索引扫描 → 检索（无命中先走扩展，同 v2）→ 组装多文档 prompt → DeepSeek（重试/回退语义不变）
- prompt 片段标注改为 `[d1 p5]` 形式（文献序号 + 页码），文末附文献清单（d1=《标题》），要求模型按 `[dN pM]` 引用
- `POST /api/libraries/ask`，请求 `{question}`（同现有边界：≤2000 字、禁额外字段）
- 响应：`{answer, citations:[{document_id, display_name, physical_page, excerpt, rects}], provider, model, fallback_reason, indexed_documents}`（`indexed_documents` 用于前端提示覆盖度）

### 4. 前端

- `AssistantPanel` 顶部加作用域开关：**当前文献 / 整个文献库**（无打开文档时默认文献库）
- 引用芯片显示 `《标题》第 N 页`（标题超长截断）；当前文献的引用行为不变
- 点击**其他文献**的引用：`openDocument(documentId)` 切换文档后跳页并高亮——Workbench 增加 `pendingCitation` 状态：文档加载完成（`document` 就绪且 `initialPage` 生效）后一次性应用并清除；修复当前"切文档必清高亮"逻辑与之共存
- 文献库模式下展示覆盖度提示（"已检索 N 篇文献"）

### 5. 测试

- 跨库检索：两篇文档种不同关键词，命中归属正确、每篇限 3 片段、BM25 排序合理
- 增量索引扫描：陈旧/缺失/最新三种状态混合，只重建该重建的
- prompt 组装：`[dN pM]` 标注与文献清单正确；模型乱标 dN 时引用过滤（只保留清单内序号）
- API 契约：边界、错误码、`indexed_documents` 字段
- 前端：作用域切换、跨文档芯片点击回调、pendingCitation 应用逻辑

### 6. 里程碑

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| M1 | 跨库检索 + 每篇限流 + 归属 | 单测全绿 |
| M2 | 增量索引扫描 + service + API | 契约测试全绿 |
| M3 | 前端作用域开关 + 跨文档跳转高亮 | vitest 全绿 |
| M4 | 全门禁 + 实证：用一个横跨两篇文献的问题验证归属与跳转 | 审计有记录、引用可跳 |

## 四、v3.2 网络检索设计（决策点先行）

### 决策点 1：搜索源（需你拍板）

| 选项 | 形态 | 说明 |
|------|------|------|
| Brave Search API | 官方 API，免费额度（约 2k 次/月），需注册 key | 独立厂商、合规清晰，推荐首选 |
| Tavily | 官方 API，免费额度（约 1k 次/月），为 RAG 场景设计（返回干净正文） | 最省事，但数据经第三方再加工 |
| SerpAPI（Google 结果） | 付费 API | 结果质量高但无免费层 |
| DuckDuckGo HTML 端点 | 无 key 免费 | 非官方、易变易限流，只作兜底 |

> 定价/额度以签约时官网为准。key 的存储复用现有 Windows 凭据仓（`SecretStore`），设置界面加一栏。

### 决策点 2：出口策略（安全硬约束）

- 搜索 API 域名进 `egress` 审计（新 purpose：`web_search`）；网页抓取逐 URL 审计（purpose：`web_fetch`，记域名与字节数，**不记正文**）
- 抓取防护：仅 http/https、拒绝内网/保留 IP 段（SSRF 防护）、单页 ≤2MB、仅 text/html、10s 超时、每问题最多抓 5 页
- `local_only` 模式：网络检索整个禁用（同翻译/扩展的既有语义）
- 设置界面加"网络检索"总开关，默认**关**（opt-in）

### 决策点 3：结果融合

- 文献库与网页**分栏而非混排**：prompt 分 `<library_fragments>` 与 `<web_fragments>` 两节，回答要求分别引用 `[dN pM]` 与 `[wN]`
- 网页正文提取：readability 式提取（去导航/广告），每页取前 ~1500 字符，截断处不引句子中段
- 网页引用：`{title, url, fetched_at, excerpt}`；前端芯片显示 `🌐 标题`，点击**外链打开**（系统浏览器）而非内嵌（避免 iframe 安全风险）

### 架构落点

```
web_search/provider.py   # Brave/Tavily 抽象，可插拔
web_search/fetcher.py    # 抓取 + SSRF 防护 + 大小/类型限制
web_search/extractor.py  # HTML → 正文（readability 式）
research/service.py      # ask_library 加 include_web 分支
```

- API：`POST /api/libraries/ask` 加可选 `include_web: bool`（默认 false；开关关闭时传 true 返回 409）
- 网页片段**不落库**、不进 `rag_chunks`，仅当次使用（隐私与存储都简单）

### 里程碑

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| M1 | 搜索 provider + 设置/凭据 + 审计 | provider 单测（mock HTTP） |
| M2 | 抓取器 + SSRF 防护 + 正文提取 | 防护用例全绿（内网地址必拒） |
| M3 | 分栏融合 + API include_web | 契约测试全绿 |
| M4 | 前端网页引用芯片 + 开关 UX + 实证 | 混合回答两类引用可区分可溯源 |

## 五、工作量与风险

- v3.1 约 1 天（M1–M4 各约 1/4），无外部依赖，风险低
- v3.2 约 2–3 天，风险集中在：搜索源条款变化、网页正文提取质量参差、混合回答的引用准确率（需实证调 prompt）
- 共有风险：全库检索放大"词法检索天花板"（跨库近义词不匹配）；向量升级仍是治本路线，v3.x 再评估
- OVERVIEW 在文献库模式下语义模糊（"总览什么？"）——v3.1 中文献库模式的总览类问题改为检索"摘要/abstract/结论"关键词 + 最近打开文档优先，在 M4 实证中校验

## 六、回滚策略

- v3.1 纯新增（新方法/新路由/新 UI 开关），出问题时隐藏作用域开关即可，现有单文档链路零影响
- v3.2 有设置总开关兜底；搜索源 key 可随时吊销，审计不留正文
