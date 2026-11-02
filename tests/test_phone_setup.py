import io
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestragent import phone_setup
from orchestragent.access import Access

ORIGIN = "https://test.tail123.ts.net:8443"


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> phone_setup.Session:
    process = Mock()
    process.stdout = io.BytesIO(b"visit https://claude.com/cai/oauth/authorize?state=fixture\n")
    process.stdin = io.BytesIO()
    process.poll.return_value = None
    monkeypatch.setattr("orchestragent.phone_setup.subprocess.Popen", Mock(return_value=process))
    value = phone_setup.Session(tmp_path, ORIGIN)
    assert value.reader is not None
    value.reader.join(timeout=1)
    return value


@pytest.fixture
def endpoint(session: phone_setup.Session) -> Iterator[str]:
    with HTTPServer(("127.0.0.1", 0), phone_setup.handler(session)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}/{session.token}"
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize("origin", ["http://test.ts.net", "https://example.com",
                                   "https://test.ts.net/path", "https://x@test.ts.net"])
def test_phone_setup_requires_private_https_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError):
        phone_setup.Session(tmp_path, origin)


def test_code_goes_only_to_official_process_stdin(session: phone_setup.Session) -> None:
    fields = {"csrf": [session.token], "action": ["claude"], "code": ["fixture-code"]}
    session.submit(fields)
    assert session.code_sent
    assert session.process is not None
    assert isinstance(session.process.stdin, io.BytesIO)
    assert session.process.stdin.getvalue() == b"fixture-code\n"
    assert b"fixture-code" not in session.page()
    with pytest.raises(ValueError):
        session.submit(fields)


@pytest.mark.parametrize("csrf,code", [("bad", "fixture"), ("valid", "a\nb"),
                                      ("valid", ""), ("valid", "x" * 4097)])
def test_invalid_code_never_reaches_process(
    session: phone_setup.Session, csrf: str, code: str,
) -> None:
    with pytest.raises(ValueError):
        session.submit({"csrf": [session.token if csrf == "valid" else csrf],
                        "action": ["claude"], "code": [code]})
    assert not session.code_sent


@pytest.mark.parametrize("origin,host,site,expected", [
    (ORIGIN, "test.tail123.ts.net:8443", "same-origin", 200),
    (None, "test.tail123.ts.net:8443", "same-origin", 200),
    ("null", "test.tail123.ts.net:8443", "same-origin", 200),
    ("null", "test.tail123.ts.net:8443", "cross-site", 403),
    ("https://attacker.invalid", "test.tail123.ts.net:8443", "cross-site", 403),
    (ORIGIN, "attacker.invalid", "same-origin", 403),
])
def test_phone_form_checks_origin_and_host(
    endpoint: str, session: phone_setup.Session, origin: str | None, host: str,
    site: str, expected: int,
) -> None:
    body = urllib.parse.urlencode({"csrf": session.token, "action": "claude",
                                   "code": "fixture-code"}).encode()
    headers = {"Host": host, "Sec-Fetch-Site": site}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(endpoint, data=body, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            status = response.status
            assert response.headers["Cache-Control"] == "no-store"
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
    assert status == expected
    assert session.code_sent is (expected == 200)


@pytest.fixture
def key_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> phone_setup.Session:
    vault: dict[str, str] = {}
    monkeypatch.setattr(Access, "_persist", staticmethod(lambda: True))
    monkeypatch.setattr(Access, "_keychain_store", staticmethod(vault.__setitem__))
    monkeypatch.setattr(Access, "_keychain_load", lambda self, provider: vault.get(provider, ""))
    monkeypatch.setattr("orchestragent.access.is_macos", lambda: True)
    monkeypatch.setattr("orchestragent.phone_setup.subprocess.Popen",
                        Mock(side_effect=AssertionError("key entry must not log in Claude")))
    return phone_setup.Session(tmp_path, ORIGIN, keys_only="dsh", cap_cents=12400)


def test_dsf_key_persists_without_login_or_model_call(key_session: phone_setup.Session) -> None:
    fields = {"csrf": [key_session.token], "action": ["dsh"], "key": ["fixture-dsf-key"]}
    message = key_session.submit(fields)
    access = Access(key_session.root)
    access.restore_keys()
    assert access.key("dsh") == "fixture-dsf-key"
    assert access.status("dsh")["cap_cents"] == 12400
    assert key_session.key_saved and "尚未验证" in message
    assert b"fixture-dsf-key" not in key_session.page()
    assert "Claude" not in key_session.page().decode()
    with pytest.raises(ValueError, match="already saved"):
        key_session.submit(fields)
    key_session.close()


@pytest.mark.parametrize("action,csrf", [("claude", "valid"), ("jev", "valid"), ("dsh", "bad")])
def test_key_session_rejects_other_actions_and_csrf(
    key_session: phone_setup.Session, action: str, csrf: str,
) -> None:
    with pytest.raises(ValueError):
        key_session.submit({"csrf": [key_session.token if csrf == "valid" else csrf],
                            "action": [action], "key": ["fixture-dsf-key"]})
    assert not key_session.key_saved


def test_stale_saved_key_is_not_reported_as_success(
    key_session: phone_setup.Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Access, "_keychain_load", lambda self, provider: "old-fixture-key")
    with pytest.raises(ValueError, match="persistence failed"):
        key_session.submit({"csrf": [key_session.token], "action": ["dsh"],
                            "key": ["new-fixture-key"]})
    assert not key_session.key_saved


@pytest.mark.parametrize("origin,keys_only,allowed", [
    ("https://temporary.trycloudflare.com", "dsh", True),
    ("https://temporary.trycloudflare.com", None, False),
    ("https://trycloudflare.com.attacker.invalid", "dsh", False),
])
def test_temporary_relay_is_only_allowed_for_key_entry(
    tmp_path: Path, origin: str, keys_only: str | None, allowed: bool,
) -> None:
    if allowed:
        session = phone_setup.Session(tmp_path, origin, keys_only=keys_only)
        assert session.process is None
        session.close()
    else:
        with pytest.raises(ValueError):
            phone_setup.Session(tmp_path, origin, keys_only=keys_only)
