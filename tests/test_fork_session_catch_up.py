"""A dead stored copy catches up from the slot's own session folder (keep-fresh).

Bug 1: the usage read stopped at the dead mark, so the catch-up paths behind it
(adopt a quiet folder's newer login, read a live seat's read-only) were never
reached and one dead copy stayed dead for good.

Bug 2: ``cswap run N`` re-seeded an invalid folder from a copy marked dead,
which signed the folder out on claude's first refresh.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.exceptions import LockError, SessionError
from claude_swap.json_output import USAGE_STORED_CREDENTIAL_STALE
from claude_swap.session import mark_session_stale
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord
from tests.test_session import (  # noqa: F401 - pytest fixtures
    ACCOUNT_EMAIL,
    ACCOUNT_NUM,
    CREDS,
    ORG_UUID,
    ROTATED_CREDS,
    auth_status_tracks_seed,
    macos_platform,
    manager,
    refresh_rotates,
    seeded_switcher,
)

EMAIL = "b@example.com"
ORG = "org-b"


def _creds(token: str, expires_in_s: float) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": token,
        "refreshToken": f"rt-{token}",
        "expiresAt": int((time.time() + expires_in_s) * 1000),
    }})


def _strike(switcher, num: str, email: str, org: str, struck: str) -> None:
    """Record one invalid_grant bound to ``struck``'s generation."""
    identities = {num: (email, org)}
    store = switcher._usage_store
    claims = store.reserve([num], identities, respect_plans=False)
    store.record(
        {num: FetchRecord(
            error="invalid_grant",
            struck_fp=oauth.credential_fingerprint(struck),
        )},
        identities, claims,
    )


def _seed_profile(switcher, creds: str, email: str = EMAIL) -> Path:
    session_dir = switcher._session_dir("2", EMAIL)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / ".credentials.json").write_text(creds)
    (session_dir / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": email, "organizationUuid": ORG}}
    ))
    return session_dir


def _make_live(session_dir: Path) -> None:
    records = session_dir / "sessions"
    records.mkdir(exist_ok=True)
    (records / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))


@pytest.fixture
def struck(temp_home):
    """Idle slot 2 whose stored copy is marked dead."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    dead = _creds("sk-dead", -3600)
    s._write_account_credentials("2", EMAIL, dead)
    _strike(s, "2", EMAIL, ORG, dead)
    return s, dead


def _collect(s, dead):
    info = [(2, EMAIL, "Org", ORG, False, dead, "")]
    usage = oauth.UsageOutcome({"five_hour": {"pct": 5}})
    with patch(
        "claude_swap.oauth.try_fetch_usage_for_account", return_value=usage
    ) as fetch:
        entries = s._collect_usage_entries(info)
    return entries["2"], fetch


def _strikes(s) -> int:
    return s._usage_store.entries({"2": (EMAIL, ORG)})["2"].auth_dead_strikes


class TestReadCatchesUpFromTheSessionFolder:
    def test_quiet_folder_with_a_later_login_is_adopted_and_heals(self, struck):
        s, dead = struck
        fresh = _creds("sk-fresh", 7200)
        _seed_profile(s, fresh)

        entry, fetch = _collect(s, dead)

        assert s.read_account_credentials("2", EMAIL) == fresh
        assert entry.sentinel is None
        assert _strikes(s) == 0
        # The fetch used the adopted login, never the dead copy.
        assert fetch.call_args.args[2] == fresh

    def test_live_seat_is_read_read_only_and_never_written(self, struck):
        s, dead = struck
        fresh = _creds("sk-fresh", 7200)
        session_dir = _seed_profile(s, fresh)
        _make_live(session_dir)

        entry, fetch = _collect(s, dead)

        assert s.read_account_credentials("2", EMAIL) == dead  # untouched
        assert (session_dir / ".credentials.json").read_text() == fresh
        assert entry.sentinel is None
        assert _strikes(s) == 0
        assert fetch.call_args.args[2] == fresh
        assert fetch.call_args.kwargs.get("is_active") is True  # no refresh

    @pytest.mark.parametrize("case", ["other_account", "older", "stale_marker"])
    def test_refused_catch_up_keeps_the_mark_and_both_copies(self, struck, case):
        s, dead = struck
        if case == "other_account":
            profile = _creds("sk-fresh", 7200)
            session_dir = _seed_profile(s, profile, email="x@example.com")
        elif case == "older":
            profile = _creds("sk-old", -7200)
            session_dir = _seed_profile(s, profile)
        else:
            profile = _creds("sk-fresh", 7200)
            session_dir = _seed_profile(s, profile)
            mark_session_stale(session_dir)

        entry, fetch = _collect(s, dead)

        assert entry.sentinel == USAGE_STORED_CREDENTIAL_STALE
        fetch.assert_not_called()
        assert s.read_account_credentials("2", EMAIL) == dead
        assert (session_dir / ".credentials.json").read_text() == profile
        assert _strikes(s) == 1

    def test_unreadable_folder_login_keeps_the_mark(self, struck):
        s, dead = struck
        _seed_profile(s, _creds("sk-fresh", 7200))

        with patch("claude_swap.session.read_session_credentials", return_value=None):
            entry, fetch = _collect(s, dead)

        assert entry.sentinel == USAGE_STORED_CREDENTIAL_STALE
        fetch.assert_not_called()
        assert s.read_account_credentials("2", EMAIL) == dead
        assert _strikes(s) == 1

    def test_lock_contention_keeps_the_mark(self, struck):
        s, dead = struck
        _seed_profile(s, _creds("sk-fresh", 7200))

        with patch.object(
            s, "_adopt_session_credential", side_effect=LockError("busy")
        ):
            entry, fetch = _collect(s, dead)

        assert entry.sentinel == USAGE_STORED_CREDENTIAL_STALE
        fetch.assert_not_called()
        assert s.read_account_credentials("2", EMAIL) == dead


class TestRunRefusesToSeedFromADeadCopy:
    def _strike_backup(self, switcher):
        _strike(switcher, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID, CREDS)

    def test_signed_out_folder_is_not_seeded(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        self._strike_backup(seeded_switcher)
        session_dir = seeded_switcher._session_dir(ACCOUNT_NUM, ACCOUNT_EMAIL)

        with pytest.raises(SessionError, match="Sign in instead"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert not (session_dir / ".credentials.json").exists()
        assert refresh_rotates == []  # the dead grant is not POSTed either

    def test_stale_marked_folder_is_not_wiped_and_reseeded(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        session_dir = seeded_switcher._session_dir(ACCOUNT_NUM, ACCOUNT_EMAIL)
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(ROTATED_CREDS)
        mark_session_stale(session_dir)
        self._strike_backup(seeded_switcher)

        with pytest.raises(SessionError, match="Sign in instead"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS

    def test_signed_in_folder_still_launches(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        """The refusal guards the seed only; a working folder is reused."""
        session_dir = seeded_switcher._session_dir(ACCOUNT_NUM, ACCOUNT_EMAIL)
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(ROTATED_CREDS)
        (session_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": ACCOUNT_EMAIL, "organizationUuid": ORG_UUID,
            },
            "hasCompletedOnboarding": True,
        }))
        self._strike_backup(seeded_switcher)

        got, _, _ = manager.setup_session(ACCOUNT_NUM, share=False)

        assert got == session_dir
        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS
