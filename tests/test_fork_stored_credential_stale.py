"""D5 (#1381): a dead STORED copy is not a dead account.

cswap quarantines a slot after ``invalid_grant`` and upstream reports every
quarantine as "re-login needed". For an idle slot the credential that failed is
cswap's own backup, and cswap has no view of the credential that slot's profile
may actually hold. On this estate that inverted the allowance picture: the
account with the most headroom (bbconsult@jeptech.co.uk, re-logged outside
cswap) was reported unusable and work steered onto nearly-exhausted accounts.

The quarantine itself is correct and unchanged in every test here — cswap has no
credential it can fetch with either way. What changes is the claim cswap makes
about the ACCOUNT.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.json_output import (
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
    USAGE_STORED_CREDENTIAL_STALE,
    usage_fields,
)
from claude_swap.switcher import (
    _DEAD_LIVE,
    _DEAD_REASON_SENTINELS,
    _DEAD_STORED_ONLY,
    _DEAD_UNPROVEN,
    ClaudeAccountSwitcher,
)
from claude_swap.usage_store import FetchRecord


def _creds(refresh: str) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": "at", "refreshToken": refresh, "expiresAt": 1,
    }})


def _strike(switcher, num: str, email: str, struck: str) -> None:
    """Record one invalid_grant bound to ``struck``'s generation."""
    identities = {num: (email, "")}
    store = switcher._usage_store
    claims = store.reserve([num], identities, respect_plans=False)
    store.record(
        {num: FetchRecord(
            error="invalid_grant",
            struck_fp=oauth.credential_fingerprint(struck),
        )},
        identities, claims,
    )


class TestStoredCredentialStale:
    def test_idle_slot_with_a_struck_backup_is_not_called_expired(self, temp_home):
        """The bbmax case: re-logged outside cswap, so only the backup died."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        dead = _creds("rt-dead")
        _strike(s, "2", "b@example.com", dead)
        info = [(2, "b@example.com", "Org", "", False, dead, "")]

        with patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch:
            entries = s._collect_usage_entries(info)

        assert entries["2"].sentinel == USAGE_STORED_CREDENTIAL_STALE
        # Quarantine is the part that must NOT change.
        fetch.assert_not_called()

    def test_active_slot_whose_live_credential_died_still_says_relogin(
        self, temp_home
    ):
        """The one case cswap can prove: the credential in use is the dead one."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        dead = _creds("rt-dead")
        _strike(s, "2", "b@example.com", dead)
        # is_active=True, and info[5] is the LIVE credential for the active slot.
        info = [(2, "b@example.com", "Org", "", True, dead, "")]

        with patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch:
            entries = s._collect_usage_entries(info)

        assert entries["2"].sentinel == USAGE_RELOGIN_REQUIRED
        fetch.assert_not_called()

    def test_active_slot_with_a_fresh_live_credential_and_struck_backup(
        self, temp_home
    ):
        """Live credential replaced, cswap's backup still spent: not locked out."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        dead = _creds("rt-dead")
        s._write_account_credentials("2", "b@example.com", dead)
        _strike(s, "2", "b@example.com", dead)
        live = _creds("rt-new")
        info = [(2, "b@example.com", "Org", "", True, live, "")]

        entries = s._collect_usage_entries(info, fetch=set())

        assert entries["2"].sentinel == USAGE_STORED_CREDENTIAL_STALE

    def test_an_unreadable_second_source_says_cannot_tell(self, temp_home):
        """Struck, live credential clean, backup unreadable: no verdict is owed.

        Reporting "re-login needed" here would dress "I could not look" as a
        proof about the user's account.
        """
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        dead = _creds("rt-dead")
        _strike(s, "2", "b@example.com", dead)
        live = _creds("rt-new")
        info = [(2, "b@example.com", "Org", "", True, live, "")]

        with patch.object(
            s, "_read_account_credentials_ex", return_value=("", True)
        ):
            entries = s._collect_usage_entries(info, fetch=set())

        assert entries["2"].sentinel == USAGE_KEYCHAIN_UNAVAILABLE

    @pytest.mark.parametrize("reason", [_DEAD_LIVE, _DEAD_STORED_ONLY, _DEAD_UNPROVEN])
    def test_every_reason_maps_to_a_sentinel(self, reason):
        """A reason with no mapping would raise KeyError at the display site."""
        assert reason in _DEAD_REASON_SENTINELS

    def test_quarantine_and_reason_cannot_drift_apart(self, temp_home):
        """``_entry_token_dead`` is exactly "a reason was named"."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        dead = _creds("rt-dead")
        _strike(s, "2", "b@example.com", dead)
        entry = s._usage_store.entries({"2": ("b@example.com", "")}, ())["2"]

        for is_active in (True, False):
            reason = s._token_dead_reason(
                entry, "2", "b@example.com", dead, is_active
            )
            dead_bool = s._entry_token_dead(
                entry, "2", "b@example.com", dead, is_active
            )
            assert dead_bool is (reason is not None)

    def test_json_status_is_its_own_value_with_no_usage(self):
        """CodexBar reads ``usageStatus``; the new state must not borrow one."""
        assert usage_fields(USAGE_STORED_CREDENTIAL_STALE) == (
            "stored_credential_stale", None,
        )
        assert usage_fields(USAGE_RELOGIN_REQUIRED) == ("relogin_required", None)
