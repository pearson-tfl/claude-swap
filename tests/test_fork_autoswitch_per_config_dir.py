"""Fork fix D4 (upstream #253): switch history is per config dir, not global.

Two ``cswap auto`` engines running under different ``CLAUDE_CONFIG_DIR``
profiles share one ``autoswitch_state.json``. Every successful switch writes a
six-key cluster describing the switch ONE live config just made, at the top
level, so each engine reads the other's history as its own:

- a switch in one profile starts a cooldown that suppresses the other;
- the no-return bar fires on ``lastSwitchTo == current``, so an engine can
  inherit another profile's bar and judge it against that profile's baselines;
- a foreign switch rewrites ``lastSwitchTo``, which disarms the own-landing
  check that arms the bar, while replacing the departure snapshot the release
  predicate diffs against.

The six keys move under ``perConfigDir[<normalised config home>]``. Quarantine
is a fact about an ACCOUNT, so it stays top-level and shared. No decision logic
changes: every gate reads the same values through one view helper, now keyed by
the config dir they describe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_swap.autoswitch import (
    SWITCH_HISTORY_KEYS,
    config_dir_key,
    record_switch_history,
    switch_history,
)
from claude_swap.settings import AutoSwitchSettings

from tests.test_autoswitch import EngineHarness


def _profile(monkeypatch: pytest.MonkeyPatch, path: Path) -> str:
    """Point the live environment at ``path`` and return its slice key."""
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    return config_dir_key()


LEGACY_RECORD = {
    "lastSwitchAt": 1000.0,
    "lastSwitchTo": "2",
    "lastSwitchFrom": 1,
    "leftHeadroom": 4.0,
    "leftRecoveryAt": None,
    "leftTrigger": "proactive",
}


class TestSliceReadAndWrite:
    def test_a_foreign_profiles_slice_is_not_this_engines_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        other = config_dir_key(tmp_path / "profile-a")
        _profile(monkeypatch, tmp_path / "profile-b")
        state = {"perConfigDir": {other: dict(LEGACY_RECORD)}}

        assert switch_history(state) == {}

    def test_an_engine_reads_back_its_own_slice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _profile(monkeypatch, tmp_path / "profile-b")
        state: dict = {}
        record_switch_history(state, LEGACY_RECORD)

        assert switch_history(state) == LEGACY_RECORD
        assert set(state) == {"perConfigDir"}, (
            "the cluster must not also stay at the top level, or a foreign "
            "engine's legacy fallback would read it"
        )

    def test_equivalent_spellings_share_one_slice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A trailing slash and a symlink are two spellings of one profile.
        Deriving two keys would give one engine two histories and neither of
        them the whole truth."""
        real = tmp_path / "profile-real"
        real.mkdir()
        link = tmp_path / "profile-link"
        link.symlink_to(real)

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(real))
        state: dict = {}
        record_switch_history(state, LEGACY_RECORD)

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(real) + os.sep)
        assert switch_history(state) == LEGACY_RECORD
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
        assert switch_history(state) == LEGACY_RECORD

    def test_a_corrupt_container_falls_back_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The state file is rebuilt from nothing when unreadable, so a wrong
        shape inside it must degrade the same way — never raise into a tick."""
        _profile(monkeypatch, tmp_path / "profile-b")
        state = {"perConfigDir": "not-a-mapping", **LEGACY_RECORD}
        assert switch_history(state) == LEGACY_RECORD

        state = {"perConfigDir": {config_dir_key(): "not-a-mapping"}}
        assert switch_history(state) == {}

        record_switch_history(state, LEGACY_RECORD)
        assert switch_history(state) == LEGACY_RECORD

    def test_quarantine_stays_top_level_and_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Quarantine is a fact about an ACCOUNT — a dead refresh-token lineage
        is dead in every profile — so it must not be sliced with the history."""
        _profile(monkeypatch, tmp_path / "profile-b")
        state = {"quarantine": {"3": {"reason": "invalid_grant"}}}
        record_switch_history(state, LEGACY_RECORD)

        assert state["quarantine"] == {"3": {"reason": "invalid_grant"}}


