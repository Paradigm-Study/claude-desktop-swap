# Claude Desktop Swap

Use your native Claude subscriptions in **local Claude Desktop Code sessions**, with account rotation handled by [claude-swap](https://github.com/realiti4/claude-swap).

This companion project connects Desktop's process launcher to the native Claude Code credential store. Desktop keeps its existing login and conversation index while `cswap` manages the account used for Code requests. It also includes an optional correction for stale quota readings in `claude-swap` 0.26.0.

**Shared native pool pilot:** Paradigm members sign into Claude Share with
`/team`, then run this one-time Mac setup command:

```sh
curl -fsSL https://raw.githubusercontent.com/Paradigm-Study/claude-desktop-swap/codex/team-pool/install.sh | /bin/bash
```

No invitation token is needed. The installer handles prerequisites, enrollment,
background sync and the Claude Swap menu bar. The menu shows every account and
quota, switches this Mac to an available account, and adds accounts through
browser authorization. Run it again to upgrade or link an
existing invite enrollment. macOS may request administrator or Keychain approval.
See the [organization setup guide](docs/share-org-setup.md) for the complete flow.
Inference stays directly on native subscriptions. Teammates can contribute new
accounts from their own Macs; the coordinator owns their refresh credentials.
The [acceptance worksheet](docs/team-pilot-acceptance.md) records the first Mac's
live results; a second physical Mac remains to be verified. Local `cswap add`
does not upload an account to the shared pool.

The coordinator can also use [Cloudflare Workers and a SQLite Durable Object](docs/cloudflare-pool.md),
with its own storage and secrets in an existing Cloudflare account. Teammates use
the same Mac client and native subscription mode; no always-on Mac is required.
The team client has a separate [Claude Swap status menu bar](docs/team-status-menu.md);
keep the personal `cswap` rotator stopped while using the shared pool.

**Experimental, unofficial, and macOS only.** The integration was exercised with Desktop's bundled Claude Code 2.1.293 and 2.1.295. Its launcher configuration is version-sensitive; compatibility with future Desktop releases is not guaranteed. This project is not affiliated with Anthropic or the upstream claude-swap maintainers.

## What it does

- Installs a small launcher for one macOS user and enables it in the currently selected Desktop profile.
- Lets bundled Claude Code resolve native subscription credentials instead of the host-supplied credential.
- Leaves account storage, credential refresh, quota polling, and account selection to upstream `cswap`.
- Provides status checks and a reversible uninstall that preserves unrelated settings.

This applies to **local Code sessions**. It does not switch regular Claude chats, Cowork, or cloud sessions. It does not create quota, pool billing balances, or guarantee uninterrupted retries and subagents when an account changes.

## Install

Requirements: macOS, Python 3.10+ for this installer, a Python version supported by upstream claude-swap, Claude Desktop with its Code feature already initialized, native Claude Code signed into a subscription, and permission to administer this Mac. Use this on a machine whose Claude settings you manage. An existing different managed process wrapper is a conflict that the installer will refuse to overwrite.

### 1. Set up the account switcher

Install and configure [upstream claude-swap](https://github.com/realiti4/claude-swap). A new installation with uv can use:

```sh
uv tool install 'claude-swap[menubar]'
```

Use current upstream releases for their maintenance and security fixes. This integration was originally exercised with 0.26.0; the optional quota patch is restricted to that version and is not applied by the Desktop installer. Do not downgrade to use it. Newer upstream versions require their own compatibility check using the verification steps below.

After signing into each account in native Claude Code, run `cswap add`. Follow upstream's login instructions: do not explicitly log out first, because that can revoke the previous account's refresh token. Only add accounts you are authorized to use.

For quota-based switching, configure a threshold and run the menu bar service:

```sh
cswap config set autoswitch.threshold 90
cswap config set autoswitch.model all
cswap config set autoswitch.strategy best
cswap menubar --install-service
```

In the menu bar app, enable **Settings → Auto-switch accounts**. Installing the service alone does not enable rotation. Use `cswap list` to check the available accounts and `cswap menubar --service-status` to check the service. API-key accounts should remain excluded if you intend to use subscription accounts only.

### 2. Install the Desktop integration

Finish or stop active work, then quit Claude Desktop before changing its launch configuration. Keep your existing Desktop login; logging Desktop into a different account changes which history it shows.

```sh
git clone https://github.com/Paradigm-Study/claude-desktop-swap.git
cd claude-desktop-swap
sudo python3 scripts/desktop_swap.py install --user "$USER"
sudo python3 scripts/desktop_swap.py status --user "$USER"
```

Reopen Claude Desktop and resume a local Code conversation. Existing processes need this one-time restart to pick up the launcher. The installer does not move, copy, or delete conversation files and does not restart applications itself.

The wrapper normally leaves Desktop's Fast-mode restriction in place. To restore the native Fast control, explicitly add `--restore-fast-control` when installing. **Fast mode consumes paid usage credits outside subscription usage**, even with subscription quota remaining. See [Anthropic's Fast mode documentation](https://code.claude.com/docs/en/fast-mode). Restoring the control does not turn on Fast, buy credits, or change billing settings, but an existing saved Fast preference may become effective. Inspect it before sending work.

### 3. Verify a real session

1. Confirm the installer status reports the expected wrapper and profile flag, and the menu bar's auto-switch option is enabled.
2. Open or resume a local Code session. Check its account/authentication information where your Desktop version exposes it; it should use a native subscription rather than a gateway API key.
3. Make a manual `cswap switch 2` to another saved, usable account. Allow the native credential cache to expire (often around 30 seconds), then send a new turn in the same conversation.
4. Confirm the conversation remains available and the new turn succeeds. This is the meaningful end-to-end check; installed files or a running menu bar alone do not establish it.

## How it works

Desktop can launch its bundled Code process through the system managed `processWrapper`. This installer sets that wrapper and enables `claudeCodeProcessWrapperEnabled` in the existing selected profile when its optional configuration library is present. An initialized standard Desktop installation uses its supported user-scoped macOS preference instead. Neither path creates a replacement Desktop profile or moves conversation data.

The wrapper removes host-pinned OAuth, API-key, and gateway environment values for the selected macOS user, then executes the original command and arguments. Claude Code can consequently read the native credential store that `cswap` updates. Other macOS users pass through the wrapper unchanged. The integration also removes the host's blanket nonessential-traffic restriction, which otherwise suppresses some account capabilities.

The Desktop launcher introduces no proxy or API server. The optional team coordinator exchanges credentials; inference still goes directly to Anthropic. No application bundle or Anthropic binary is modified. The repository contains no account exports, credentials, session histories, or Anthropic source code.

See [architecture and limitations](docs/architecture.md) for the exact state boundaries and modified files.

## Optional: stale-quota correction

An observed failure in `claude-swap` 0.26.0 allowed a low cached usage reading to remain trusted while Anthropic's usage endpoint returned HTTP 429. The active subscription then exhausted its quota without triggering timely rotation.

The optional patch treats an active account's quota as unknown when that specific error persists and its last successful reading is older than five minutes. Existing unhealthy-account handling can then select a freshly verified alternate. It preserves upstream polling backoff and the displayed last-known usage. If all alternates are stale or unavailable, it holds instead of blindly rotating.

See [the patch instructions](patches/README.md). The patch is version- and checksum-guarded, opt-in, and reversible. It does not intercept inference errors, resume a stalled request, or make paid credits interchangeable with subscription quota.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Sessions seem to have disappeared | Check that Desktop is still logged into the original account/profile. This installer does not migrate its history. |
| Quota reached but no switch | Confirm auto-switch is enabled, an alternate has usable quota, and usage readings are fresh. Review the optional stale-quota patch for 0.26.0. |
| A turn still says out of credits after a switch | A retry loop may retain its previous credential. Stop the failed turn if needed and send a new turn in the same chat after the cache expires. |
| All accounts are near the threshold | Upstream uses quota, freshness, cooldown, and hysteresis to select an eligible target; 90% is not a promise to switch on every tick. If none is eligible, wait for quota to reset. |
| Fast is disabled or credits are exhausted | Fast has separate billing and availability requirements. Subscription quota rotation does not replenish usage credits. |
| Artifact or account-scoped tools fail | Permissions belong to the active native account. Rotating accounts does not transfer resource access. |
| Remote Control is unavailable | Launcher-based sessions can lose phone/web Remote Control. This integration does not restore it. |
| An app update breaks the integration | Run status, check the selected profile, and consult the compatibility notes. Restore the original setup with uninstall if needed. |

## Uninstall

Quit Claude Desktop, then run:

```sh
sudo python3 scripts/desktop_swap.py uninstall --user "$USER"
```

The uninstaller restores the settings it owns and refuses conflicting edits. Reopen Desktop afterward. The optional quota patch has its own revert command. Uninstalling this companion does not remove `cswap`, its saved accounts, or its menu bar service.

## Development

The tests use temporary directories, synthetic credentials, and local HTTPS;
they do not touch your installed Claude setup or call Anthropic. Install the
pilot's test/runtime dependencies in an isolated Python 3.12+ environment:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -v
```

The quota regression tests require `claude-swap==0.26.0` in an isolated environment; see the patch instructions. CI exercises the installer on macOS and Linux fixtures and the quota behavior against the pinned upstream package. Live Desktop authentication is a manual integration check, not covered by fixture tests.

## Credits and license

MIT © 2026 Paradigm contributors. Account management is provided by [realiti4/claude-swap](https://github.com/realiti4/claude-swap), © Onur Cetinkol, under MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Claude and Claude Code are Anthropic's trademarks.
