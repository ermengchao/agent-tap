# agent-tap

[简体中文](README_zh.md)

agent-tap brings Codex traffic captured by Surge into the original claude-tap web viewer. It reads local capture files as they grow and adds each completed model call to the dashboard, even when its WebSocket stays open.

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

## Source and tests

The web viewer and its supporting trace and storage code come from [claude-tap at b21d171](https://github.com/liaohch3/claude-tap/tree/b21d17157b674a0562e0ed08f4c4c607e8e5a56f), under the included MIT license. The original proxy and client launch code are outside this project.

```fish
uv run --no-editable pytest -q
uv run --no-editable ruff check src tests
```
