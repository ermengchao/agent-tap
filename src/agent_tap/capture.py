"""Incremental readers for the local files written by Surge HTTP Capture."""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import brotli

from agent_tap.sse import SSEReassembler
from agent_tap.trace import TraceWriter, create_trace_writer
from agent_tap.trace_store import TraceStore

try:
    from compression import zstd
except ImportError:
    import backports.zstd as zstd

LOG = logging.getLogger(__name__)
MODEL_PATHS = {
    "chatgpt.com": "/backend-api/codex/responses",
    "api.openai.com": "/v1/responses",
}
TERMINAL = {
    "response.completed",
    "response.done",
    "response.incomplete",
    "response.failed",
    "response.error",
    "error",
}
MAX_FRAME = 32 * 1024 * 1024
READ_SIZE = 256 * 1024
FOLDER = re.compile(r"^(\d+) - [\d.]+ - (GET|POST) - (.*)$")
CODEX_BINARY = re.compile(r"^codex(?:-(?:aarch64|x86_64|arm64)[\w-]*)?(?:\.exe)?$", re.I)
ALLOWED_HEADERS = {
    "content-type",
    "content-encoding",
    "user-agent",
    "originator",
    "upgrade",
    "sec-websocket-extensions",
}


class DecodeError(ValueError):
    """Capture cannot be decoded safely."""


def headers(raw: str | dict | None, allowed=ALLOWED_HEADERS) -> dict[str, str]:
    if isinstance(raw, str):
        raw = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line)
    if not isinstance(raw, dict):
        return {}
    return {
        key.strip().lower(): value.strip()
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and key.strip().lower() in allowed
    }


def client_name(metadata: dict) -> str | None:
    process = str(metadata.get("processPath") or "")
    name = Path(process).name
    if re.search(r"/(?:ChatGPT|Codex)\.app/", process, re.I) and (
        CODEX_BINARY.fullmatch(name) or name.startswith(("Codex", "ChatGPT"))
    ):
        return "codexapp"
    user_agent = headers(metadata.get("requestHeader")).get("user-agent", "")
    if CODEX_BINARY.fullmatch(name) or re.match(r"(?:codex_cli_rs|codex-cli)/", user_agent, re.I):
        return "codex"
    return None


def candidate(path: Path) -> tuple[int, str, str, str] | None:
    match = FOLDER.fullmatch(path.name)
    if not match:
        return None
    url = urlsplit(unquote(match[3]))
    if url.scheme not in {"https", "wss"} or not url.hostname or MODEL_PATHS.get(url.hostname) != url.path:
        return None
    return int(match[1]), match[2], url.hostname, url.path


class Tail:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.file_id: tuple[int, int] | None = None

    def read(self):
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            return
        with handle:
            stat = os.fstat(handle.fileno())
            identity = stat.st_dev, stat.st_ino
            if self.file_id is not None and (identity != self.file_id or stat.st_size < self.offset):
                raise DecodeError("dump was replaced or truncated")
            self.file_id = identity
            handle.seek(self.offset)
            left = stat.st_size - self.offset
            while left and (chunk := handle.read(min(left, READ_SIZE))):
                left -= len(chunk)
                self.offset += len(chunk)
                yield chunk


