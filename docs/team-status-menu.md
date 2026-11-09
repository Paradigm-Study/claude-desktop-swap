# Claude Swap shared-pool menu

The **Claude Swap** menu lists every shared account, its 5-hour and weekly quota
used, and a checkmark for the account installed on this Mac. Open an account's
submenu for model-specific usage, when usage was checked, and its availability.
The menu displays the coordinator's latest reported usage, not a live billing
counter. It refreshes the catalog approximately once a minute; **Refresh Pool
and Status** requests an earlier refresh.

Some subscriptions currently report weekly quota without a five-hour meter.
The menu shows **Not reported** for that meter, rather than inventing 0%.
Availability follows the measured weekly and model-specific quotas. Old readings
are explicitly labeled **stale** when a fresh provider response is unavailable.
Version a10 understands this response; rerun the one-command setup to update an
older menu. Automatic background assignment remains compatible with older clients.

## Switch this Mac

Open an available account's submenu and choose **Switch This Mac to This
Account**. The coordinator confirms current eligibility, then the normal native
client writes the assignment with its existing policy, ownership and recovery
checks. The menu reports success only after the local commit. Other devices are
not switched. The selected account remains assigned until automatic quota
failover is needed; the configured threshold still applies.

Unavailable accounts cannot be selected. If the pool refresh fails, prior rows
remain visible with a refresh-failed message and selection is disabled. Their
quota is labeled stale only when its timestamp is older than five minutes,
invalid, or explicitly stale on the coordinator. Paused sync,
a manual Claude login, or an interrupted credential write requires recovery;
the menu does not overwrite those conditions. A busy background sync returns a
short retry message instead of queuing another native writer indefinitely.
With a13, reading the catalog does not wait for that writer's lock. The reader
checks enrollment identity before and after the request and uses the latest
committed account for its checkmark.

With a15, **Waiting for Keychain** means the client confirmed a locked Keychain
and its background service is still running. Unlock your login Keychain; the
service checks again without requesting interaction and restores sync only after
the normal ownership, backup and policy checks pass. Manual switching is blocked
during that wait. A pending write needing review or a stopped service still shows
**Automatic switching paused**. Unknown access errors and approval failures also
remain paused; a successful pool refresh does not resume them.

Native inference continues directly to Anthropic. Switching does not restart
Desktop, replace its profile, retry a failed request, or change credentials
already cached by an in-flight turn or subagent. Resume the conversation normally
after the switch. Fast mode keeps its separate paid-credit requirements.

## Add an account

Choose **Add Account to Shared Pool…**, confirm that you intend to share that
subscription, and sign in on the official Claude authorization page. Back in the
menu, choose **Enter Authorization Code…** and paste the complete code, including
`#state`, into the hidden field. No Terminal command is needed.

The coordinator receives a fresh authorization and owns the resulting refresh
credentials. This does not change the Mac's current native login. Authorization
codes go to the helper through stdin, never process arguments, temporary files
or application logs. The menu does not receive provider access/refresh tokens or
an administrator token.

Pending additions offer **Check Account Addition** and **Cancel Account Addition**.
If a completion response was lost, check status before continuing; the code is
never replayed automatically. An uncertain result offers a fresh authorization
flow with a new code. A completed account appears on the next successful pool
refresh. Existing personal `cswap` accounts are not uploaded automatically.

## Install and recover

With a12 or newer, a quarantined account's submenu offers **Reconnect Account…**.
Confirm the account, then sign into that exact Claude account on the authorization
page. Use **Enter Authorization Code…** to submit its complete code in the hidden
field. Recovery renews that account for the shared pool and preserves its pool
entry; it does not switch the current Mac or replace its native recovery backup.
The coordinator verifies both the account and organization before replacement,
requires a fresh authorization, and refuses to overwrite a healthy account or
an entry that changed while sign-in was open.

A successful recovery clears obsolete quota readings. The next provider check
establishes current availability. Signing into a different account fails recovery
without adding it to the pool or replacing the selected account. A cancelled or
expired login can be started again. If a response was lost, check status before
starting another sign-in; authorization codes are never replayed automatically.
The ordinary **Add Account to Shared Pool…** flow still treats existing accounts
as duplicates. Use the selected account's reconnect action for recovery.

Use the [one-command setup](share-org-setup.md) to install or update the menu,
client and persistent runtime together. It installs
`~/Applications/Claude Swap Status.app` with the LaunchAgent
`study.paradigm.claude-desktop-swap.status`. The menu's private helper configuration
points to that release's runtime; keep the release directory in place.

The original personal `cswap` menu stays disabled because it would compete for
the same native credential store. The shared menu calls the existing pool client
and coordinator rather than becoming a separate account-rotation service.

For `keychain_access_required`, or an old pause left after Keychain became
readable, rerun ordinary [one-command setup](share-org-setup.md) in the enrolled
user's desktop Terminal. A15 can repair an owned access-required or locked pause
after verifying the pool's credential fingerprint and recovery backup. Check-only
setup cannot resume it. Unknown errors and command timeouts do not trigger
automatic access retries. A concurrent lock or permission change can still
produce a macOS approval prompt; see [Keychain recovery](keychain-access.md).
Long or interrupted operations retain the client's write journal or contribution
receipt; check status before retrying. **Quit Menu Bar** closes only the menu,
leaving the independent automatic sync service running.

## Loading diagnostics

Client a13 records timestamped background failure and recovery events in
`~/.config/claude-desktop-swap/agent.log`. Request diagnostics use fixed categories
such as timeout, DNS, TLS, connection, or HTTP error, plus numeric status and
duration where available. Only recognized coordinator error codes are retained.

The menu keeps its latest 64 loading events in the private
`~/.config/claude-desktop-swap/menu-diagnostics.json` file. Initial loading,
recovery, and failures are recorded as timestamps and fixed categories. Neither
diagnostic stream records authorization codes, tokens, headers, request bodies,
provider responses, account addresses, or conversation content.

These records distinguish a failed catalog request from stale quota or a stopped
background service. A successful catalog load alone does not establish that an
existing Claude conversation has received the latest credential.
