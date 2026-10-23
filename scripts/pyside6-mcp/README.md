# Screenshot-safe developer MCP

This opt-in tool fixes the owning `com55/pyside6-mcp` implementation at
`6d3a4f25e1f98378204f1340f477f9dda8c89dfe` (version 0.1.0). The patch includes
source and offscreen regressions; it does not add a VNTTS runtime dependency.
Live control IDs follow the native Qt object across temporary Python wrappers;
destroyed widgets and restarted applications cannot reuse an earlier ID.
`type_text` appends at the cursor. Use `press_key("Ctrl+A")` before typing to replace
text; `press_key` accepts named keys, a character or one portable Qt combination,
and rejects unsupported sequences without typing them. Each click dispatches once.
Shutdown acknowledges the quit request before application cleanup and separately
checks the real Qt process exit; a process that remains alive keeps its session.
Status follows the Qt PID even after its launcher exits. Exit/return codes are
`null` when only the separate launcher's wait status is available.
Windows liveness uses a read-only process handle wait. Mutating requests are never
automatically retried; a lost response reports that the action may have completed.
Inspect current app state before deciding whether to repeat it. Safe GET requests
retain transient-error retries.
Action responses distinguish accepted `queued` and `dispatched` (HTTP 202) from
`completed` and `error`. A modal action acknowledges dispatch from its nested Qt
loop while the original `QAction.trigger()` remains active. `completed` describes
that invocation returning, not background application work. Inspect app state
before repeating an incomplete action; late invocation errors are correlated by
`invocation_id` in the existing logs. Qt signal-handler exceptions that Qt itself
swallows remain application log/excepthook concerns.

From VNTTS, with Git, uv and Python 3.11+ installed:

```sh
python3 scripts/install_pyside6_mcp.py --source-dir /absolute/new/pyside6-mcp-capture --install
```

The parent must exist. Both the source path and adjacent `.git` bare repository
must be new; existing checkouts are never reset. The installer creates a dedicated
worktree at the pinned commit, checks/applies the patch, runs focused offscreen
Qt/HTTP/MCP regressions, builds a wheel, then replaces only the `pyside6-mcp` uv
tool. Without `--install`, it only prepares, tests and builds. Failed preparation
leaves the source available for inspection; use a new path to repeat. Build
provenance (upstream, patch and wheel SHA256) is saved in `dist/capture-build.json`.
No upstream branch or PR is published.

Keep the existing MCP config command pointing to the uv tool's `pyside6-mcp`
executable. Restart the client/MCP server and relaunch bridged apps to activate
it: an already running server or temporary injected bridge retains old code.
No existing application is stopped automatically. The updated server rejects
legacy bridges before screenshot. For each capture, read `get_widget_tree`, then
call `screenshot(widget_id=<observed ID>, pid=<app PID>)`. Omitting widget_id is an
error. IDs are process-specific; reacquire them after restart. Hidden, closed,
minimized, missing or stale targets never fall back to any other window or desktop.

Successful MCP responses retain PNG image content and add JSON text plus `_meta`
capture provenance. The bridge response retains `image` and adds `capture`.
Metadata is generated on the Qt capture path: correlation ID, UTC request/capture
times, owner PID/application, window/widget IDs and title, global logical bounds,
physical output dimensions, `QWidget.grab` method, isolated-window/widget scope
and success/failure. Failed captures carry an error code and no image. Metadata
logs exclude image/base64 and widget/dialogue text; only the selected window title
is recorded. This is Qt-rendered client content, without native decorations or
external/native surfaces; Windows compositor/video acceptance remains separate.
