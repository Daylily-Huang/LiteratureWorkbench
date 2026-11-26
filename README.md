# Literature Workbench (文献工作台)

A local-first, privacy-focused desktop literature reading, translation, and AI research workbench for Windows.

个人自用入口与完整流程见[个人科研使用指南](docs/personal-research-guide.md)：全文/增量研读、缓存复用、双栏对照、原文依据审核和知识图谱。

---

## 🌟 Features

- **📖 High-Performance PDF Reader**
  - Continuous scrolling with neighborhood canvas rendering on virtual scroll viewport.
  - Fast mathematical page index computation with zero DOM layout thrashing.
  - Precise text selection overlay, cross-column drag disambiguation, and coordinate-faithful annotation overlays.
  - Native PDF annotation support (highlight, underline, comment) with incremental saving and crash-recovery journaling.

- **🤖 AI Research Assistant & RAG**
  - **Single Document & Whole-Library Q&A**: Fast FTS5 trigram full-text search across documents and corpora with Chinese/English mixed tokenization.
  - **Trustworthy Fragment Citations**: Strict fragment-level verification (`[rN pM]`, `[dN rM pK]`) with bounded `bbox_1000` flash highlighting and multi-chunk collision disambiguation.
  - **Harden Prompt Trust Boundary**: System instruction (`role=system`) defines the boundary while retrieved evidence is safely demarcated with `<source_fragment>` XML tags.
  - **Dual-Engine Architecture**: DeepSeek Cloud API with automatic local `Qwen` (llama.cpp) fallback and cross-process lock lifecycle management.

- **🈳 Academic Translation**
  - Inline selection translation with sentence-level context preservation.
  - History tracking and term lookup.

- **🔒 Local Security & Windows Hardening**
  - Strict loopback binding (`127.0.0.1`), one-time bootstrap tokens, origin guards, and CSRF token verification.
  - Windows file identity verification (volume ID + file ID) and single hard-link write protection.
  - Secure credential storage using Windows Credential Manager.

---

## 🏗️ Architecture

```text
LiteratureWorkbench/
├── backend/                  # FastAPI 0.139 + Python 3.12 Backend
│   ├── src/literature_workbench/
│   │   ├── api/              # HTTP routers & lifecycle supervisor
│   │   ├── db/               # SQLite migrations & async task queue
│   │   ├── features/
│   │   │   ├── annotations/  # PDF annotation service & crash recovery journal
│   │   │   ├── library/      # File watcher, scanner, repository & security
│   │   │   ├── models/       # DeepSeek client & Qwen local process manager
│   │   │   ├── parser/       # PyMuPDF reading order & text normalizer
│   │   │   ├── research/     # FTS5 retriever, chunker & RAG service
│   │   │   └── translation/  # Selection translation pipeline
│   │   └── security/         # Windows ACL, path safety & session tokens
│   └── tests/                # 660+ automated pytest suite
├── web/                      # React 19 + TypeScript + Vite Frontend
│   └── src/
│       ├── app/              # Workbench layout & three-column shell
│       └── features/
│           ├── annotations/  # Annotation sidebar & tools
│           ├── dashboard/    # Workspace overview & quick entry
│           ├── library/      # Document tree & file drop manager
│           ├── reader/       # Virtualized PDF reader & selection overlay
│           ├── research/     # AI assistant panel & citation navigator
│           └── settings/     # DeepSeek credentials & billing estimate
└── scripts/                  # PowerShell Windows service & startup scripts
```

---

## 🚀 Quickstart

### Prerequisites

- Windows 10/11
- Python 3.12 (with [uv](https://docs.astral.sh/uv/))
- Node.js 24.x (the frontend dependency set requires a newer runtime than Node 20; CI uses Node 24)

For a fresh checkout, run these steps from the repository root. Each setup block returns to that root.

### 1. Frontend Setup

```powershell
cd web
npm ci
npm run build
cd ..
```

### 2. Backend Setup

```powershell
cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe --requirement requirements.lock -e ".[dev]"
cd ..
```

### 3. Launch Application

Use the provided PowerShell scripts to start and stop the local instance:

```powershell
# Start Literature Workbench
& '.\scripts\Start-LiteratureWorkbench.ps1'

# Stop Literature Workbench
& '.\scripts\Stop-LiteratureWorkbench.ps1'
```

Install the optional desktop shortcut:

```powershell
& '.\scripts\Install-DesktopShortcut.ps1'
```

---

## 🧪 Testing & Quality Assurance

### Backend Validation

```powershell
cd backend
# Run linting
.venv\Scripts\ruff.exe check src tests

# Run type checks
.venv\Scripts\mypy.exe src

# Run test suite
.venv\Scripts\python.exe -m pytest
```

### Frontend Validation

```powershell
cd web
# Type check
npm run typecheck

# Run unit tests
npm test

# Build production bundle
npm run build
```

---

## 📄 License

Internal Research Use.

Third-party components retain their own licenses. Frontend builds copy upstream browser-package licenses to `web/dist/licenses/` and preserve the PDF.js asset notices in `web/dist/pdfjs/`. This does not change the project license above.

The dependency inventory and collected license evidence are recorded in [the dependency audit](docs/dependency-distribution-2026-10-23.md). The snapshot records missing metadata and incomplete notices; it is not commercial release approval.
