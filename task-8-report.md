# Task 8 交付报告：PDF 解析链路第三轮加固

日期：2026-07-15

## 交付结论

本报告累计覆盖 Task 8 第二、三轮审查修复。实现及测试仅使用临时目录、合成 PDF 和假的 MinerU runner；未调用真实 MinerU、未读取真实论文、未创建或写入 `D:\LiteratureWorkbenchData`。

## 已完成项

- `parse_pdf` 入队与 `parse_request_revision` 分配置于同一事务；旧请求在新 revision 分配后不能再启动或发布。
- 暂存复制接入任务取消检查；取消、复制中断及 checkpoint 失败时，只通过已持有的文件能力清理本次拥有的 `.part`、`.pdf` 和 journal。
- MinerU 工作树封存时递归固定目录/文件句柄，拒绝 reparse point 与硬链接文件，并生成包含文件身份、大小和 SHA-256 的 `manifest.json`。
- 最终结果通过句柄固定的源文件复制生成；发布指针绑定 `manifest_digest`，发布前再次验证结果树。
- staging、work 与 promotion journal 使用校验和 append-only frame 和句柄 flush；末尾 torn frame 不会覆盖最后一个完整状态。
- create_run 后立即持久 checkpoint；work journal 绑定 content/run/source job/revision/input stem，前置失败只在句柄、绑定和全树安全重验后精确清理。
- promotion journal 持久化 `copying`、`promoted`、`output_recorded`、`db_completed`、`published` 状态；恢复可从数据库 `source_job_id + request_revision + running status` 发现 run，不依赖 job checkpoint，也不重跑 MinerU。
- manifest 与 promotion journal 共同绑定 input stem、source job 和 revision；发布前递归重枚举，文件及目录集合必须与封存集合严格相等，并拒绝新增 reparse point。
- staging rename 后 checkpoint 前崩溃时，即使 checkpoint 仍指向 `.part`，也可通过同 attempt journal、`.pdf` 身份和 hash 恢复；证据不足时保留现场。
- copying 故障通过仍持有的文件和目录句柄逆序清理 partial result 与 promotion journal；未知对象不做路径式删除。
- Windows 长路径统一使用 `\\?\` Win32 路径拼写，覆盖深层结果树的创建、扫描和重新打开。
- Windows Job Object 实测可并发排空大于 64 KiB 的 stdout/stderr，保留各自最后 64 KiB；取消后派生进程无残留。
- 页面文本 API 改为 repository 单连接、单 SQL 快照读取，同时区分 `document_not_found` 与 `page_not_found`。

## 验证结果

- 后端：`385 passed, 2 skipped`（跳过项为显式 opt-in 测试）。
- Ruff：`All checks passed!`
- mypy：`Success: no issues found in 48 source files`
- Web Vitest：`6 passed`，共 `25 passed`
- Web production build：成功。
- `git diff --check`：无空白错误。
- `D:\LiteratureWorkbenchData`：不存在。

## 已知非阻塞提示

- FastAPI 测试仍显示现有 `starlette.testclient` / `httpx` 弃用警告。
- Vite 构建仍显示现有主 chunk 大于 500 kB 的性能提示；不影响本轮正确性与安全边界。