class WebSocketDecoder:
    """Decode both masked and unmasked messages, retaining compression context."""

    def __init__(self, *, no_context_takeover: bool = False):
        self.buffer = bytearray()
        self.message = bytearray()
        self.opcode = 0
        self.compressed = False
        self.inflater = zlib.decompressobj(-15)
        self.no_context_takeover = no_context_takeover

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        buffer = self.buffer
        pos = 0
        try:
            while len(buffer) - pos >= 2:
                a, b = buffer[pos : pos + 2]
                offset = pos + 2
                size = b & 127
                if size >= 126:
                    length_bytes = 2 if size == 126 else 8
                    if len(buffer) - offset < length_bytes:
                        break
                    size = int.from_bytes(buffer[offset : offset + length_bytes], "big")
                    offset += length_bytes
                if size > MAX_FRAME:
                    raise DecodeError("WebSocket frame exceeds the supported size")
                masked = bool(b & 128)
                key = buffer[offset : offset + 4] if masked else b""
                offset += 4 if masked else 0
                if len(buffer) - offset < size:
                    break
                payload = buffer[offset : offset + size]
                pos = offset + size
                if masked:
                    payload = bytes(value ^ key[i % 4] for i, value in enumerate(payload))
                opcode = a & 15
                if opcode >= 8:
                    continue
                if opcode in (1, 2):
                    if self.opcode:
                        raise DecodeError("new WebSocket message before previous fragment completed")
                    self.opcode = opcode
                    self.compressed = bool(a & 64)
                elif opcode != 0 or not self.opcode:
                    raise DecodeError("invalid WebSocket continuation")
                if len(self.message) + len(payload) > MAX_FRAME:
                    raise DecodeError("WebSocket message exceeds the supported size")
                self.message.extend(payload)
                if not a & 128:
                    continue
                message = bytes(self.message)
                self.message.clear()
                self.opcode = 0
                if self.compressed:
                    try:
                        message = self.inflater.decompress(message + b"\x00\x00\xff\xff", MAX_FRAME + 1)
                    except zlib.error:
                        raise DecodeError("invalid compressed WebSocket message") from None
                    if len(message) > MAX_FRAME or self.inflater.unconsumed_tail:
                        raise DecodeError("inflated WebSocket message exceeds the supported size")
                    if self.no_context_takeover:
                        self.inflater = zlib.decompressobj(-15)
                try:
                    obj = json.loads(message)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(obj, dict):
                    yield obj
        finally:
            del buffer[:pos]


def response_body(events: list[dict]) -> dict | None:
    parser = SSEReassembler(store_events=False)
    for item in events:
        parser.add_event(item.get("type", ""), item)
    return parser.reconstruct()


def decode_content(body: bytes, encoding: str) -> bytes:
    for item in reversed(encoding.lower().split(",")):
        item = item.strip()
        if item in {"", "identity"}:
            continue
        if item == "gzip":
            body = gzip.decompress(body)
        elif item == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -15)
        elif item == "zstd":
            body = zstd.decompress(body)
        elif item == "br":
            body = brotli.decompress(body)
        else:
            raise DecodeError("unsupported HTTP Content-Encoding")
    return body


class StreamContent:
    def __init__(self, encoding: str):
        self.decoders = []
        for item in reversed(encoding.lower().split(",")):
            item = item.strip()
            if item in {"", "identity"}:
                continue
            if item == "gzip":
                self.decoders.append(zlib.decompressobj(31).decompress)
            elif item == "deflate":
                self.decoders.append(zlib.decompressobj().decompress)
            elif item == "zstd":
                self.decoders.append(
                    zstd.EndlessZstdDecompressor().decompress
                    if hasattr(zstd, "EndlessZstdDecompressor")
                    else zstd.ZstdDecompressor().decompress
                )
            elif item == "br":
                self.decoders.append(brotli.Decompressor().process)
            else:
                raise DecodeError("unsupported streaming Content-Encoding")

    def feed(self, data: bytes) -> bytes:
        for decode in self.decoders:
            if data:
                data = decode(data)
        return data


