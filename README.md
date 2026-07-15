# Literature Workbench

Windows-only personal literature reading and translation application.

Runtime data belongs under `D:\LiteratureWorkbenchData`. The initial monitored
library is `D:\black-muntjac-project\papers` and is always treated read-only.

v0.1 contains PDF reading, local parsing/OCR, selection translation, DeepSeek,
and a verified local Qwen fallback. It does not yet contain RAG or web search.

## Windows launcher

Build the web app once, then use the fixed scripts:

```powershell
& 'D:\Hermes\node\npm.cmd' --prefix 'D:\LiteratureWorkbench\web' run build
& 'D:\LiteratureWorkbench\scripts\Start-LiteratureWorkbench.ps1'
& 'D:\LiteratureWorkbench\scripts\Stop-LiteratureWorkbench.ps1'
```

The launcher binds only `127.0.0.1`. It prefers port `58380`, records the
actual loopback port and exact supervisor/worker identities under
`D:\LiteratureWorkbenchData\runtime\launcher`, serves `web/dist` from the same
origin, and opens a one-use `/bootstrap#token` URL. A second start validates and
reuses the running instance. Stop uses the authenticated local control path and
never kills processes by name.

Install the optional desktop shortcut with:

```powershell
& 'D:\LiteratureWorkbench\scripts\Install-DesktopShortcut.ps1'
```

## Opt-in live v0.1 smoke

The automated suite uses only temporary data and fake providers. Before the
controller runs the real smoke, confirm whether `D:\LiteratureWorkbenchData`
already exists and capture a stable hash/size/mtime manifest of the approved
source PDF library. Then launch through `scripts\Start-LiteratureWorkbench.ps1`,
exercise one PDF selection and the configured provider, optionally exercise the
dedicated Qwen fallback on `127.0.0.1:18080`, stop through
`scripts\Stop-LiteratureWorkbench.ps1`, and compare the source manifest exactly.
The launcher does not start Qwen until a translation actually requires it.