class TestPreUpgradeRecords:
    def test_a_top_level_record_is_honoured_until_the_first_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Downgrade safety: a file written by an older cswap has no slices.
        Ignoring it would silently drop the anti-flap bar for an upgrade
        cycle, which is the over-permissive direction this history never
        takes."""
        _profile(monkeypatch, tmp_path / "profile-b")
        assert switch_history(dict(LEGACY_RECORD)) == LEGACY_RECORD

    def test_a_foreign_engine_retiring_a_record_moves_it_to_the_default_slice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Pre-upgrade autoswitch ran the default profile in the common case,
        so an unattributed record most likely belongs to it. Holding a bar
        that may not be yours is the over-conservative direction; dropping one
        that is yours is not."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        default_key = config_dir_key(tmp_path / ".claude")
        _profile(monkeypatch, tmp_path / "profile-b")

        state = dict(LEGACY_RECORD)
        new_record = {**LEGACY_RECORD, "lastSwitchAt": 2000.0, "lastSwitchTo": "3"}
        record_switch_history(state, new_record)

        assert state["perConfigDir"][default_key] == LEGACY_RECORD
        assert switch_history(state) == new_record
        assert not any(k in state for k in SWITCH_HISTORY_KEYS)

    def test_the_move_never_overwrites_an_existing_default_slice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The default profile's own engine has already spoken for itself. An
        unattributed record is a guess; a real slice is a measurement, and a
        guess must never replace one."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        default_key = config_dir_key(tmp_path / ".claude")
        _profile(monkeypatch, tmp_path / "profile-b")

        owned = {**LEGACY_RECORD, "lastSwitchAt": 5000.0, "lastSwitchTo": "9"}
        state = {"perConfigDir": {default_key: owned}, **LEGACY_RECORD}
        record_switch_history(state, {**LEGACY_RECORD, "lastSwitchAt": 2000.0})

        assert state["perConfigDir"][default_key] == owned
        assert not any(k in state for k in SWITCH_HISTORY_KEYS)

    def test_the_default_profiles_own_switch_just_replaces_the_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No move to make: the retiring engine IS the default profile, so its
        slice replaces its own record rather than copying it aside."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        default = tmp_path / ".claude"
        default_key = _profile(monkeypatch, default)

        state = dict(LEGACY_RECORD)
        new_record = {**LEGACY_RECORD, "lastSwitchAt": 2000.0}
        record_switch_history(state, new_record)

        assert state["perConfigDir"] == {default_key: new_record}
        assert not any(k in state for k in SWITCH_HISTORY_KEYS)


class TestGatesReadTheirOwnSlice:
    """The point of the change: the decision gates, unaltered, now read the
    slice their own config dir owns."""

    def _engine(self, temp_home: Path):
        h = EngineHarness(temp_home, cooldown_seconds=600)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        return h

    def test_a_foreign_switch_does_not_put_this_engine_in_cooldown(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        h = self._engine(temp_home)
        foreign = config_dir_key(tmp_path / "profile-a")
        _profile(monkeypatch, tmp_path / "profile-b")
        h.clock.now = 1100.0

        state = {"perConfigDir": {foreign: dict(LEGACY_RECORD)}}
        assert h.engine._in_cooldown(state) is False

    def test_this_engines_own_switch_still_does(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        h = self._engine(temp_home)
        own = _profile(monkeypatch, tmp_path / "profile-b")
        h.clock.now = 1100.0

        state = {"perConfigDir": {own: dict(LEGACY_RECORD)}}
        assert h.engine._in_cooldown(state) is True

    def test_a_pre_upgrade_record_still_holds_the_cooldown(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        h = self._engine(temp_home)
        _profile(monkeypatch, tmp_path / "profile-b")
        h.clock.now = 1100.0

        assert h.engine._in_cooldown(dict(LEGACY_RECORD)) is True

    def test_a_foreign_profiles_bar_does_not_bind_this_engine(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The cross-talk that bites hardest: engine B inherits A's bar and
        judges account 1 against A's departure baselines."""
        h = self._engine(temp_home)
        foreign = config_dir_key(tmp_path / "profile-a")
        _profile(monkeypatch, tmp_path / "profile-b")
        settings = AutoSwitchSettings(threshold=80)

        state = {"perConfigDir": {foreign: dict(LEGACY_RECORD)}}
        assert h.engine._no_return_account(
            "proactive", state, {"1": 4.0}, 2.0, False, settings, current="2"
        ) is None

    def test_this_engines_own_bar_still_binds(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        h = self._engine(temp_home)
        own = _profile(monkeypatch, tmp_path / "profile-b")
        settings = AutoSwitchSettings(threshold=80)

        state = {"perConfigDir": {own: dict(LEGACY_RECORD)}}
        assert h.engine._no_return_account(
            "proactive", state, {"1": 4.0}, 2.0, False, settings, current="2"
        ) == "1"

    def test_the_release_predicate_reads_its_own_departure_snapshot(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``leftHeadroom`` absent means "pre-upgrade record, no evidence,
        release". A foreign slice must read as absent here too, or one
        profile's departure baseline decides another's release."""
        h = self._engine(temp_home)
        foreign = config_dir_key(tmp_path / "profile-a")
        _profile(monkeypatch, tmp_path / "profile-b")
        settings = AutoSwitchSettings(threshold=80)

        state = {"perConfigDir": {foreign: dict(LEGACY_RECORD)}}
        assert h.engine._left_account_recovered(
            state, {}, {"1": 4.0}, 2.0, settings, 1100.0, current="2"
        ) is True


class TestSwitchWritesItsOwnSlice:
    def test_a_recorded_switch_lands_in_this_profiles_slice_only(
        self, temp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """End to end through the state file: the keys a switch writes are the
        six the gates read, and they land under this profile's key."""
        h = EngineHarness(temp_home, cooldown_seconds=600)
        own = _profile(monkeypatch, tmp_path / "profile-b")

        def record(state: dict) -> None:
            record_switch_history(state, LEGACY_RECORD)

        h.engine._mutate_state(record)
        written = json.loads(h.engine.state_path.read_text(encoding="utf-8"))

        assert written["perConfigDir"][own] == LEGACY_RECORD
        assert not any(k in written for k in SWITCH_HISTORY_KEYS)
        assert written["schemaVersion"] == 1, (
            "no schema bump: the read path honours pre-upgrade records, and an "
            "older cswap reading this file lands in its absence-releases paths"
        )
