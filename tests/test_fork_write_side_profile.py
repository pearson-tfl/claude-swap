"""Fork fix D1 (upstream #206): the active-credential WRITE must stay on one profile.

Upstream fixed the read and capture sides (#190, #205): both resolve the
Keychain item claude would read for the current environment. The write side was
left on the hardcoded, unsuffixed ``CLAUDE_CODE_KEYCHAIN_SERVICE``, which names
the DEFAULT profile's item only. Under ``CLAUDE_CONFIG_DIR`` that splits a
switch across two profiles:

- the target account's credential lands in the DEFAULT profile's Keychain item,
  a cross-profile write that later rotations invalidate; and
- the profile the caller is actually in keeps its old hashed item, which is the
  one claude reads there — so the switch reports success and changes nothing.

The fix routes the write and the stale-item delete through the same resolver the
read uses, so the two agree by construction rather than by coincidence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CredentialStore,
)
from claude_swap.models import Platform
from claude_swap.session import keychain_service_name


class _Host:
    """Minimal ``_StoreHost``: data only, read at call time."""

    def __init__(self, credentials_dir: Path):
        self.platform = Platform.MACOS
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("test")


CREDS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-target",
        "refreshToken": "rt-target",
        "expiresAt": 9999999999000,
    }
})


def _record_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture ``(service, value)`` for every Keychain write. Never a real one."""
    written: list[tuple[str, str]] = []

    def fake_set_password(service: str, account: str, value: str) -> None:
        written.append((service, value))

    monkeypatch.setattr("claude_swap.macos_keychain.set_password", fake_set_password)
    return written


def _record_deletes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    deleted: list[str] = []

    def fake_delete_password(service: str, account: str) -> None:
        deleted.append(service)

    monkeypatch.setattr(
        "claude_swap.macos_keychain.delete_password", fake_delete_password
    )
    return deleted


class TestActiveWriteStaysOnOneProfile:
    def test_custom_config_dir_writes_its_own_hashed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The defect in one line: under a custom profile the write went to the
        default profile's item, so claude in that profile never saw the switch."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        written = _record_writes(monkeypatch)

        CredentialStore(_Host(tmp_path / "backups"))._write_oauth_credentials(CREDS)

        assert [s for s, _ in written] == [keychain_service_name(str(custom))]
        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in [s for s, _ in written], (
            "wrote the default profile's item from inside a custom profile"
        )

    def test_default_profile_still_writes_the_unsuffixed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The common case is unchanged: no env var means the unsuffixed item
        IS this profile's credential."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        written = _record_writes(monkeypatch)

        CredentialStore(_Host(tmp_path / "backups"))._write_oauth_credentials(CREDS)

        assert [s for s, _ in written] == [CLAUDE_CODE_KEYCHAIN_SERVICE]

    def test_config_dir_naming_the_default_profile_writes_both_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``CLAUDE_CONFIG_DIR=~/.claude`` exported: claude hashes the exported
        string and reads the SUFFIXED item, but a user who has also run without
        the variable has an unsuffixed one. They are the same profile and the
        same account, so both are written — leaving one stale is what lets the
        two disagree about which account is logged in.

        The read resolver already lists both for this case; the write follows it,
        which is the point of routing them through one resolver."""
        default = tmp_path / ".claude"
        default.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        written = _record_writes(monkeypatch)

        CredentialStore(_Host(tmp_path / "backups"))._write_oauth_credentials(CREDS)

        assert [s for s, _ in written] == [
            keychain_service_name(str(default)),
            CLAUDE_CODE_KEYCHAIN_SERVICE,
        ]
        assert {v for _, v in written} == {CREDS}, "the two items must not diverge"

    def test_secure_storage_override_pins_the_single_store_claude_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A defined ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` names the ONLY secure
        store claude reads for this environment. Writing anywhere else is a
        cross-profile write, so the resolver's single answer is the whole list."""
        secure = tmp_path / "secure-profile"
        secure.mkdir()
        other = tmp_path / "config-profile"
        other.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure))
        written = _record_writes(monkeypatch)

        CredentialStore(_Host(tmp_path / "backups"))._write_oauth_credentials(CREDS)

        assert [s for s, _ in written] == [keychain_service_name(str(secure))]

    def test_empty_secure_storage_override_selects_the_default_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Defined-but-empty is claude's spelling of "the default secure store",
        whose item is the unsuffixed one — even under a custom config dir."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
        written = _record_writes(monkeypatch)

        CredentialStore(_Host(tmp_path / "backups"))._write_oauth_credentials(CREDS)

        assert [s for s, _ in written] == [CLAUDE_CODE_KEYCHAIN_SERVICE]


class TestStaleItemDeleteStaysOnOneProfile:
    def test_delete_targets_this_profile_not_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The delete exists so claude's keychain-first read cannot shadow a
        plaintext fallback. Deleting the DEFAULT profile's item instead does
        both halves of the damage: it logs the default profile out, and the
        profile that fell back keeps the stale item that shadows its file."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        deleted = _record_deletes(monkeypatch)

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._delete_active_keychain_entry() is True
        assert deleted == [keychain_service_name(str(custom))]
        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in deleted

    def test_delete_reports_failure_when_any_item_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The return value is the caller's proof that nothing can shadow the
        file. With more than one item in scope, one surviving item is enough to
        shadow, so the verdict is the conjunction — not the last call's luck."""
        default = tmp_path / ".claude"
        default.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        hashed = keychain_service_name(str(default))
        attempted: list[str] = []

        def fake_delete_password(service: str, account: str) -> None:
            attempted.append(service)
            if service == hashed:
                raise OSError("keychain down")

        monkeypatch.setattr(
            "claude_swap.macos_keychain.delete_password", fake_delete_password
        )

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._delete_active_keychain_entry() is False
        assert attempted == [hashed, CLAUDE_CODE_KEYCHAIN_SERVICE], (
            "a failure on the first item must not skip the second — the "
            "unattempted one would be the residual that shadows the file"
        )
