"""Fork fixes D3 and D2: which account is this profile actually on?

A Claude Code profile holds its account identity in two places that are written
by two different programs:

- ``.claude.json`` ``oauthAccount`` — written by Claude Code at ``/login``;
- the macOS Keychain item for that config dir — written by Claude Code, and
  (from D1 on) by ``cswap switch``.

They can disagree. cswap reads the identity from the first and the credential
from the second, so a disagreement makes it label one account's quota, history
and switch decisions with another account's name — silently, because each read
on its own is correct. Nothing in cswap answered "do these two agree?", so no
agent could read its own account reliably.

This is upstream #217's "cheaper mitigation" (compare the credential's account
against the slot's recorded identity and surface the mismatch) generalised to
the profile, and it is the estate's restatement of D3.

The attribution is OFFLINE and it is proof, not inference: a live credential
whose fingerprint equals slot N's stored backup IS slot N's credential. The
network probe is an extra, asked for explicitly, for the case where the live
bytes match no stored backup.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_swap import identity
from claude_swap.identity import Verdict
from claude_swap.models import Platform

A_CREDS = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-a", "refreshToken": "rt-a", "expiresAt": 9999999999000}})
B_CREDS = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-b", "refreshToken": "rt-b", "expiresAt": 9999999999000}})
UNKNOWN_CREDS = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-x", "refreshToken": "rt-x", "expiresAt": 9999999999000}})


class _Switcher:
    """The reads ``identity`` makes of a switcher, and nothing else."""

    def __init__(self, *, config_identity, slots, live, degraded=False):
        self._config_identity = config_identity
        self._slots = slots            # {num: (email, org_uuid, creds)}
        self._live = live
        self._degraded = degraded

    def _get_current_identity_triple(self):
        return self._config_identity

    def _get_sequence_data_migrated(self):
        return {
            "sequence": sorted(int(n) for n in self._slots),
            "accounts": {
                n: {"email": e, "organizationUuid": o}
                for n, (e, o, _) in self._slots.items()
            },
        }

    def _read_account_credentials(self, num, email):
        return self._slots.get(num, (None, None, ""))[2]

    def _read_active_credentials(self):
        from claude_swap.credentials import ActiveCredentials

        return ActiveCredentials(self._live, False, self._degraded)


def _switcher(**kw):
    kw.setdefault("slots", {
        "1": ("a@example.com", "org-a", A_CREDS),
        "2": ("b@example.com", "org-b", B_CREDS),
    })
    return _Switcher(**kw)


class TestOfflineAttribution:
    def test_agreement_when_the_credential_is_the_named_accounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=A_CREDS
        )

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.AGREE
        assert result.config_email == "a@example.com"
        assert result.credential_email == "a@example.com"
        assert result.credential_slot == "1"

    def test_disagreement_is_proven_offline_by_the_fingerprint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The defect in one line. ``.claude.json`` names account A; the
        credential this profile actually presents is byte-identical to the
        backup cswap holds for account B, so it IS account B's. No network, no
        inference, and no way to read it as anything else."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=B_CREDS
        )

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.DISAGREE
        assert result.config_email == "a@example.com"
        assert result.credential_email == "b@example.com"
        assert result.credential_slot == "2"
        assert "a@example.com" in result.detail and "b@example.com" in result.detail

    def test_an_unmatched_credential_is_unverified_not_agreed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A credential matching no stored backup is the ordinary state of a
        profile cswap has never captured. Absence of a match is not evidence of
        agreement, and calling it agreement is the silent wrong answer this
        whole reader exists to remove."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=UNKNOWN_CREDS
        )

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.UNVERIFIED
        assert result.credential_slot is None

    def test_the_same_email_in_two_orgs_is_not_a_disagreement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Two managed slots may share one email across organisations, so the
        comparison is (email, org) — an email-only match would report a
        disagreement that is not one."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            slots={
                "1": ("a@example.com", "org-one", A_CREDS),
                "2": ("a@example.com", "org-two", B_CREDS),
            },
            config_identity=("a@example.com", "org-two", "uuid-a"),
            live=B_CREDS,
        )

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.AGREE
        assert result.credential_slot == "2"

    def test_the_same_email_in_the_wrong_org_is_a_disagreement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            slots={
                "1": ("a@example.com", "org-one", A_CREDS),
                "2": ("a@example.com", "org-two", B_CREDS),
            },
            config_identity=("a@example.com", "org-two", "uuid-a"),
            live=A_CREDS,
        )

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.DISAGREE


class TestTheStatesThatAreNotDisagreements:
    def test_a_profile_with_neither_source_is_logged_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(config_identity=None, live="")

        assert identity.read_profile_account(s).verdict == Verdict.LOGGED_OUT

    def test_a_named_account_with_no_credential_is_reported_as_such(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``.claude.json`` claims an account nothing backs. Claude Code will
        ask this profile to log in again, so an agent reading only the config
        file would name an account it cannot use."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(config_identity=("a@example.com", "org-a", "uuid-a"), live="")

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.CREDENTIAL_MISSING
        assert result.config_email == "a@example.com"

    def test_an_unreadable_keychain_is_never_reported_as_a_disagreement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A degraded read may be serving a superseded generation, which can
        match a stale backup or none at all. Neither answer is evidence about
        the live account, so the verdict must say "could not tell"."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"),
            live=B_CREDS,
            degraded=True,
        )

        result = identity.read_profile_account(s)

        assert result.verdict == Verdict.KEYCHAIN_UNREADABLE


class TestTheNetworkProbeIsOptAndOnlyAnExtra:
    def test_no_probe_runs_unless_it_is_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``cswap --list --json`` is polled every few minutes by CodexBar. A
        reader that reached the network by default would put a request on that
        path, so the default is strictly offline."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        calls: list[str] = []
        monkeypatch.setattr(
            "claude_swap.oauth.fetch_oauth_profile",
            lambda token: calls.append(token),
        )
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=UNKNOWN_CREDS
        )

        identity.read_profile_account(s)

        assert calls == []

    def test_the_probe_resolves_an_otherwise_unverifiable_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "claude_swap.oauth.fetch_oauth_profile",
            lambda token: {
                "uuid": "uuid-c",
                "email": "c@example.com",
                "organizationUuid": "org-c",
            },
        )
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=UNKNOWN_CREDS
        )

        result = identity.read_profile_account(s, verify=True)

        assert result.verdict == Verdict.DISAGREE
        assert result.resolved_email == "c@example.com"

    def test_an_unresolvable_probe_leaves_the_verdict_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``fetch_oauth_profile`` answers None for every failure — offline,
        401, a schema change. None is "unresolvable", never "different"."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "claude_swap.oauth.fetch_oauth_profile", lambda token: None
        )
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=UNKNOWN_CREDS
        )

        result = identity.read_profile_account(s, verify=True)

        assert result.verdict == Verdict.UNVERIFIED

    def test_the_offline_proof_is_not_re_asked_over_the_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A fingerprint match is already proof. Spending a request to
        re-confirm it would also let a probe failure downgrade a proven
        verdict, which is strictly worse than not asking."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        calls: list[str] = []
        monkeypatch.setattr(
            "claude_swap.oauth.fetch_oauth_profile",
            lambda token: calls.append(token),
        )
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=B_CREDS
        )

        result = identity.read_profile_account(s, verify=True)

        assert result.verdict == Verdict.DISAGREE
        assert calls == []


class TestTheProfileItIsReportingOn:
    def test_the_config_dir_and_its_keychain_service_are_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The point of the reader for an agent: name the directory and the
        Keychain item it just read, so the answer can be checked rather than
        trusted. The service name is derived, never stored — renaming a
        profile directory changes it and orphans the credential."""
        from claude_swap.session import keychain_service_name

        profile = tmp_path / "harness-blueberry"
        profile.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=A_CREDS
        )

        result = identity.read_profile_account(s)

        assert result.config_dir == str(profile)
        assert result.config_dir_source == "CLAUDE_CONFIG_DIR"
        assert result.keychain_service == keychain_service_name(str(profile))

    def test_the_default_profile_reports_the_unsuffixed_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=A_CREDS
        )

        result = identity.read_profile_account(s)

        assert result.config_dir_source == "default"
        assert result.keychain_service == CLAUDE_CODE_KEYCHAIN_SERVICE

    def test_the_result_serialises_without_any_credential_material(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """This object is printed by ``cswap whoami --json`` and may reach a
        log or a ticket. No token, no fingerprint, no file bytes — emails and
        the profile's own metadata only."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        s = _switcher(
            config_identity=("a@example.com", "org-a", "uuid-a"), live=B_CREDS
        )

        payload = json.dumps(identity.read_profile_account(s).to_json())

        for secret in ("sk-a", "sk-b", "rt-a", "rt-b", "sha256"):
            assert secret not in payload, f"{secret!r} reached the JSON output"


class TestListWarnings:
    """The same proof, on the path CodexBar already polls — offline only, and
    additive, so the ``schemaVersion: 1`` contract is untouched."""

    def _rows(self, active_creds):
        # (num, email, org_name, org_uuid, is_active, creds, alias)
        return [
            (1, "a@example.com", "", "org-a", True, active_creds, ""),
            (2, "b@example.com", "", "org-b", False, B_CREDS, ""),
        ]

    def test_a_clean_profile_warns_about_nothing(self):
        assert identity.active_slot_warnings(self._rows(A_CREDS)) == []

    def test_the_active_slots_credential_belonging_elsewhere_warns(self):
        warnings = identity.active_slot_warnings(self._rows(B_CREDS))

        assert len(warnings) == 1
        assert "a@example.com" in warnings[0] and "b@example.com" in warnings[0]

    def test_no_warning_without_an_active_slot(self):
        rows = [(1, "a@example.com", "", "org-a", False, A_CREDS, "")]
        assert identity.active_slot_warnings(rows) == []

    def test_no_warning_when_the_active_slot_has_no_credential(self):
        assert identity.active_slot_warnings(self._rows("")) == []

    def test_the_warning_carries_no_credential_material(self):
        warnings = identity.active_slot_warnings(self._rows(B_CREDS))
        for secret in ("sk-a", "sk-b", "rt-a", "rt-b", "sha256"):
            assert secret not in warnings[0]


class TestWhoamiCommand:
    """``cswap whoami`` — the command an agent runs to read its own account."""

    def _patch(self, monkeypatch, result):
        import claude_swap.cli as cli

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kw: object())
        monkeypatch.setattr(cli, "_guard_root", lambda s: None)
        monkeypatch.setattr(
            identity, "read_profile_account", lambda s, verify=False: result
        )
        return cli

    def _account(self, **kw):
        fields = dict(
            config_dir="/Users/x/.claude-blueberry",
            config_dir_source="CLAUDE_CONFIG_DIR",
            keychain_service="Claude Code-credentials-deadbeef",
            config_email="a@example.com",
            config_org_uuid="org-a",
            config_account_uuid="uuid-a",
            credential_present=True,
            credential_slot="1",
            credential_email="a@example.com",
            credential_org_uuid="org-a",
            resolved_email=None,
            resolved_org_uuid=None,
            resolved_account_uuid=None,
            verdict=Verdict.AGREE,
            detail="fine",
        )
        fields.update(kw)
        return identity.ProfileAccount(**fields)

    def test_agreement_exits_zero_and_names_the_account(self, monkeypatch, capsys):
        cli = self._patch(monkeypatch, self._account())
        cli._whoami_command([])
        out = capsys.readouterr().out
        assert "a@example.com" in out and "/Users/x/.claude-blueberry" in out

    def test_a_proven_disagreement_exits_one(self, monkeypatch, capsys):
        """An agent must be able to branch on this without parsing prose."""
        cli = self._patch(
            monkeypatch,
            self._account(
                verdict=Verdict.DISAGREE,
                credential_email="b@example.com",
                credential_slot="2",
                detail="names a@example.com but presents b@example.com's",
            ),
        )
        with pytest.raises(SystemExit) as exc:
            cli._whoami_command([])
        assert exc.value.code == 1
        assert "b@example.com" in capsys.readouterr().out

    def test_not_knowing_is_not_a_failure(self, monkeypatch, capsys):
        """``unverified`` exits 0. A caller must never read "could not tell"
        as "the profile is wrong" — that would make every uncaptured profile
        look broken."""
        cli = self._patch(monkeypatch, self._account(verdict=Verdict.UNVERIFIED))
        cli._whoami_command([])
        assert "unverified" in capsys.readouterr().out

    def test_json_output_is_one_object_with_the_schema_version(
        self, monkeypatch, capsys
    ):
        cli = self._patch(monkeypatch, self._account())
        with pytest.raises(SystemExit) as exc:
            cli._whoami_command(["--json"])
        assert exc.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schemaVersion"] == 1
        assert payload["configEmail"] == "a@example.com"
        assert payload["verdict"] == "agree"

    def test_json_output_exits_one_on_a_proven_disagreement(
        self, monkeypatch, capsys
    ):
        cli = self._patch(
            monkeypatch, self._account(verdict=Verdict.DISAGREE)
        )
        with pytest.raises(SystemExit) as exc:
            cli._whoami_command(["--json"])
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["verdict"] == "disagree"

    def test_the_probe_runs_only_when_verify_is_passed(self, monkeypatch):
        import claude_swap.cli as cli

        seen: list[bool] = []
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kw: object())
        monkeypatch.setattr(cli, "_guard_root", lambda s: None)
        monkeypatch.setattr(
            identity,
            "read_profile_account",
            lambda s, verify=False: seen.append(verify) or self._account(),
        )
        cli._whoami_command([])
        cli._whoami_command(["--verify"])
        assert seen == [False, True]


class TestWhoamiEndToEnd:
    """Through ``main()``: dispatch, argparse, the switcher, and the print.

    The unit tests above stub ``read_profile_account``; this one does not, so
    it is what proves the command is wired to the reader rather than to a
    mock. The Keychain is faked at the ``macos_keychain`` boundary — a live run
    is recorded on the ticket for approval instead, because a real
    ``security`` call against the login keychain is out of bounds for this lane.
    """

    def test_a_disagreeing_profile_is_reported_and_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        import claude_swap.cli as cli

        profile = tmp_path / "harness-blueberry"
        profile.mkdir()
        (profile / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "organizationUuid": "org-a",
                "accountUuid": "uuid-a",
            }
        }), encoding="utf-8")
        # The profile presents account B's credential: the D3 shape exactly.
        (profile / ".credentials.json").write_text(B_CREDS, encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password", lambda service, account: None
        )

        switcher_cls = cli.ClaudeAccountSwitcher
        switcher = switcher_cls()
        # File backend for the BACKUPS, so seeding writes .enc files rather
        # than reaching the real Keychain. The profile's own credential still
        # comes from the faked macOS read above, which is the path under test.
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._init_sequence_file()
        switcher._write_account_credentials("1", "a@example.com", A_CREDS)
        switcher._write_account_config("1", "a@example.com", json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com"}}))
        switcher._write_account_credentials("2", "b@example.com", B_CREDS)
        switcher._write_account_config("2", "b@example.com", json.dumps({
            "oauthAccount": {"emailAddress": "b@example.com"}}))
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "a@example.com", "organizationUuid": "org-a"},
            "2": {"email": "b@example.com", "organizationUuid": "org-b"},
        }
        switcher._write_json(switcher.sequence_file, data)

        monkeypatch.setattr(sys, "argv", ["cswap", "whoami", "--json"])
        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "disagree"
        assert payload["configEmail"] == "a@example.com"
        assert payload["credentialEmail"] == "b@example.com"
        assert payload["credentialSlot"] == "2"
