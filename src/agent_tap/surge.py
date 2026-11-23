"""Surge HTTP API and recoverable ownership of capture configuration."""

from __future__ import annotations

import fcntl
import fnmatch
import ipaddress
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

HOSTS = ("chatgpt.com", "api.openai.com")
FEATURES = ("mitm", "capture")
DIRECTIVE = re.compile(r"(?im)^([ \t]*hostname[ \t]*=[ \t]*)([^\r\n]*)")
SECTION = re.compile(r"(?m)^[ \t]*\[([^\]\r\n]+)\][ \t]*\r?$")


class SurgeError(RuntimeError):
    """A safe error message that contains neither credentials nor request bodies."""


class SurgeHTTPError(SurgeError):
    def __init__(self, status: int, path: str):
        self.status = status
        super().__init__(f"Surge returned HTTP {status} for {path}.")


class SurgeAPI:
    def __init__(self, base: str, secret: str):
        url = urlsplit(base)
        try:
            local = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            local = url.hostname == "localhost"
        if (
            not local
            or url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise SurgeError("AGENT_TAP_SURGE_API_URL must be a local HTTP or HTTPS listener.")
        if not secret or "\r" in secret or "\n" in secret:
            raise SurgeError("Set AGENT_TAP_SURGE_API_KEY to the Surge HTTP API key.")
        self.base = base.rstrip("/")
        self.secret = secret
        self.http: ClientSession

    async def __aenter__(self):
        self.http = ClientSession(headers={"X-Key": self.secret}, timeout=ClientTimeout(total=5), trust_env=False)
        return self

    async def __aexit__(self, *_):
        await self.http.close()

    async def call(self, path: str, *, params: dict | None = None, body: dict | None = None):
        try:
            async with self.http.request(
                "GET" if body is None else "POST",
                self.base + path,
                params=params,
                json=body,
                allow_redirects=False,
            ) as result:
                if not 200 <= result.status < 300:
                    raise SurgeHTTPError(result.status, path)
                return await result.json()
        except (ClientError, TimeoutError, ValueError) as exc:
            raise SurgeError(f"Could not read Surge API response for {path} ({type(exc).__name__}).") from None

    async def enabled(self, feature: str) -> bool:
        value = (await self.call(f"/v1/features/{feature}")).get("enabled")
        if type(value) is not bool:
            raise SurgeError(f"Surge did not report the {feature} switch state.")
        return value

    async def switch(self, feature: str, enabled: bool):
        await self.call(f"/v1/features/{feature}", body={"enabled": enabled})
        if await self.enabled(feature) != enabled:
            raise SurgeError(f"Surge did not apply the {feature} switch change.")

    async def section(self) -> dict:
        section = await self.call("/v1/profiles/section", params={"name": "MITM"})
        if not isinstance(section.get("content"), str) or not section.get("revision") or not section.get("file"):
            raise SurgeError("Surge did not return the MITM section, file and revision. Surge Mac 6.11+ is required.")
        return section

    async def effective_mitm(self) -> str:
        profile = (await self.call("/v1/profiles/current", params={"sensitive": 0})).get("profile")
        if not isinstance(profile, str):
            raise SurgeError("Surge did not return the effective profile.")
        sections = list(SECTION.finditer(profile))
        for index, section in enumerate(sections):
            if section.group(1).casefold() == "mitm":
                end = sections[index + 1].start() if index + 1 < len(sections) else len(profile)
                return profile[section.end() : end]
        return ""

    async def replace(self, section: dict, content: str):
        await self.call(
            "/v1/profiles/section", body={"name": "MITM", "content": content, "revision": section["revision"]}
        )

    async def active_session(self) -> str | None:
        result = await self.call("/v1/capture/sessions")
        for row in result.get("sessions", []):
            if row.get("active") and isinstance(row.get("name"), str):
                return row["name"]
        return None

    async def active_requests(self) -> list[dict]:
        data = await self.call("/v1/requests/active")
        return data if isinstance(data, list) else data.get("requests", [])


def tokens(content: str, name: str) -> list[str]:
    lines = re.findall(rf"(?im)^[ \t]*{re.escape(name)}[ \t]*=[ \t]*([^\r\n]*)", content)
    return [entry.strip() for line in lines for entry in line.split(",") if entry.strip()]


def matches(token: str, host: str) -> bool:
    pattern, _, port = token.removeprefix("-").casefold().partition(":")
    if port not in {"", "0", "443"}:
        return False
    return fnmatch.fnmatchcase(host, pattern)


def missing_hosts(effective: str) -> list[str]:
    enabled = tokens(effective, "hostname")
    disabled = tokens(effective, "hostname-disabled")
    missing = []
    for host in HOSTS:
        first = next((entry for entry in enabled if matches(entry, host)), None)
        if any(not entry.startswith("-") and matches(entry, host) for entry in disabled) or (
            first is not None and first.startswith("-")
        ):
            raise SurgeError(f"MITM explicitly excludes {host}. Enable the hostname in Surge, then try again.")
        if first is None:
            missing.append(host)
    return missing


def append_hosts(content: str, hosts: list[str]) -> str:
    match = DIRECTIVE.search(content)
    if match is None:
        return content + ("\n" if content and not content.endswith("\n") else "") + "hostname = " + ", ".join(hosts)
    end = match.end(2)
    current = match[2].rstrip()
    end -= len(match[2]) - len(current)
    return content[:end] + (", " if current else "") + ", ".join(hosts) + content[end:]


def remove_hosts(content: str, hosts: list[str], had_hostname: bool) -> str:
    directives = list(DIRECTIVE.finditer(content))
    if len(directives) > 1:
        raise SurgeError("The MITM section now contains multiple hostname lines. Resolve them before restoring.")
    if not directives:
        return content
    match = directives[0]
    remaining = match[2].split(",")
    for host in hosts:
        indices = [i for i, entry in enumerate(remaining) if entry.strip().casefold() == host]
        if len(indices) > 1:
            raise SurgeError(f"MITM contains duplicate {host} entries. Resolve those entries before restoring.")
        if indices:
            del remaining[indices[0]]
    if not any(entry.strip() for entry in remaining) and not had_hostname:
        end = match.end()
        if content[end : end + 2] == "\r\n":
            end += 2
        elif content[end : end + 1] == "\n":
            end += 1
        return content[: match.start()] + content[end:]
    kept = ",".join(remaining).lstrip()
    return content[: match.start(2)] + kept + content[match.end(2) :]


@dataclass
class Journal:
    path: Path

    @contextmanager
    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SurgeError("Another agent-tap capture or restore is running.") from None
            yield
        finally:
            os.close(fd)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise SurgeError("Surge recovery state could not be read. Inspect the local state file.") from None

    def save(self, state: dict):
        pending = self.path.with_suffix(".tmp")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


class CaptureSettings:
    def __init__(self, api: SurgeAPI, journal: Journal):
        self.api = api
        self.journal = journal

    async def prepare(self):
        if self.journal.load() is not None:
            raise SurgeError("An earlier capture needs recovery. Run agent-tap restore first.")
        section = await self.api.section()
        effective = await self.api.effective_mitm()
        disabled = tokens(section["content"], "hostname-disabled")
        if disabled:
            effective += "\nhostname-disabled = " + ", ".join(disabled)
        hosts = missing_hosts(effective)
        if hosts and section.get("editable") is False:
            raise SurgeError("The current MITM section cannot be edited. Add the missing domains in Surge.")
        local_hosts = tokens(section["content"], "hostname")
        if any(host in {entry.casefold() for entry in local_hosts} for host in hosts):
            raise SurgeError("The active profile or a module overrides the local MITM hostname list.")
        original = {name: await self.api.enabled(name) for name in FEATURES}
        state = {
            "api": self.api.base,
            "file": section["file"],
            "had_hostname": bool(DIRECTIVE.search(section["content"])),
            "hosts": [],
            "original": original,
            "switches": [],
        }
        self.journal.save(state)
        if hosts:
            state["hosts"] = hosts
            self.journal.save(state)
            try:
                await self.api.replace(section, append_hosts(section["content"], hosts))
            except SurgeHTTPError as exc:
                if 400 <= exc.status < 500 and exc.status != 408:
                    state["hosts"] = []
                    self.journal.save(state)
                raise
            if missing_hosts(await self.api.effective_mitm()):
                raise SurgeError("Surge did not apply the requested MITM hostnames.")
        for name in FEATURES:
            if not await self.api.enabled(name):
                if original[name]:
                    raise SurgeError(f"The {name} switch changed during startup.")
                state["switches"].append(name)
                self.journal.save(state)
                await self.api.switch(name, True)

    def bind_capture(self, session: str):
        state = self.journal.load()
        if state and "capture" in state["switches"]:
            state["capture_session"] = session
            self.journal.save(state)

    async def owns_capture(self, state: dict) -> bool:
        if "capture" not in state["switches"]:
            return False
        if not state.get("capture_session"):
            return True
        return await self.api.active_session() == state["capture_session"]

    async def restore(self) -> bool:
        state = self.journal.load()
        if state is None:
            return False
        if state["api"] != self.api.base or (await self.api.section())["file"] != state["file"]:
            raise SurgeError("The Surge API or profile changed. Reconnect to the original profile and run restore.")
        for name in reversed(state["switches"][:]):
            owned = name != "capture" or await self.owns_capture(state)
            if owned and await self.api.enabled(name) != state["original"][name]:
                await self.api.switch(name, state["original"][name])
            state["switches"].remove(name)
            self.journal.save(state)
        if state["hosts"]:
            section = await self.api.section()
            content = remove_hosts(section["content"], state["hosts"], state["had_hostname"])
            if content != section["content"]:
                await self.api.replace(section, content)
                if (await self.api.section())["content"] != content:
                    raise SurgeError("Restored MITM profile could not be verified. Run agent-tap restore again.")
        self.journal.clear()
        return True
