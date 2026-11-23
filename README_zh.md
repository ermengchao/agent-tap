# agent-tap

[English](README.md)

agent-tap 将 Surge 捕获的 Codex 流量送入 claude-tap 原版网页查看器。它持续读取本地抓包文件，每次模型调用完成后更新网页，同一条 WebSocket 保持打开也能继续记录。

需要 macOS、Surge Mac 6.11 或更新版本、已受信任的 Surge MITM CA、Python 3.11 或更新版本和 [uv](https://docs.astral.sh/uv/)。请在 Surge 中开启 [HTTP API](https://manual.nssurge.com/tools/http-api.html)，使用回环地址、本机配置的端口和密钥。

```fish
set -gx AGENT_TAP_SURGE_API_URL http://127.0.0.1:6171
read -sx -P 'Surge HTTP API key: ' AGENT_TAP_SURGE_API_KEY
uv run --no-editable agent-tap capture
```

随后正常使用 Codex App 或 Codex CLI。适配器识别 ChatGPT 登录的 `chatgpt.com/backend-api/codex/responses` 和 API Key 的 `api.openai.com/v1/responses`，支持 WebSocket 和 HTTP/SSE。默认读取 `$TMPDIR/Surge Capture`，可用 `capture --capture-dir PATH` 指定目录。Surge 原有的抓包过滤条件和会话限制仍然生效。

适配器复用已经开启的开关。启动时补充 MITM 列表里缺少的目标域名，并按需开启 MITM 和 HTTP Capture；如果目标域名被显式禁用或排除，则报错退出。切换 HTTP Capture 会让现有连接重连。按 **Ctrl+C** 停止，适配器会恢复本次修改。

如果进程被强制终止或 Mac 重启，下次采集前先用同一 API 地址和密钥运行 `uv run --no-editable agent-tap restore`。如果切换了 Surge 配置，先切回原配置再恢复。

运行 `uv run --no-editable agent-tap viewer` 可独立浏览历史。两个网页命令都支持 `--port` 和 `--no-open`。SQLite 默认位于 `$XDG_DATA_HOME/agent-tap/traces.sqlite3` 或 `~/.local/share/agent-tap/traces.sqlite3`，可用 `AGENT_TAP_DB` 指定其他数据库。

本地数据库保存提示词和模型输出。适配器省略认证请求头，网页只监听本机回环地址。

## 源码和测试

网页查看器及其 trace 和存储依赖来自 [claude-tap 的 b21d171 提交](https://github.com/liaohch3/claude-tap/tree/b21d17157b674a0562e0ed08f4c4c607e8e5a56f)，随项目附带 MIT 许可证。

```fish
uv run --no-editable pytest -q
uv run --no-editable ruff check src tests
```
