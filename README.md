# codex-swap

Multi-account switcher for the [OpenAI Codex CLI](https://github.com/openai/codex).
Switch between several ChatGPT accounts without logging out, see every
account's rate-limit usage in one dashboard, run accounts in parallel
terminals, and let it rotate accounts automatically before a limit hits.

Inspired by [claude-swap](https://github.com/realiti4/claude-swap), which does
the same for Claude Code.

```
$ cxswap list
→ 1. work@example.com  [work]  pro
     5h: 62% (resets 1h12m), 1w: 31% (resets 4d2h)
  2. personal@example.com  [home]  pro
     5h: 4% (resets 3h40m), 1w: 12% (resets 6d11h)
```

## Why it's safe to use

Codex keeps its login in a plain file, `~/.codex/auth.json`. Naive switchers
that just copy this file around eventually kill accounts, because:

- **OpenAI rotates refresh tokens.** Restoring a stale copy after codex
  refreshed the token gets you `refresh token was already used` and a dead
  session.
- **`codex login` revokes the token it replaces.** Logging into a second
  account on top of a live `auth.json` best-effort revokes the previous
  session server-side (see `codex-rs/login/src/server.rs` in openai/codex).

cxswap is built around those two facts:

- Identity is matched by the stable ChatGPT `account_id`, and before every
  switch the live tokens (including session-profile copies) are synced back
  into the store, so a rotated refresh token is never clobbered.
- `cxswap add --login` moves `auth.json` aside before running `codex login`,
  so there is nothing to revoke — both sessions stay alive.
- An account whose session was revoked anyway (logout, overwriting login) is
  detected and quarantined with a `needs relogin` marker instead of being
  rotated onto.
- All writes are atomic (temp file + rename, 0600), invocations are
  serialized with a file lock, and the store keeps a `.bak` copy.

## Install

```bash
uv tool install git+https://github.com/PavloPopravkin/codex-swap
# or
pipx install git+https://github.com/PavloPopravkin/codex-swap
```

Requires Python ≥ 3.9 and the codex CLI. No runtime dependencies (stdlib
only). macOS and Linux (POSIX only).

## Usage

```bash
# capture the currently logged-in account:
cxswap add --alias work

# add ANOTHER account safely (do NOT run a plain `codex login` on top of a
# live auth.json — that revokes the previous session):
cxswap add --login --alias home

cxswap list                     # dashboard: accounts + live usage + active marker
cxswap status                   # who am I right now

cxswap switch                   # rotate to next enabled account
cxswap switch 2                 # jump by slot number
cxswap switch home              # by alias (or email, or unique email prefix)
cxswap switch --strategy best   # most quota left

cxswap disable 2                # hold out of rotation (still an explicit target)
cxswap enable 2
cxswap remove 2
```

Running codex sessions keep the account they started with; a switch applies
to newly started codex processes (codex reads `auth.json` at startup).

### Live usage

`list`, `status`, and `auto` fetch fresh rate-limit windows for every stored
account from the ChatGPT backend (`wham/usage` — the same undocumented
endpoint the popular community switchers use). Access tokens are refreshed
automatically when needed and rotated refresh tokens are persisted
immediately. Codex's local session rollout files are the offline fallback;
`--offline` skips the network entirely.

### Automatic switching

```bash
cxswap auto                     # foreground loop, poll every 60s, switch at 90%
cxswap auto --threshold 80
cxswap auto --once              # single check for cron (exit 0 switched / 2 nothing / 3 blocked)
cxswap auto --dry-run --json

cxswap config                   # show settings
cxswap config --set autoswitch.threshold=80
```

A cooldown (default 5 min) prevents flip-flopping near the threshold.

### Parallel accounts (session mode)

```bash
cxswap run 2                    # launch codex as account 2 in THIS terminal only
cxswap run home -- exec "task"  # everything after -- goes to codex
```

`run` builds a per-account profile in `~/.codex-swap/profiles/` that becomes
`CODEX_HOME` for that process. Everything from the real `~/.codex`
(config.toml, prompts, sessions, history, caches) is symlinked in, so
behaviour and history stay shared — only `auth.json` is a real per-account
file. Fresher tokens (whichever copy refreshed last) always win on sync.
Other terminals and the default login are untouched.

### Headless servers

To log an account in on a machine with no browser, tunnel the OAuth callback
port from your desktop:

```bash
ssh -N -L 1455:localhost:1455 you@server   # keep running
# on the server:
cxswap add --login --alias work
# open the printed URL in your LOCAL browser (use a clean incognito window
# and sign in with the account you actually want — the browser session
# decides which account gets captured)
```

`codex login --device-auth` also works, but only after enabling device code
authorization in the ChatGPT account's Security Settings.

Tip: give each machine its own login (its own session) instead of copying
tokens between machines — two hosts sharing one session race each other on
refresh-token rotation and eventually one of them dies.

### Moving between machines

```bash
cxswap export accounts.json     # contains secrets, written 0600 — treat as a password
cxswap import accounts.json
```

(See the tip above — prefer fresh logins per machine when you can.)

## How it works

- Accounts live in `~/.codex-swap/store.json` (0600, dir 0700), including a
  full copy of each account's `auth.json` payload and the last-known usage
  snapshot.
- Email and plan are decoded from the `id_token` JWT; identity is the stable
  `account_id`.
- Switching atomically rewrites `~/.codex/auth.json` after syncing back the
  freshest tokens (`last_refresh` decides).
- Usage comes from `GET https://chatgpt.com/backend-api/wham/usage`
  (Bearer access token + `chatgpt-account-id` header), falling back to the
  newest `rate_limits` event in `~/.codex/sessions/**/rollout-*.jsonl`.

## Caveats

- Unofficial tool relying on codex's on-disk format and an undocumented
  usage endpoint; either may change without notice.
- Whether multiple ChatGPT accounts are within OpenAI's terms for your plan
  is on you.
- POSIX only (uses `flock` and `exec`); Windows is untested and unsupported.

## License

MIT
