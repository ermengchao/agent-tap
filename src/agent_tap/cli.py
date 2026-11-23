"""Run the Surge adapter or browse its local trace history."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
import webbrowser
from pathlib import Path

from agent_tap.capture import CaptureReader
from agent_tap.live import LiveViewerServer
from agent_tap.surge import CaptureSettings, Journal, SurgeAPI, SurgeError
from agent_tap.trace_store import get_trace_store, resolve_db_path

LOG = logging.getLogger(__name__)


def recovery_journal() -> Journal:
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return Journal(base.expanduser() / "agent-tap" / "surge-recovery.json")


def arguments(argv=None):
    parser = argparse.ArgumentParser(prog="agent-tap", description="Live Codex traces from Surge HTTP Capture.")
    group = parser.add_subparsers(dest="command", required=True)
    capture = group.add_parser("capture", help="Manage a Surge capture and start the viewer")
    capture.add_argument(
        "--capture-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "Surge Capture",
        help="Surge HTTP Capture root",
    )
    viewer = group.add_parser("viewer", help="Browse captured traces")
    for command in (capture, viewer):
        command.add_argument("--port", type=int, default=0, help="Loopback viewer port (default: pick a free port)")
        command.add_argument("--no-open", action="store_true", help="Do not open the browser")
    group.add_parser("restore", help="Recover Surge settings after an interrupted capture")
    return parser.parse_args(argv)


def surge_api() -> SurgeAPI:
    return SurgeAPI(
        os.environ.get("AGENT_TAP_SURGE_API_URL", "http://127.0.0.1:6171"),
        os.environ.get("AGENT_TAP_SURGE_API_KEY", ""),
    )


async def run_capture(args, stop: asyncio.Event):
    journal = recovery_journal()
    with journal.lock():
        if journal.load() is not None:
            raise SurgeError("An earlier capture needs recovery. Run agent-tap restore first.")
        async with surge_api() as api:
            settings = CaptureSettings(api, journal)
            reader = None
            server = LiveViewerServer(port=args.port, host="127.0.0.1", dashboard_mode=True)
            try:
                await settings.prepare()
                name = None
                for _ in range(12):
                    if stop.is_set():
                        return
                    name = await api.active_session()
                    if name:
                        break
                    await asyncio.sleep(0.25)
                if not name:
                    raise SurgeError("Surge did not report an active HTTP Capture session.")
                settings.bind_capture(name)
                reader = CaptureReader(args.capture_dir.expanduser(), name, get_trace_store())
                if (journal.load() or {}).get("original", {}).get("capture"):
                    reader.remember_existing()
                await server.start()
                print(f"Surge capture: {name}", flush=True)
                print(f"Trace database: {resolve_db_path()}", flush=True)
                show(server, args.no_open)
                dashboard_stop = asyncio.create_task(server.wait_stopped())
                try:
                    while not stop.is_set() and not dashboard_stop.done():
                        if await api.active_session() != name:
                            print("Surge capture ended or changed.", flush=True)
                            await reader.poll([], final=True)
                            break
                        await reader.poll(await api.active_requests())
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1)
                        except TimeoutError:
                            pass
                    else:
                        await reader.poll(await api.active_requests())
                        state = journal.load()
                        if state and await settings.owns_capture(state):
                            await api.switch("capture", False)
                            await asyncio.sleep(0.3)
                        await reader.poll([], final=True)
                finally:
                    dashboard_stop.cancel()
                    await asyncio.gather(dashboard_stop, return_exceptions=True)
            finally:
                try:
                    if reader:
                        reader.close()
                    await server.stop()
                finally:
                    if journal.load() is not None:
                        try:
                            await settings.restore()
                        except (SurgeError, OSError) as exc:
                            LOG.error("Surge recovery is pending. Run agent-tap restore. %s", exc)
                            raise


def show(server: LiveViewerServer, no_open: bool):
    print(f"Viewer: {server.url}", flush=True)
    if not no_open:
        webbrowser.open(server.url)


async def browse(args, stop: asyncio.Event):
    server = LiveViewerServer(port=args.port, host="127.0.0.1", dashboard_mode=True)
    try:
        await server.start()
        show(server, args.no_open)
        waits = [asyncio.create_task(coro) for coro in (stop.wait(), server.wait_stopped())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
    finally:
        await server.stop()


async def run(args):
    if args.command != "viewer" and sys.platform != "darwin":
        raise SurgeError("Surge capture and recovery require macOS.")
    if args.command == "restore":
        journal = recovery_journal()
        with journal.lock():
            if journal.load() is None:
                print("No Surge settings need recovery.")
                return
            async with surge_api() as api:
                await CaptureSettings(api, journal).restore()
                print("Surge settings restored.")
        return
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if args.command == "capture":
            await run_capture(args, stop)
        else:
            await browse(args, stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        asyncio.run(run(arguments(argv)))
    except (SurgeError, OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0
