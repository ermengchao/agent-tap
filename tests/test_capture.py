import asyncio
import json
import zlib
from pathlib import Path

from agent_tap.capture import CaptureReader, WebSocketDecoder
from agent_tap.trace_store import TraceStore

SESSION = "2026-11-23-160421"
CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


def frame(payload: bytes, *, bits=0x81, masked=False):
    n = len(payload)
    size = bytes([n]) if n < 126 else b"\x7e" + n.to_bytes(2, "big")
    if masked:
        size = bytes([size[0] | 128]) + size[1:]
        return bytes([bits]) + size + b"mask" + bytes(b ^ b"mask"[i % 4] for i, b in enumerate(payload))
    return bytes([bits]) + size + payload


def event(kind, **fields):
    return frame(json.dumps({"type": kind, **fields}).encode(), masked=kind == "response.create")


def append(path: Path, chunk: bytes):
    with path.open("ab") as handle:
        handle.write(chunk)


def test_fragmented_compression_retains_context():
    compressor = zlib.compressobj(wbits=-15)
    chunks = []
    for i in range(2):
        msg = json.dumps({"text": "shared context " * 40, "number": i}).encode()
        data = compressor.compress(msg) + compressor.flush(zlib.Z_SYNC_FLUSH)
        chunks.append(data[:-4])
    wire = (
        frame(chunks[0][:4], bits=0x41, masked=True)
        + frame(b"keepalive", bits=0x89)
        + frame(chunks[0][4:], bits=0x80, masked=True)
        + frame(chunks[1], bits=0xC1, masked=True)
    )
    decoder = WebSocketDecoder()
    decoded = [msg for i in range(0, len(wire), 3) for msg in decoder.feed(wire[i : i + 3])]
    assert [msg["number"] for msg in decoded] == [0, 1]


def test_active_websocket_multiple_turns_and_auth_redaction(tmp_path):
    async def scenario():
        root = tmp_path / "Surge Capture"
        folder = (
            root
            / SESSION
            / "Requests/000017 - 16.04.21 - GET - https%3A%2F%2Fchatgpt.com%2Fbackend-api%2Fcodex%2Fresponses"
        )
        folder.mkdir(parents=True)
        store = TraceStore(tmp_path / "traces.sqlite3")
        reader = CaptureReader(root, SESSION, store)
        metadata = {
            "id": 17,
            "processPath": CODEX,
            "completed": False,
            "requestHeader": "Authorization: secret\r\nCookie: secret\r\nOriginator: Codex",
        }
        for turn in range(2):
            append(folder / "request.dump", event("response.create", model="gpt-test", input=[{"role": "user"}]))
            append(folder / "response.dump", event("response.created", response={"id": f"r{turn}"}))
            await reader.poll([metadata])
            assert sum(s["record_count"] for s in store.list_session_rows()) == turn
            append(folder / "response.dump", event("response.completed", response={"id": f"r{turn}", "output": []}))
            await reader.poll([metadata])
            await reader.poll([metadata])
        row = store.list_session_rows()[0]
        records = list(store.load_records(row["id"]))
        assert len(records) == 2
        assert records[1]["request"]["headers"] == {"originator": "Codex"}
        assert records[1]["response"]["body"]["id"] == "r1"
        reader.close()

    asyncio.run(scenario())


def test_gzip_sse_written_at_terminal_event(tmp_path):
    async def scenario():
        root = tmp_path / "capture"
        folder = root / SESSION / "Requests/000019 - 16.04.21 - POST - https%3A%2F%2Fapi.openai.com%2Fv1%2Fresponses"
        folder.mkdir(parents=True)
        store = TraceStore(tmp_path / "traces.sqlite3")
        reader = CaptureReader(root, SESSION, store)
        metadata = {
            "id": 19,
            "processPath": "/usr/local/bin/codex",
            "completed": False,
            "responseHeader": "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Encoding: gzip",
        }
        (folder / "request.dump").write_bytes(b'{"model":"gpt-test","input":"hello","stream":true}')
        compressor = zlib.compressobj(wbits=31)
        start = b'data: {"type":"response.created","response":{"id":"test"}}\n\n'
        end = b'data: {"type":"response.completed","response":{"id":"test","usage":{"input_tokens":3}}}\n\n'
        append(folder / "response.dump", compressor.compress(start) + compressor.flush(zlib.Z_SYNC_FLUSH))
        await reader.poll([metadata])
        assert not store.list_session_rows()
        append(folder / "response.dump", compressor.compress(end) + compressor.flush(zlib.Z_SYNC_FLUSH))
        await reader.poll([metadata])
        row = store.list_session_rows()[0]
        record = list(store.load_records(row["id"]))[0]
        assert record["response"]["body"]["usage"]["input_tokens"] == 3
        assert record["response"]["status"] == 200
        reader.close()

    asyncio.run(scenario())
