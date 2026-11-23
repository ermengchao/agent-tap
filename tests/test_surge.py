import asyncio

import pytest

from agent_tap.surge import CaptureSettings, Journal, SurgeError, SurgeHTTPError, missing_hosts


class FakeSurge:
    base = "http://127.0.0.1:6171"

    def __init__(self, section="hostname = chatgpt.com\nca-p12 = keep", *, capture=False, mitm=False):
        self.content = section
        self.features = {"capture": capture, "mitm": mitm}
        self.changes = []
        self.session = "initial" if capture else None

    async def effective_mitm(self):
        return self.content

    async def section(self):
        return {"file": "Proxy.conf", "revision": str(len(self.changes)), "content": self.content}

    async def replace(self, section, content):
        assert section == await self.section()
        self.changes.append("profile")
        self.content = content

    async def enabled(self, feature):
        return self.features[feature]

    async def switch(self, feature, enabled):
        self.features[feature] = enabled
        self.changes.append((feature, enabled))
        if feature == "capture":
            self.session = "owned" if enabled else None

    async def active_session(self):
        return self.session


@pytest.mark.parametrize(
    ("hostnames", "disabled", "expected"),
    [
        ("chatgpt.com", "", ["api.openai.com"]),
        ("", "", ["chatgpt.com", "api.openai.com"]),
        ("chatgpt.com", "chatgpt.com", None),
        ("", "chatgpt.com", None),
        ("-chatgpt.com, *", "", None),
        ("chatgpt.com, *.openai.com", "", []),
    ],
)
def test_mitm_preflight(hostnames, disabled, expected):
    config = f"hostname = {hostnames}\nhostname-disabled = {disabled}"
    if expected is None:
        with pytest.raises(SurgeError, match="explicitly excludes"):
            missing_hosts(config)
    else:
        assert missing_hosts(config) == expected


def test_recovery_preserves_existing_and_unrelated_edits(tmp_path):
    async def scenario():
        api = FakeSurge(capture=True)
        journal = Journal(tmp_path / "state.json")
        await CaptureSettings(api, journal).prepare()
        assert api.changes == ["profile", ("mitm", True)]
        assert api.features == {"capture": True, "mitm": True}
        assert "keep" in api.content and "api.openai.com" in api.content
        api.content = api.content.replace("api.openai.com", "api.openai.com, example.com")
        await CaptureSettings(api, journal).restore()
        assert api.features == {"capture": True, "mitm": False}
        assert api.content == "hostname = chatgpt.com, example.com\nca-p12 = keep"
        assert journal.load() is None

    asyncio.run(scenario())


def test_recovery_does_not_stop_replacement_capture(tmp_path):
    async def scenario():
        api = FakeSurge("hostname = chatgpt.com, api.openai.com", mitm=True)
        journal = Journal(tmp_path / "state.json")
        settings = CaptureSettings(api, journal)
        await settings.prepare()
        settings.bind_capture("owned")
        api.session = "started-by-user"
        await settings.restore()
        assert api.changes == [("capture", True)]
        assert api.features["capture"] is True

    asyncio.run(scenario())


def test_initialization_error_restores_prior_steps(tmp_path):
    class FailingSurge(FakeSurge):
        async def switch(self, feature, enabled):
            if feature == "capture" and enabled:
                raise SurgeError("unavailable")
            await super().switch(feature, enabled)

    async def scenario():
        api = FailingSurge()
        original = api.content
        journal = Journal(tmp_path / "state.json")
        with pytest.raises(SurgeError, match="unavailable"):
            await CaptureSettings(api, journal).prepare()
        assert journal.load() is not None
        await CaptureSettings(api, journal).restore()
        assert api.content == original
        assert api.features == {"capture": False, "mitm": False}

    asyncio.run(scenario())


def test_profile_conflict_does_not_claim_user_edit(tmp_path):
    class ConflictingSurge(FakeSurge):
        async def replace(self, section, content):
            self.content = content
            raise SurgeHTTPError(400, "/v1/profiles/section")

    async def scenario():
        api = ConflictingSurge()
        settings = CaptureSettings(api, Journal(tmp_path / "state.json"))
        with pytest.raises(SurgeHTTPError):
            await settings.prepare()
        await settings.restore()
        assert "api.openai.com" in api.content
        assert api.changes == []

    asyncio.run(scenario())
