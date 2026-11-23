"""The URL and version helpers used by the upstream viewer."""

from agent_tap import __version__

CLAUDE_TAP_VERSION = __version__


def dashboard_url(host: str, port: int) -> str:
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{address}:{port}"
