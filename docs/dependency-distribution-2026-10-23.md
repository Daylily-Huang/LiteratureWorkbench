# 分发依赖与许可证据（2026-10-23）

本轮完成安装来源和构建许可材料的具体修正，未安装AI关系分析候选、未重启正式服务、未改变其数据库/文献/密钥、未调用付费模型。整体验收及商用目标仍未完成。

## 已改进

- `backend/requirements.lock`中8个间接依赖对齐本机已存在的有效版本元数据，未实际安装或升级包。保留所有10个直接依赖及开发依赖声明。CI安装命令现在明确读取锁文件；避免只安装pyproject时解析到另一批间接版本。
- README新检出安装过程先创建Python3.12虚拟环境，显式使用该解释器和锁文件；前端改用npm ci，每段操作回到项目根目录。上一轮Node24配置保持。
- 前端现有资源复制步骤新增React/ReactDOM/scheduler/PDF.js上游LICENSE及JSON校验清单，且继续保留PDF.js的CMap、字体、WASM和ICC许可。可传显式输出路径用于隔离构建，默认路径不变。此项普通可逆打包改动不触发新的独立审核，也不替代原AI候选验收。

## 实际检查结果

- 47个后端锁定包：当前Windows运行依赖闭包31个，其余为锁定但不在当前运行闭包。184个前端锁定条目（含跨平台可选条目）：16个非dev，123个在此检出环境有package.json。未安装的跨平台条目保留锁声明，不虚构许可正文。
- 本机8个有效版本漂移已修复锁定；67项可读取的项目/包声明约束无冲突，CI YAML语法、使用锁文件及Node24配置通过。pip check返回No broken requirements found，但不能据此忽略缺失元数据。
- 隔离输出14份浏览器许可文件均与上游字节及SHA256一致；200份PDF.js资源（CMap169、标准字体16、WASM13、ICC2）全部哈希一致；构建脚本Node语法检查通过。未写现有web/dist。
- 收集187份本地实际许可/NOTICE等文件；压缩档CRC检查、嵌入清单及逐文件字节/SHA256全部核对。归档仅依赖许可文件和清单，未包含PDF、研究笔记、数据库或API密钥。

## 明确缺项

- websockets16.1目录存在，但dist-info的Name/Version/METADATA/RECORD不可用，许可证目录为空；模块version.py写16.1，但不能替代完整安装元数据。该包未被称为版本核验通过，也未在运行环境中重装。后续应在隔离干净环境重建并验证HTTP/必要时WebSocket启动，再决定正式修复；单次pip check不足。
- 4个已安装前端包未找到根许可文件：@napi-rs/canvas-win32-x64-msvc、@rolldown/binding-win32-x64-msvc、saxes、stackback。库存中标明declared_metadata_only；声明不等同已收齐正文或原生库NOTICE。不能把开发依赖或未安装OS条目当作实际浏览器打包内容。
- PyMuPDF的COPYING文件仅含双许可声明，不是完整AGPL文本或商业授权证明。当前README保留Internal Research Use；没有选择项目开源/商用许可、购买授权、对外发布或更换PDF引擎。
- Python/Node/Windows运行时再分发、原生组件嵌入库、模型/llama.cpp/OCR、图标来源、实际发布包裁剪与NOTICE义务仍未完整核实。新机器安装、远端CI和真实供应商长期质量未验证。

## 产物

- 结构化清单：`docs/third-party-dependencies-2026-10-23.json`（锁定/安装版本、当前运行角色、缺失状态、文件来源与哈希）。
- 许可证据包：`docs/third-party-license-evidence-2026-10-23.zip`，309304字节，SHA256 `b4f76d01004926ee0383b4d0d0093782bbf7a1119cc6231a2ed6777592bc74da`。归档含同一清单及187份原始文件，不是可发布的应用安装包。

本轮不重置关系分析的审核预算；该候选9文件维持NO-GO与未安装状态，等待用户明确追加有界修复复验。所有许可证字段均是上游声明事实，不构成整体合法分发或全面商用通过结论。

收尾只读检查：正式59978健康200、仍index-l6s5-6eY.js；关系分析9文件冻结哈希无变化，未安装。依赖库存输入哈希和ZIP一致。清理D:/_scratch/2026-10-23-dependency-notices经绝对路径核对后被自动审批拒绝（blocked by policy），未绕过，临时资源保留；无临时运行服务。
