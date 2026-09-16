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
build apart from an upstream one without reading any file.

Branch `estate`. `upstream` is the original remote, so `git fetch upstream` and
a rebase are the way to take later upstream work.

## What this fork changes

Four fixes, one commit each, each with its own test file. Nothing else in the
tool is touched.

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
