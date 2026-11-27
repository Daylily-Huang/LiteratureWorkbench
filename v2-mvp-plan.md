# v2 MVP：单文档 RAG 问答实施计划

> 2026-07-29 制定。对应 `roadmap.md` v2 研究助手的最小可行版。
> 原则：**零新增外部依赖、复用既有出口审计与计费、单文档单轮问答先行**。

## 一、目标与非目标

### 目标（MVP 内）

- 在右侧"研究助手"栏对**当前打开的文档**提问，得到带页码引用的中文/英文回答
- 引用可点击跳转到对应 PDF 页面
- 全链路走既有 DeepSeek 出口审计与用量计费；DeepSeek 不可用时按翻译同款策略回退
- 索引自动跟随解析结果：重解析后自动重建

### 非目标（明确排除，留给 v2.x/v3）

- 跨文档问答、多轮对话历史、向量语义检索、网络检索、流式输出、对话持久化

## 二、技术选型（已验证）

| 决策点 | 选择 | 依据 |
|--------|------|------|
| 检索方式 | **SQLite FTS5 词法检索** | 已实测当前 SQLite 3.49.1 带 FTS5；单文档语料量小（一篇论文几万字），词法检索足够；零新依赖、离线可用。DeepSeek 无 embeddings API，本地向量模型要引入模型管理，超 MVP 范围 |
| 语料来源 | `pages` 表当前解析结果 | 阅读顺序修复后块序干净（7/18 实证），块带 `physical_page` 与 bbox，天然支持页码引用 |
| 答案生成 | 复用 `DeepSeekGateway` 审计通道新增问答方法 | 翻译链路已有：审计（`egress_audit`）、用量计费（版本化价格快照）、一次有界重试、qwen 回退——全部照搬 |
| 索引时机 | **首次提问时惰性构建**（同步，<1s） | 不动 worker/任务围栏；按 `(content_sha256, parse_run_id)` 判陈旧，重解析后自动重建 |
| 前端落点 | `Workbench.tsx` 助手侧栏（`assistant-placeholder` 处） | 占位文案即为此预留；注意 263/280 行两处重复 aside 需提取共享组件 |

## 三、数据模型（迁移 v07，仿 v01–v06 既有模式）

```sql
-- 块级 chunk：来自 pages.blocks 的合并文本，保留页码与块序用于引用定位
CREATE TABLE rag_chunks(
  id TEXT PRIMARY KEY,               -- uuid
  content_sha256 TEXT NOT NULL,
  parse_run_id TEXT NOT NULL,        -- 与 contents.current_parse_run_id 对齐判陈旧
  physical_page INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,      -- 页内顺序
  text TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX rag_chunks_content ON rag_chunks(content_sha256, parse_run_id);

-- FTS5 外接内容表：rowid 对齐 rag_chunks.rowid
CREATE VIRTUAL TABLE rag_chunks_fts USING fts5(
  text, content='rag_chunks', content_rowid='rowid'
);
```

- 分块策略：同页内按块顺序贪心合并至 800–1200 字符，页边界硬切（不跨页合并，引用即页码）
- 重建策略：提问时检查 `rag_chunks` 是否存在该文档当前 `parse_run_id` 的记录；缺失则事务内删除旧行 + 重建（幂等）

## 四、后端实现

### 4.1 新 feature：`features/research/`

| 文件 | 职责 |
|------|------|
| `chunker.py` | 从 `ParsedPage.blocks` 生成 chunk 序列（纯函数，可单测） |
| `retriever.py` | FTS5 MATCH 查询 → 取 top-k（默认 6）chunk，按 BM25 排序，附 `physical_page` |
| `service.py` | 编排：惰性索引 → 检索 → 组装 prompt → 调网关 → 解析引用 → 返回；镜像 `translation/service.py` 的重试/回退结构 |
| `repository.py` | rag_chunks 读写、陈旧判断 |

### 4.2 网关扩展：`DeepSeekGateway.answer_question(...)`

- 复用 `_send`（审计/计费/重试全继承）；prompt 结构：系统指令（"仅依据所给片段回答，标注 [p页码]"）+ 编号片段 + 问题
- 用量计费沿用版本化价格快照；qwen 回退作为降级（答案质量下降，前端标注"本地模型回答"）
- 解析回答中的 `[pN]` 标记 → 结构化 citations 数组返回

### 4.3 API：`api/research.py`（注册进 `app.py`，同款 CSRF/会话中间件）

```
POST /api/documents/{document_id}/ask
  请求: { "question": "..." }           # 1–2000 字符
  200: { "answer": "...", "citations": [{"physical_page": 1, "excerpt": "..."}],
         "provider": "deepseek"|"qwen", "usage": {...} }
  400: question_empty / question_too_long
  404: document_not_found
  409: parse_required                   # 文档无完成态解析
```