class ModelConnection:
    def __init__(
        self,
        path: Path,
        info: tuple[int, str, str, str],
        client: str,
        store: TraceStore,
        session: str,
        http_writers: dict[tuple, TraceWriter],
    ):
        self.path = path
        self.ident, self.method, self.host, self.route = info
        self.client = client
        self.store = store
        self.session = session
        self.http_writers = http_writers
        self.http_group: tuple = ()
        self.writer: TraceWriter | None = None
        self.tails = {part: Tail(path / (part + ".dump")) for part in ("request", "response")}
        self.decoders: dict[str, WebSocketDecoder] = {}
        self.pending: deque[tuple[dict, float]] = deque()
        self.events: list[dict] = []
        self.completed_ids: set[str] = set()
        self.request_raw = bytearray()
        self.response_raw = bytearray()
        self.stream: StreamContent | None = None
        self.sse = SSEReassembler()
        self.http_written = False
        self.finished = False
        self.request_headers: dict[str, str] = {}
        self.response_headers: dict[str, str] = {}

    async def emit(self, body: dict, response: dict, *, started: float, partial: bool = False, baseline: bool = False):
        if baseline:
            return
        if self.writer is None:
            self.writer = self.http_writers.get(self.http_group) if self.method == "POST" else None
            if self.writer is None:
                self.writer = create_trace_writer(
                    store=self.store,
                    client=self.client,
                    proxy_mode="surge",
                    metadata={"source": "surge", "surge_session": self.session},
                )
                if self.method == "POST":
                    self.http_writers[self.http_group] = self.writer
        record = {
            "timestamp": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "request_id": f"surge-{self.session}-{self.path.name.split(' - ')[0]}-{self.writer.count + 1}",
            "transport": "websocket" if self.method == "GET" else "http",
            "duration_ms": max(0, round((time.time() - started) * 1000)),
            "upstream_base_url": f"https://{self.host}",
            "request": {
                "method": "WEBSOCKET" if self.method == "GET" else "POST",
                "path": self.route,
                "headers": self.request_headers,
                "body": body,
            },
            "response": response,
        }
        if partial:
            record["partial"] = True
        await self.writer.write_next_turn(record)
        if self.writer.dropped_trace_records:
            raise OSError("Trace database could not store a captured record.")

    async def ws_flush(self, *, baseline: bool, partial: bool = False):
        body, started = self.pending.popleft()
        events, self.events = self.events, []
        terminal = events[-1].get("type") if events else None
        response = {
            "status": 101,
            "headers": self.response_headers,
            "body": response_body(events),
            "ws_events": events,
        }
        if partial:
            response["error"] = "Connection closed before a terminal response event."
        elif terminal in TERMINAL - {"response.completed", "response.done"}:
            response["error"] = str(terminal)
        await self.emit(body, response, started=started, partial=partial, baseline=baseline)

    async def websocket(self, *, baseline: bool):
        extensions = self.response_headers.get("sec-websocket-extensions", "").lower()
        for part, prefix in (("request", "client"), ("response", "server")):
            if part not in self.decoders:
                self.decoders[part] = WebSocketDecoder(
                    no_context_takeover=f"{prefix}_no_context_takeover" in extensions
                )
            decoder = self.decoders[part]
            if f"{prefix}_no_context_takeover" in extensions:
                decoder.no_context_takeover = True
            for chunk in self.tails[part].read():
                for event in decoder.feed(chunk):
                    kind = event.get("type")
                    if part == "request":
                        if kind == "response.create":
                            self.pending.append((event, time.time()))
                        continue
                    response = event.get("response")
                    response_id = str(response.get("id")) if isinstance(response, dict) and response.get("id") else None
                    if kind in TERMINAL and response_id and response_id in self.completed_ids:
                        continue
                    if not self.pending:
                        continue
                    self.events.append(event)
                    if kind in TERMINAL:
                        if response_id:
                            self.completed_ids.add(response_id)
                        await self.ws_flush(baseline=baseline)

    def read_http(self, completed: bool):
        for chunk in self.tails["request"].read():
            self.request_raw.extend(chunk)
        encoding = self.response_headers.get("content-encoding", "")
        content_type = self.response_headers.get("content-type", "").lower()
        if self.stream is None:
            for chunk in self.tails["response"].read():
                self.response_raw.extend(chunk)
            plain_sse = self.response_raw.lstrip().startswith((b"data:", b"event:", b":"))
            if "text/event-stream" in content_type or (not encoding and plain_sse):
                self.stream = StreamContent(encoding)
                self.sse.feed_bytes(self.stream.feed(bytes(self.response_raw)))
                self.response_raw.clear()
            elif not completed:
                return None
        else:
            for chunk in self.tails["response"].read():
                self.sse.feed_bytes(self.stream.feed(chunk))
        events = self.sse.events
        terminal = any(
            item["event"] in TERMINAL or (isinstance(item["data"], dict) and item["data"].get("type") in TERMINAL)
            for item in events
        )
        if not (terminal or completed):
            return None
        return events

    async def http(self, metadata: dict, *, completed: bool, baseline: bool):
        if self.http_written:
            return
        events = self.read_http(completed)
        if events is None:
            return
        try:
            body = json.loads(decode_content(bytes(self.request_raw), self.request_headers.get("content-encoding", "")))
        except (ValueError, EOFError, zlib.error, brotli.error, zstd.ZstdError):
            if not completed:
                return
            raise DecodeError("could not decode HTTP request JSON") from None
        if not isinstance(body, dict):
            raise DecodeError("HTTP request was not a JSON object")
        raw_status = str(metadata.get("responseHeader") or "").splitlines()
        status_match = re.match(r"HTTP/\S+\s+(\d{3})", raw_status[0]) if raw_status else None
        status = int(status_match[1]) if status_match else 200
        if events:
            decoded = [
                {**item["data"], "type": item["data"].get("type") or item["event"]}
                for item in events
                if isinstance(item["data"], dict)
            ]
            response = {"body": response_body(decoded), "sse_events": events}
            terminal = next((event["type"] for event in reversed(decoded) if event["type"] in TERMINAL), None)
            if terminal in TERMINAL - {"response.completed", "response.done"}:
                response["error"] = terminal
            partial = terminal is None
        else:
            raw = decode_content(bytes(self.response_raw), self.response_headers.get("content-encoding", ""))
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                result = raw.decode("utf-8", errors="replace") if raw else None
            response = {"body": result}
            partial = not raw
        response.update({"headers": self.response_headers, "status": status})
        if partial:
            response["error"] = "Connection closed before the full response was captured."
        await self.emit(
            body, response, started=float(metadata.get("startDate") or time.time()), partial=partial, baseline=baseline
        )
        self.http_written = True

    async def poll(self, metadata: dict, *, completed: bool = False, baseline: bool = False):
        if self.finished:
            return
        self.request_headers.update(headers(metadata.get("requestHeader")))
        self.response_headers.update(headers(metadata.get("responseHeader")))
        if self.method == "GET":
            await self.websocket(baseline=baseline)
            if completed:
                while self.pending:
                    await self.ws_flush(baseline=baseline, partial=True)
        else:
            ids = headers(metadata.get("requestHeader"), {"session-id", "x-session-id"})
            group = ids.get("session-id") or ids.get("x-session-id") or metadata.get("pid") or self.ident
            self.http_group = (self.client, self.host, group)
            await self.http(metadata, completed=completed, baseline=baseline)
        if completed or self.http_written:
            self.close()

    def close(self):
        self.finished = True
        if self.writer and self.method == "GET":
            self.writer.close()
        self.pending.clear()
        self.events.clear()
        self.request_raw.clear()
        self.response_raw.clear()
        self.sse.events.clear()
        self.decoders.clear()


