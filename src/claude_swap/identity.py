"""Which account is this profile actually on — and do its two sources agree?

Fork fixes D3 and D2 (upstream #279 restated for this estate, and #217's
"cheaper mitigation").

A Claude Code profile records its account in two places, written by two
programs at different times:

- ``.claude.json`` ``oauthAccount`` — Claude Code writes it at ``/login``;
- the profile's credential — Claude Code writes it, and (from D1 on) so does
  ``cswap switch``.

Each read on its own is correct, so a disagreement between them is silent.
cswap takes the identity from the first and the credential from the second, so
while they disagree it labels one account's quota, history and switch decisions
with another account's name. Nothing answered "do these two agree?", which is
why no agent could reliably read its own account.

**The attribution is offline and it is proof, not inference.** A live
credential whose fingerprint equals slot N's stored backup IS slot N's
credential: the fingerprint is the refresh token's hash, so it survives
access-token rotation and no two accounts share one. The network probe
(``oauth.fetch_oauth_profile``) is an extra, asked for explicitly, for the case
where the live bytes match no backup cswap holds — a profile cswap has never
captured.

**Absence of a match is never agreement.** An unattributable credential is
``UNVERIFIED``. Reporting it as agreement would reinstate exactly the silent
wrong answer this reader exists to remove.

**Nothing here handles credential material beyond hashing it.** No token, no
fingerprint and no file bytes reach the returned object, so the result is safe
to print, log and paste into a ticket.

This module is a leaf: it imports the path, session and oauth helpers and two
leaf helpers from ``credentials`` (the active-store service name and the
API-key shape test), and takes the switcher as a parameter. Nothing it imports
imports it back, and it never writes anything.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from claude_swap import oauth
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    looks_like_api_key,
)
from claude_swap.paths import get_claude_config_home
from claude_swap.session import keychain_service_name


class Verdict:
    """What the two sources say about each other.

    ``AGREE``/``DISAGREE`` are proven; the rest are honest ignorance, and the
    difference matters: an agent may act on a proof and must not act on a
    guess.
    """

    AGREE = "agree"
    DISAGREE = "disagree"
    UNVERIFIED = "unverified"
    LOGGED_OUT = "logged_out"
    CREDENTIAL_MISSING = "credential_missing"
    KEYCHAIN_UNREADABLE = "keychain_unreadable"
    API_KEY = "api_key"


@dataclass(frozen=True)
class ProfileAccount:
    """One profile's account, from both of its sources, with the comparison.

    Emails and profile metadata only — see the module docstring.
    """

    config_dir: str
    config_dir_source: str
    keychain_service: str | None
    config_email: str | None
    config_org_uuid: str | None
    config_account_uuid: str | None
    credential_present: bool
    credential_slot: str | None
    credential_email: str | None
    credential_org_uuid: str | None
    resolved_email: str | None
    resolved_org_uuid: str | None
    resolved_account_uuid: str | None
    verdict: str
    detail: str

    def to_json(self) -> dict:
        """camelCase payload for ``cswap whoami --json``."""
        raw = asdict(self)
        out = {_camel(k): v for k, v in raw.items()}
        out["schemaVersion"] = 1
        return out

    @property
    def agrees(self) -> bool:
        """Proven agreement only. Every other verdict answers False, because
        an agent branching on this must not read "could not tell" as "fine"."""
        return self.verdict == Verdict.AGREE


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def config_dir_source() -> str:
    """Which rule chose this profile — the answer to "why am I here?"."""
    if os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR") is not None:
        return "CLAUDE_SECURESTORAGE_CONFIG_DIR"
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return "CLAUDE_CONFIG_DIR"
    return "default"


def active_keychain_service() -> str:
    """The Keychain item claude reads for this environment.

    Derived from the config dir, never stored: renaming a profile directory
    changes the hash and orphans its credential. Reported so the answer can be
    checked against the Keychain rather than trusted.
    """
    secure = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if secure is not None:
        return keychain_service_name(secure) if secure else CLAUDE_CODE_KEYCHAIN_SERVICE
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return CLAUDE_CODE_KEYCHAIN_SERVICE
    return keychain_service_name(config_dir)


def _slot_rows(switcher) -> list[tuple[str, str, str]]:
    """``(num, email, org_uuid)`` for every managed slot, in sequence order."""
    data = switcher._get_sequence_data_migrated() or {}
    accounts = data.get("accounts", {}) or {}
    rows = []
    for num in data.get("sequence", []) or []:
        account = accounts.get(str(num), {}) or {}
        rows.append((
            str(num),
            account.get("email", "") or "",
            account.get("organizationUuid", "") or "",
        ))
    return rows


def attribute_credential(switcher, credentials: str) -> tuple[str, str, str] | None:
    """The slot whose stored backup IS these bytes, or None.

    Compares refresh-token fingerprints, so a rotated access token still
    attributes. None means "no backup matches", which is the ordinary state of
    a profile cswap has never captured — never "belongs to nobody".
    """
    live = oauth.credential_fingerprint(credentials)
    if not live:
        return None
    for num, email, org_uuid in _slot_rows(switcher):
        # Called bare, like every other call site of this reader. It already
        # absorbs each legitimate failure internally — a missing file, an OS
        # error, corrupt base64, an unusable Keychain — and answers "" for all
        # of them. Only a programming error can reach here, and this codebase
        # lets those propagate rather than turning a bug into a silent
        # "no match", which is the one answer this function must never invent.
        stored = switcher._read_account_credentials(num, email)
        if stored and oauth.credential_fingerprint(stored) == live:
            return (num, email, org_uuid)
    return None


def read_profile_account(switcher, *, verify: bool = False) -> ProfileAccount:
    """Reconcile this profile's two account sources.

    ``verify=False`` (the default) is strictly offline. ``cswap --list --json``
    is polled every few minutes, so nothing on that path may reach the network;
    a caller that wants the extra answer asks for it.

    ``verify=True`` adds the profile probe for an otherwise unattributable
    credential only. A fingerprint match is already proof, and re-asking it
    over the network would let a probe failure downgrade a proven verdict.
    """
    config_dir = str(get_claude_config_home())
    source = config_dir_source()
    service = active_keychain_service()

    triple = switcher._get_current_identity_triple()
    config_email, config_org, config_uuid = triple if triple else (None, None, None)

    active = switcher._read_active_credentials()
    live = active.value or ""

    def _result(verdict: str, detail: str, **kw) -> ProfileAccount:
        fields = {
            "config_dir": config_dir,
            "config_dir_source": source,
            "keychain_service": service,
            "config_email": config_email or None,
            "config_org_uuid": config_org or None,
            "config_account_uuid": config_uuid or None,
            "credential_present": bool(live),
            "credential_slot": None,
            "credential_email": None,
            "credential_org_uuid": None,
            "resolved_email": None,
            "resolved_org_uuid": None,
            "resolved_account_uuid": None,
            "verdict": verdict,
            "detail": detail,
        }
        fields.update(kw)
        return ProfileAccount(**fields)

    if not live:
        if config_email:
            return _result(
                Verdict.CREDENTIAL_MISSING,
                f"{config_dir} names {config_email} but holds no credential; "
                "Claude Code will ask this profile to log in again",
            )
        return _result(Verdict.LOGGED_OUT, f"{config_dir} holds no login")

    # A degraded read may be serving a superseded generation: it can match a
    # stale backup, or none, and neither outcome is evidence about the account
    # this profile is really on. Say so rather than guess in either direction.
    if active.degraded:
        return _result(
            Verdict.KEYCHAIN_UNREADABLE,
            "the Keychain read failed and the credential served may be a "
            "superseded generation, so the two sources cannot be compared",
        )

    if looks_like_api_key(live):
        return _result(
            Verdict.API_KEY,
            "this profile is on a managed API key, which carries no account "
            "identity to compare with the config file",
        )

    match = attribute_credential(switcher, live)
    if match is not None:
        num, email, org_uuid = match
        agree = (email or "") == (config_email or "") and (
            (org_uuid or "") == (config_org or "")
        )
        detail = (
            f"{config_dir} is on {email}"
            if agree
            else (
                f"{config_dir} names {config_email or '(none)'} in its config "
                f"file, but the credential it presents is {email}'s "
                f"(cswap slot {num})"
            )
        )
        return _result(
            Verdict.AGREE if agree else Verdict.DISAGREE,
            detail,
            credential_slot=num,
            credential_email=email or None,
            credential_org_uuid=org_uuid or None,
        )

    if not verify:
        return _result(
            Verdict.UNVERIFIED,
            "the credential matches no account cswap has stored, so its owner "
            "cannot be proven offline; re-run with --verify to ask the server",
        )

    data = oauth.extract_oauth_data(live) or {}
    token = data.get("accessToken")
    resolved = oauth.fetch_oauth_profile(token) if isinstance(token, str) else None
    if not resolved:
        return _result(
            Verdict.UNVERIFIED,
            "the credential matches no stored account and the server could "
            "not resolve it (offline, expired, or a schema change)",
        )

    resolved_email = resolved.get("email")
    resolved_org = resolved.get("organizationUuid")
    resolved_uuid = resolved.get("uuid")
    # Uuid first: an email can be recycled across accounts, a uuid cannot.
    # A uuid-only response still resolves, so fall back to the email when the
    # config file has no uuid recorded to compare against.
    if config_uuid and resolved_uuid:
        agree = config_uuid == resolved_uuid
    elif resolved_email is not None:
        agree = (resolved_email or "") == (config_email or "")
    else:
        return _result(
            Verdict.UNVERIFIED,
            "the server resolved the credential but returned nothing this "
            "profile records, so there is nothing to compare",
            resolved_account_uuid=resolved_uuid,
        )
    detail = (
        f"{config_dir} is on {config_email}"
        if agree
        else (
            f"{config_dir} names {config_email or '(none)'} in its config "
            f"file, but the server resolves its credential to "
            f"{resolved_email or resolved_uuid}"
        )
    )
    return _result(
        Verdict.AGREE if agree else Verdict.DISAGREE,
        detail,
        resolved_email=resolved_email,
        resolved_org_uuid=resolved_org,
        resolved_account_uuid=resolved_uuid,
    )


def active_slot_warnings(accounts_info) -> list[str]:
    """The same proof on the list path — offline, and from rows already read.

    ``_build_accounts_info`` has already read the active slot's live credential
    and every other slot's backup, so this costs no read and no request. It
    warns only on a fingerprint match against a DIFFERENT slot: proof that the
    account cswap is about to label the active row with is not the account
    whose quota it is reporting.

    Additive, like ``duplicateAccountWarnings`` and ``lockstepUsageWarnings``
    beside it, so the ``schemaVersion: 1`` payload CodexBar reads is unchanged
    when nothing is wrong.
    """
    rows = [
        (num, email, org_uuid, is_active, creds)
        for num, email, _org_name, org_uuid, is_active, creds, _alias
        in accounts_info
    ]
    active = [r for r in rows if r[3]]
    if len(active) != 1:
        return []
    num, email, _org_uuid, _is_active, creds = active[0]
    if not creds or looks_like_api_key(creds):
        return []
    live = oauth.credential_fingerprint(creds)
    if not live:
        return []
    for other_num, other_email, _other_org, other_active, other_creds in rows:
        if other_active or not other_creds:
            continue
        if oauth.credential_fingerprint(other_creds) != live:
            continue
        return [
            f"Account-{num} ({email}) is marked active, but the credential "
            f"this profile presents is Account-{other_num}'s ({other_email}). "
            f"The usage shown against {email} is {other_email}'s. Run "
            f"`cswap whoami` for the profile's own reading, and `cswap add` "
            f"after a /login to re-attribute it."
        ]
    return []