## 五、前端实现

- 新组件 `features/research/AssistantPanel.tsx`：提问输入框、回答卡片（引用渲染为页码芯片）、加载/错误态（含"本地模型回答"降级标识）
- 点击页码芯片 → 复用阅读器既有页跳转机制（与批注跳转同通道）
- `Workbench.tsx`：两处重复 aside 提取共享组件，`assistant-placeholder` 替换为新面板；保留"翻译与模型设置"按钮
- 空态引导：未解析文档显示"需先完成解析"

## 六、测试（仓库既有门禁，不动流程）

**后端**（`tests/research/` 新增）：
- chunker 纯函数：合并阈值、页边界硬切、空页/无块页
- retriever：FTS5 查询命中排序、无命中、中文 query 对英文语料的行为钉死
- repository：陈旧重建幂等（同 parse_run_id 不重建、新 run 触发重建）
- service：DeepSeek 成功 / 一次重试后成功 / 回退 qwen / 引用解析
- API 契约：400/404/409 各分支（仿 `test_parsing_api.py`）
- 迁移：v07 升级与幂等（仿 `test_migrations.py`）

**前端**：面板渲染、提问流（mock fetch）、引用点击回调、降级标识

**门禁**：后端 ruff / mypy / 全量 pytest；前端 tsc / vitest / build——与现状一致

## 七、里程碑与验收

| 里程碑 | 内容 | 验收 | 状态（2026-07-29） |
|--------|------|------|------|
| M1 | 迁移 v07 + chunker + retriever + repository | 新单测全绿；鼷鹿文档离线分块/检索人工抽查 | ✅ 18 个新单测全绿 |
| M2 | 网关问答方法 + service + API | API 契约测试全绿 | ✅ 后端 608 passed；ruff/mypy 全绿 |
| M3 | 前端 AssistantPanel + 页跳转 | vitest 全绿；build 通过 | ✅ 前端 102 passed；typecheck/build 通过 |
| M4 | 全门禁 + 线上实证 | 实例重启后对鼷鹿文档提问"银背鼷鹿在哪里被重新发现"，回答正确且引用页码可跳转；egress_audit 有对应记录 | ✅ 已实证（见下） |

**M4 实证记录**：实例重启后提问 "Where was Tragulus versicolor rediscovered and what evidence is reported?"，DeepSeek 回答正确（越南、相机陷阱首张照片），引用 [p1][p2][p7] 页码芯片；egress_audit 写入 `purpose=research_question, payload_kind=research_prompt, outcome=succeeded`；42 个 chunk 覆盖 8 页；纯中文无拉丁词元问题正确走 retrieval_only 罐装回答（7ms，不调云端）。

**v2.1 增补（2026-07-29）：查询扩展已上线**。中文问题首次检索无命中时，自动调一次 DeepSeek 做查询翻译/总览识别（`features/research/expansion.py`，审计用途 `research_query_expansion`）：总览类问题（"这篇讲了什么"）喂文档开头摘要块，其余翻译成英文检索词再走 FTS；local_only 模式不触发（不出本机）；扩展失败如实返回错误码。实证："解释下这篇文献的主题" → 正确中文摘要 + [p1] 引用，全程 ~5s。后端 622 passed。

**v2.2 增补（2026-07-29）：trigram 分词 + 引用精准高亮已上线**。迁移 v08 把 `rag_chunks_fts` 换成 trigram 分词器（中英文都是子串匹配）并清空旧 chunk；chunker 记录每个 chunk 的贡献块 `bbox_1000` 坐标（`rects_json`），引用 API 返回 rects，前端点引用芯片跳页后在 PDF 上按块高亮来源段落（`CitationHighlight`，脉冲闪烁提示）。中文问句现在直接命中中文语料（不再依赖扩展），如"暗针叶林的径级分布有什么特点？"→ 倒"J"形径级结构详答 + [p5][p7] 行级坐标。后端 632 passed，前端 105 passed。

**回滚策略**：v07 迁移只增不改；功能出问题时下线 API 路由 + 还原侧栏占位即可，数据库新表留置无害。

## 八、风险与备注

- **词法检索天花板**：中文问句检索英文语料依赖关键词重合（如物种学名、地名）。MVP 接受此局限；v2.x 升级路径：本地向量模型（fastembed/BGE，届时再评估依赖）
- **qwen 回退质量**：本地小模型做开放式问答质量有限，仅作可用性兜底并前端明示
- **成本**：每次问答 1 次 DeepSeek 调用（~2–4k tokens 上下文），既有计费快照覆盖
- **隐私**：问答把文档片段外发 DeepSeek——与翻译一致的既有出口策略，无需新授权面