class CaptureReader:
    def __init__(self, root: Path, session: str, store: TraceStore):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}", session):
            raise ValueError("Unexpected Surge session name.")
        self.session = session
        self.requests = root / session / "Requests"
        self.store = store
        self.connections: dict[Path, ModelConnection] = {}
        self.http_writers: dict[tuple, TraceWriter] = {}
        self.baseline: set[Path] = set()

    def remember_existing(self):
        self.baseline = set(self.requests.glob("*"))

    async def poll(self, active: list[dict], *, final: bool = False):
        active_ids = {str(row.get("id")): row for row in active}
        for path in sorted(self.requests.glob("*")):
            if not path.is_dir() or path.is_symlink():
                continue
            info = candidate(path)
            if not info:
                continue
            conn = self.connections.get(path)
            if conn and conn.finished:
                continue
            try:
                saved = json.loads((path / "model.json").read_text())
                if not isinstance(saved, dict):
                    saved = {}
            except (FileNotFoundError, ValueError):
                saved = {}
            live = active_ids.get(str(info[0]), {})
            metadata = {**saved, **{k: v for k, v in live.items() if v is not None}}
            if conn is None and path in self.baseline and metadata.get("completed") is True:
                continue
            if conn is None:
                client = client_name(metadata)
                if not client:
                    continue
                conn = ModelConnection(path, info, client, self.store, self.session, self.http_writers)
                self.connections[path] = conn
            try:
                await conn.poll(
                    metadata, completed=final or metadata.get("completed") is True, baseline=path in self.baseline
                )
            except (DecodeError, EOFError, UnicodeError, zlib.error, brotli.error, zstd.ZstdError) as exc:
                if isinstance(exc, DecodeError):
                    LOG.warning("Capture %s could not be decoded: %s", info[0], exc)
                else:
                    LOG.warning("Capture %s could not be decoded (%s).", info[0], type(exc).__name__)
                conn.close()
            self.baseline.discard(path)

    def close(self):
        for conn in self.connections.values():
            if not conn.finished:
                conn.close()
        for writer in self.http_writers.values():
            writer.close()
        self.http_writers.clear()
