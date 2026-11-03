# cswap — Agent-Harness-Reconfig fork

Fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap), taken
at upstream `7187ce8` (in-tree `0.27.0b1`, unreleased; latest PyPI release
0.26.0, 2 September 2026).

Authorising ticket: `pearson-tfl/Agent-Harness-Reconfig` **#1381**, cutover
blocker 1 for #632. John's ruling, 5 September 2026: fork the tool and fix its
four defects for this estate rather than working around them, and do **not**
offer the fixes upstream yet — run them for some weeks first, so we do not
create a review dependency on a stranger.

Version marker: `0.27.0b1+estate.1`. `cswap --version` therefore tells a fork
build apart from an upstream one without reading any file. **SUGGESTED — not
one of the four defects.** It is one line, and it serves John's ruling that
this fork runs for some weeks before anything is offered upstream: without it,
nothing on the machine distinguishes a fork build from an upstream one. Drop it
if it is not wanted.

Branch `estate`. `upstream` is the original remote, so `git fetch upstream` and
a rebase are the way to take later upstream work.

## What this fork changes

Fixes D1–D6, one commit each. D1–D4 are the original four, each with its own
test file; D5 and D6 were found later on this estate. Nothing else in the tool
is touched.

### D1 — upstream #206: the credential write followed the wrong profile

