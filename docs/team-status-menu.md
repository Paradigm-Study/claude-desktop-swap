# Claude Swap shared-pool menu

The **Claude Swap** menu lists every shared account, its 5-hour and weekly quota
used, and a checkmark for the account installed on this Mac. Open an account's
submenu for model-specific usage, when usage was checked, and its availability.
The menu displays the coordinator's latest reported usage, not a live billing
counter. It refreshes the catalog approximately once a minute; **Refresh Pool
and Status** requests an earlier refresh.

## Switch this Mac

Open an available account's submenu and choose **Switch This Mac to This
Account**. The coordinator confirms current eligibility, then the normal native
client writes the assignment with its existing policy, ownership and recovery
checks. The menu reports success only after the local commit. Other devices are
not switched. The selected account remains assigned until automatic quota
failover is needed; the configured threshold still applies.

Unavailable accounts cannot be selected. If the pool refresh fails, prior rows
remain visible with a stale-data message and selection is disabled. Paused sync,
a manual Claude login, or an interrupted credential write requires recovery;
the menu does not overwrite those conditions. A busy background sync returns a
short retry message instead of queuing another native writer indefinitely.

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

Use the [one-command setup](share-org-setup.md) to install or update the menu,
client and persistent runtime together. It installs
`~/Applications/Claude Swap Status.app` with the LaunchAgent
`study.paradigm.claude-desktop-swap.status`. The menu's private helper configuration
points to that release's runtime; keep the release directory in place.

The original personal `cswap` menu stays disabled because it would compete for
the same native credential store. The shared menu calls the existing pool client
and coordinator rather than becoming a separate account-rotation service.

Keychain failures return instructions for foreground setup and do not repeatedly
request passwords. Read [Keychain recovery](keychain-access.md) when needed.
Long or interrupted operations retain the client's write journal or contribution
receipt; check status before retrying. **Quit Menu Bar** closes only the menu,
leaving the independent automatic sync service running.
