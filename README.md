# Literature Workbench (文献工作台)

A local-first, privacy-focused desktop literature reading, translation, and AI research workbench for Windows.

---

## 🌟 Features

- **📖 High-Performance PDF Reader**
  - Continuous scrolling with virtualized neighborhood page rendering.
  - Fast mathematical page index computation with zero DOM layout thrashing.
  - Precise text selection overlay, cross-column drag disambiguation, and coordinate-faithful annotation overlays.
  - Native PDF annotation support (highlight, underline, comment) with incremental saving and crash-recovery journaling.

- **🤖 AI Research Assistant & RAG**
  - **Single Document & Whole-Library Q&A**: Fast FTS5 trigram full-text search across documents and corpora with Chinese/English mixed tokenization.
  - **Trustworthy Fragment Citations**: Strict fragment-level verification (`[pX]`, `[rN:pX]`) with bounded `bbox_1000` flash highlighting and multi-chunk collision disambiguation.
  - **Harden Prompt Trust Boundary**: Retrieved chunks are strictly separated into untrusted evidence channels using XML tags (`<source_fragment>`).
  - **Dual-Engine Architecture**: DeepSeek Cloud API with automatic local `Qwen` (llama.cpp) fallback and process management.

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
- Node.js 20+

### 1. Frontend Setup

```powershell
cd web
npm install
npm run build
```

### 2. Backend Setup

```powershell
cd backend
uv pip install -e ".[dev]"
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
