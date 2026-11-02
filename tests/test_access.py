from pathlib import Path

import pytest

from orchestragent.access import Access
from orchestragent.store import Conflict


@pytest.fixture
def access(tmp_path: Path) -> Access:
    return Access(tmp_path)


@pytest.fixture
def granted(access: Access) -> Access:
    operation = access.operation("dsh", 500, "balance")
    access.authorize("dsh", 500, "balance", str(operation["operation_digest"]), "fake-test-secret")
    return access


def test_a_recorded_cap_never_blocks_a_call(granted: Access) -> None:
    # No local spending gate: only the provider's own balance (HTTP 402) stops calls.
    assert (
        granted.reserve("dsh", "first", 300),
        granted.reserve("dsh", "second", 300),
        granted.status("dsh")["reserved_cents"],
    ) == (True, True, 600)


def test_a_call_without_a_configured_key_is_not_reserved(access: Access) -> None:
    assert (access.reserve("dsh", "first", 300),
            access.status("dsh")["reserved_cents"]) == (False, 0)


def test_duplicate_reservation_never_replays(granted: Access) -> None:
    granted.reserve("dsh", "same", 300)
    with pytest.raises(Conflict):
        granted.reserve("dsh", "same", 300)


def test_credentials_do_not_survive_restart_or_enter_database(granted: Access) -> None:
    root = granted.store.path.parent
    reopened = Access(root)
    assert (
        reopened.key("dsh"),
        reopened.status("dsh")["authorized"],
        b"fake-test-secret" in granted.store.path.read_bytes(),
    ) == ("", True, False)


@pytest.mark.parametrize(
    "cap,kind", [(-1, "balance"), (50100, "budget"), (True, "balance"), (500, "unknown")]
)
def test_invalid_grants(access: Access, cap: int, kind: str) -> None:
    with pytest.raises(ValueError):
        access.operation("dsh", cap, kind)


def test_approval_digest_binds_amount(access: Access) -> None:
    operation = access.operation("dsh", 500, "balance")
    with pytest.raises(Conflict):
        access.authorize("dsh", 600, "balance", str(operation["operation_digest"]), "fake-test-key")
