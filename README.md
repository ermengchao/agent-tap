# agent-tap

[简体中文](README_zh.md)

agent-tap brings Codex traffic captured by Surge into the original claude-tap web viewer. It reads local capture files as they grow and adds each completed model call to the dashboard, even when its WebSocket stays open.

Inspired by [claude-tap](https://github.com/liaohch3/claude-tap).

Requires macOS, Surge Mac 6.11 or later with a trusted MITM CA, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/). Enable the [Surge HTTP API](https://manual.nssurge.com/tools/http-api.html) on a loopback listener and use its configured port and key.

```fish
set -gx AGENT_TAP_SURGE_API_URL http://127.0.0.1:6171
read -sx -P 'Surge HTTP API key: ' AGENT_TAP_SURGE_API_KEY
uv run --no-editable agent-tap capture
```

Use Codex App or Codex CLI normally. The adapter recognizes ChatGPT-login traffic at `chatgpt.com/backend-api/codex/responses` and API-key traffic at `api.openai.com/v1/responses`, via WebSocket or HTTP/SSE. It reads the active capture under `$TMPDIR/Surge Capture`. Use `capture --capture-dir PATH` to select a different root. Surge's existing capture filters and limits still apply.

The adapter reuses switches that are already on. On startup it adds any missing hostname to the editable MITM section and turns on MITM and HTTP Capture when needed. An explicit disable or exclusion for either hostname stops startup. Turning HTTP Capture on or off reconnects existing traffic. Press **Ctrl+C** to stop and restore the changes made by this run.

If the process was killed or the Mac restarted, run `uv run --no-editable agent-tap restore` with the same API address and key before starting another capture. If the profile has changed, switch back to the original profile before restoring.

Run `uv run --no-editable agent-tap viewer` to browse history without connecting to Surge. Both viewer commands accept `--port` and `--no-open`. The SQLite database defaults to `$XDG_DATA_HOME/agent-tap/traces.sqlite3` or `~/.local/share/agent-tap/traces.sqlite3`. Set `AGENT_TAP_DB` to choose a different database.

The local database stores prompts and model output. The adapter omits authentication headers and only binds the web viewer to the loopback interface.

## How capture works

1. **Discover the capture and active requests.** agent-tap queries the Surge HTTP API at `/v1/capture/sessions` for the active session and `/v1/requests/active` for metadata such as the process, headers, and connection state. It combines that metadata with `model.json` when the file is available.
2. **Read request and response bodies from disk.** agent-tap scans `$TMPDIR/Surge Capture/<session>/Requests/` and reads new bytes from each request's `request.dump` and `response.dump` by tracking file offsets. The request folder name corresponds to Surge's `key`. The HTTP API provides metadata, and the dump files provide the bodies.
3. **Decode, store, and display.** agent-tap decodes WebSocket or HTTP/SSE data and writes a trace to SQLite when a model call ends. The dashboard updates from SQLite, including when Codex reuses the same open WebSocket for several calls.

## Source and tests

The web viewer and its supporting trace and storage code come from [claude-tap at b21d171](https://github.com/liaohch3/claude-tap/tree/b21d17157b674a0562e0ed08f4c4c607e8e5a56f), under the included MIT license. The original proxy and client launch code are outside this project.

```fish
uv run --no-editable pytest -q
uv run --no-editable ruff check src tests
```