Upstream resolves the Keychain item claude reads for the current environment on
the **read** and **capture** sides (upstream #190, #205). The **write** side
stayed on the hardcoded, unsuffixed `Claude Code-credentials`, which names the
default profile only. Under `CLAUDE_CONFIG_DIR` that split every switch across
two profiles: the target account's credential landed in a profile the caller is
not in, while the profile claude actually reads kept its old account. The switch
reported success and changed nothing where it mattered. The stale-item delete
had the same split, so it logged the *default* profile out instead.

Both now go through `_active_oauth_keychain_services()`, the function the active
read already uses, so read and write cannot name different profiles. The delete
attempts every resolved item even after one fails and returns the conjunction:
an unattempted item is a residual that can still shadow the plaintext file.

Writing claude's hashed Keychain item is new for cswap, and the `session` module
cautions against authoring that format. The caution does not reach this change:
the format is round-tripped, not authored — the active read already parses the
same item under the same derived name, so a naming or format drift would break
reads first and is not introduced by writing.

Files: `src/claude_swap/credentials.py`, `tests/test_fork_write_side_profile.py`.

### D2 + D3 — the two sources of a profile's identity can disagree

This is the fix the estate needs most, and it is **not** what #1381's defect
list originally recorded for D3. See "What this fork deliberately does not do"
below.

A profile records its account twice:

- `.claude.json` `oauthAccount` — Claude Code writes it at `/login`;
- the credential the profile presents — Claude Code writes it, and from D1 on
  so does `cswap switch`.

Each read on its own is correct, so a disagreement between them is silent. cswap
takes the identity from the first and the credential from the second, so while
they differ it labels one account's quota, history and switch decisions with
another account's name. Nothing answered "do these two agree?", which is why no
agent could reliably read its own account.

New leaf module `claude_swap/identity.py`:

- `attribute_credential` compares refresh-token fingerprints against the backups
  cswap already holds. A match is **proof**, not inference: the fingerprint
  survives access-token rotation and no two accounts share one.
- `read_profile_account` reconciles the two sources and returns a verdict.
  Absence of a match is `unverified`, never agreement — calling it agreement
  would reinstate the silent wrong answer the module exists to remove. A
  degraded Keychain read, a managed API key, a missing credential and a
  logged-out profile each get their own verdict rather than being collapsed.
- The default is strictly offline. The network probe resolves only a credential
  no backup matches, and only when asked.
- Nothing beyond a hash touches credential material, so the result is safe in a
  log, a ticket or a pane.

New command:

    cswap whoami [--json] [--verify]

Exit 1 on a **proven** disagreement, so a script can branch without parsing
prose. Every other verdict is honest ignorance and exits 0 — a caller must never
read "could not tell" as "the profile is wrong".

For D2 (upstream #217) the same offline proof is applied on the list path, from
rows `_build_accounts_info` has already read, so the path CodexBar polls gains
no request and no read. It surfaces as `profileIdentityWarnings`, additive
beside the existing `duplicateAccountWarnings` and `lockstepUsageWarnings`.

**CodexBar contract.** `cswap --list --json` still answers `schemaVersion: 1`
and the payload is byte-identical when nothing is wrong. `profileIdentityWarnings`
appears only when a disagreement is proven, and is the same shape as the two
warning arrays already in the payload. Recorded on #1381.

D2's original mechanism — the active slot polled with the default profile's
credential — is already fixed at upstream `7187ce8`: `_build_accounts_info`
reads the active credential through `_read_active_credentials`, which resolves
per profile. What remained was the identity comparison above, which is the
"cheaper mitigation" upstream #217 itself proposes.

Files: `src/claude_swap/identity.py`, `src/claude_swap/cli.py`,
`src/claude_swap/switcher.py`, `tests/test_fork_profile_identity.py`.

### D4 — upstream #253: autoswitch history was global, not per profile

Two `cswap auto` engines under different `CLAUDE_CONFIG_DIR` profiles share one
`autoswitch_state.json`. The six keys a switch writes describe what one live
config did, but they sat at the top level, so each engine read the other's
history as its own: a foreign switch started a cooldown that suppressed this
one, the no-return bar fired on another profile's landing, and a foreign switch
rewrote `lastSwitchTo`, disarming the own-landing check while replacing the
departure snapshot the release predicate diffs against.

The six keys move under `perConfigDir[<normalised config home>]`, read and
written through one view helper. No decision logic changes. Quarantine is a fact
about an **account**, so it stays top-level and shared. Keys collapse through
`realpath`, so a trailing separator or a symlink spelling of one profile shares
one slice.

No schema bump: the read path honours a pre-upgrade record's top-level keys
until the first post-upgrade switch retires them, and an older cswap reading the
new file lands in its existing absence-releases paths.

Files: `src/claude_swap/autoswitch.py`,
`tests/test_fork_autoswitch_per_config_dir.py`, plus a scoped read helper in
`tests/test_autoswitch.py` for assertions about a persisted switch.

### D5 — a dead stored copy was reported as a dead account

Found on this estate, 16 September 2026, and not in the original four. `cswap
--list --json` reported slot 4 (`bbconsult@jeptech.co.uk`) as
`usageStatus: relogin_required` with both usage figures null, while the account
was live, correctly attributed (`cswap whoami --verify` → `agree`) and the
LEAST used of the four. Agents read the list, believed the account with the
most headroom was unusable, and steered work onto nearly-exhausted accounts.

cswap quarantines a slot after `invalid_grant` and reported every quarantine as
"re-login needed". The credential that failed, though, is whichever copy cswap
POSTed. For an **idle** slot that is cswap's own backup — and cswap has no view
of the credential that account's profile may actually hold. A profile re-logged
outside cswap leaves exactly this state: the backup holds a spent generation,
the account is untouched.

So "I cannot measure this account" was being reported as "the user is locked
out". Three states now, not two:

| what cswap proved | state |
|---|---|
| the credential in use is dead (active slot) | `relogin_required` |
| only cswap's stored copy is dead | `stored_credential_stale` (new) |
| struck, but the second source cannot be read | `keychain_unavailable` |

**The three-way split is only fully reachable for the ACTIVE slot, and this
matters — it is the limit of the fix.** The active slot has two stored sources,
so cswap performs a real second read and can tell a dead credential-in-use from
a merely stale backup. An **idle** slot has one: cswap's own backup. Nothing in
the collect path carries that account's live credential, so `relogin_required`
is unreachable for an idle slot, and a genuinely expired idle account and a
healthy idle account with a stale backup both report `stored_credential_stale`.

So for an idle slot the new state means **"cswap cannot tell"**, not #1381's
words "unattributable-but-valid". cswap proves only that its own copy died; it
is silent on the account. That is honest where the old state was not — it stops
asserting a death cswap never established, which was the harm — but it is a
weaker claim than proof of liveness, and this fork does not pretend otherwise.

Proving liveness for an idle slot needs evidence cswap does not currently
gather: a read of that profile's own Keychain item, or the network probe
`whoami --verify` already performs. Both are real costs — a per-profile Keychain
read on a path CodexBar polls every few minutes, or a network call on a path
that is deliberately offline — so which, if either, is worth it is a decision
for #1381, not a gap to be quietly closed here.

`_entry_token_dead` keeps its exact boolean meaning and is now defined as
"`_token_dead_reason` named a cause", so the quarantine decision and the
displayed state cannot drift apart. **No fetch behaviour changes**: a
quarantined slot is still not polled in any of the three states, which is the
property the existing tests guard and they still guard it.

`AUTH_DEAD_STRIKES` is 1, so a single `invalid_grant` reaches this display.
That threshold is untouched here — it is upstream's and a separate argument.

Three existing assertions in `tests/test_switcher.py` expected
`relogin_required` on an **idle** slot with a struck backup. That is the defect
itself, so they now expect `stored_credential_stale`; every other assertion in
those tests, including the `fetch.assert_not_called()` quarantine checks they
exist for, is unchanged. One was strengthened: the quarantine-lift test
asserted only "not re-login", which the new state would have satisfied
vacuously, and now asserts the strike survives under neither state.

**CodexBar contract.** `schemaVersion` stays 1 and `usageStatus` gains one
value, which is additive rather than a redefinition: a consumer switching on the
old set meets an unknown string instead of a confidently wrong one.

Two separate claims, which an earlier draft of this file ran together. cswap's
OWN displays are provably safe — `SENTINEL_NOTES` lookups fall back to the raw
value (`menubar.py`, `tui/data.py`, `switcher.py`), so no display path here can
raise. That says nothing about CodexBar, which is a separate application not in
this repository. What was measured of it: CodexBar 0.60.3's binary contains
`relogin_required`, `token_expired`, `keychain_unavailable`, `foreign_credential`,
`api_key` and `unavailable`, and does not contain `stored_credential_stale`. How
it handles an unrecognised value was not established and cannot be from here.

Files: `src/claude_swap/json_output.py`, `src/claude_swap/switcher.py`,
`tests/test_fork_stored_credential_stale.py`, `tests/test_switcher.py`.

### D6 — `CLAUDE_CONFIG_DIR=~/.claude` switched off the same-account guard

Found on this estate, 3 November 2026 (`pearson-tfl/Agent-Harness-Reconfig`
#5645). `cswap run N` refuses to give the account that is the active default
login a second credential copy, because seeding one spends the refresh token
the default profile holds. Any set `CLAUDE_CONFIG_DIR` skipped that guard, and
the estate's launch lines set it to `~/.claude` itself. On the Dell, with the
default login on slot 2, `CLAUDE_CONFIG_DIR=/Users/johnp/.claude cswap run 2`
bootstrapped a session profile at 18:33Z, and the default login died at 18:44Z
("OAuth session expired and could not be refreshed"). Now a preset that
resolves to the default profile keeps the guard, which still identifies the
default login from that profile's `.claude.json`; a preset naming any other
folder still skips it.

Files: `src/claude_swap/session.py`, `tests/test_session.py`.

## What this fork deliberately does not do

**It does not port upstream PR #280, the Keychain trusted-app ACL pin.**

#1381 records D3 as upstream #279: "`security add-generic-password -U` without
`-T` resets the item's ACL to writer-only on every rotation, so Claude Code is
locked out and reports a false expiry." That premise is wrong, and the proposed
fix is harmful in exactly the context this estate runs.

Measured on this Mac, 16 September 2026, on a disposable keychain file (never
the login keychain, never a real credential):

| step | result |
|---|---|
| create an item with `-T /usr/bin/security -T <claude>` | rc 0, ACL records 2 trusted applications |
| update it **without** `-T` (cswap's current write shape) | rc 0 in 0.01 s, value changed, **ACL unchanged** |
| update it **repeating** `-T` (what PR #280 does) | **blocked on a GUI authorisation dialog**, killed at 120 s |

macOS treats a repeated `-T` on an existing item as an ACL change and requests
`change_acl` authorisation. A headless or launchd context can never answer that
prompt, and cswap's `security` timeout is 5 seconds, so every rotation would
fall back to plaintext-file mode.

This agrees with upstream's own evidence. Upstream #324 reported both findings
and the maintainer closed PR #280 on them, adding that Claude Code's macOS
binary reads credentials through `security` as well, so the extra trust entry is
not needed even on creation.

The estate's real D3 problem — the one #1381's `/implement` brief states — is
the `.claude.json`-versus-Keychain disagreement, fixed above.

## Running the tests

    cd ~/Code/cswap
    uv run pytest -q

2312 passed, 4 skipped at the head of `estate`. The suite is offline and never
touches the login keychain.

## Installing the fork

**Not done by this ticket.** #1381 forbids touching `~/.local/bin/cswap` or the
installed upstream package, which CodexBar currently points at. The install is
the PM's or John's call:

    uv tool install --force --from ~/Code/cswap claude-swap

That replaces `~/.local/bin/cswap` and `~/.local/bin/claude-swap` in place, so
CodexBar's "claude-swap executable" field needs no change. `cswap --version`
then reads `0.27.0b1+estate.1`. Reverting is `uv tool install --force
claude-swap`.

## Taking later upstream work

    git fetch upstream
    git rebase upstream/main        # or merge, on the `estate` branch

The four fixes are separate commits with separate test files, so an upstream
change that lands one of them upstream can be dropped from this branch on its
own.
