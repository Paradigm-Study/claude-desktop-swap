# Live two-Mac acceptance worksheet

Status: **IN PROGRESS — client 0.1.0a8 active on Mac A; a9 prepared on Mac B**.
Offline integration tests do not complete this worksheet. Mac B's a7 and a8
attempts failed. The a9 diagnostic is verified there; administrator repair,
enrollment, and a successful Desktop conversation remain pending.
Use only non-sensitive labels such as Account A / Mac B. Do not paste credentials,
private prompts, account emails, or session identifiers.

| Check | Mac A | Mac B | Evidence / observed limitation |
| --- | --- | --- | --- |
| macOS / Desktop / bundled Code version | 27.0.1 / 3.6556.2 / 2.1.308 | 27.2 / 3.6556.2 / Pending | Cloudflare coordinator; a8 active on Mac A, a9 runtime prepared on Mac B. |
| Public one-command setup and repeat run | Passed on enrolled Mac | a7 and a8 failed; a9 repair/enrollment pending | Mac A's public a8 activation and exact piped-command rerun preserved device, token, Share identity, backup, native fingerprint, and Desktop profile. Mac B's tagged a9 script and runtime are verified, but full setup has not completed. |
| Standard Desktop layout without a profile library | Existing profile preserved | a8 blocked by directory permissions | The supported user-scoped launcher policy passed fixtures without changing conversation data. The real a8 retry exposed an additional access failure left by a7; fixture success did not establish successful fresh-Mac setup. |
| Installer directory access and repair | Existing installation accessible | a9 diagnostic passed; repair pending | Mac B's root-owned ClaudeCode directory is mode 0700. a9 `--check` reports the repair needed before accessing Keychain; the administrator repair has not yet run. |
| One-time enrollment | Passed | Pending | Original invite enrollment linked to live Paradigm Share membership; same device token retained. |
| Share membership enrollment without an invitation | Passed | Pending | Saved Share sign-in enrolled a temporary device, delivered an access-only assignment, rejected grant replay, and rejected delivery after test-device revocation. |
| Membership linking preserves native login and Desktop profile | Passed | Pending | Native fingerprint, device ID, and original Desktop profile unchanged by linking; background sync active. |
| Browser contribution from each Mac | Passed | Pending | Two real accounts contributed from Mac A through fresh authorization. |
| Contribution preserves native login and Desktop profile | Passed | Pending | Native fingerprint unchanged by contribution; Desktop profile unchanged after activation/restart. |
| Existing provider sessions survive fresh-lineage authorization | Pending | Pending | |
| Account contributed by Mac B serves a native turn on Mac A | Pending | Pending | |
| Contribution status recovers a lost completion response | Pending | Pending | |
| Pending contribution cancellation | Passed | Pending | Live contribution start/cancel exercised before real authorization. |
| Full pool catalog and quota through installed menu helper | Passed | Pending | Five accounts returned in 0.24 s initially and 0.39 s after rerun; three eligible and two weekly-exhausted. Actual menu appearance and clicks remain unverified. |
| Manual account selection through installed menu helper | Passed | Pending | Selection completed in 0.40 s, followed by a native Sonnet OK in 2.72 s. Restoring the original account completed in 0.41 s, followed by native OK in 2.19 s. Auth reported claude.ai / firstParty / max. |
| Menu contribution start and cancellation | Passed through helper | Pending | Official OAuth start and cancellation succeeded without changing native credentials. No new account was added through the menu in this check; actual GUI clicks remain unverified. |
| Initial native turn | Passed | Pending | Auth reports claude.ai / firstParty / max. Normal-speed Sonnet returned OK in 2.1 s after the a5 repair; user previously confirmed a successful Desktop message after restart. |
| Existing conversation retained | Passed | Pending | User confirmed existing conversations remained available and a resumed conversation worked. |
| No refresh/trusted-device token delivered | Passed | Pending | Assignment and active native-store verification confirm access-only credentials; recovery backup retained separately. |
| Contributed-account refresh occurs only on coordinator | Pending | Pending | |
| Real provider-token refresh followed by successful turn | Pending | Pending | |
| Quota-based account switch in same conversation | Pending | Pending | |
| Cache delay / in-flight turn / subagent behavior | Pending | Pending | |
| Sleep/wake recovery | Pending | Pending | |
| Coordinator outage and token expiry | Pending | Pending | |
| Manual login pauses rather than being overwritten | Pending | Pending | |
| Device revocation blocks future delivery | Passed on test device | Pending | Revoked test device received HTTP 401 on subsequent delivery. |
| Actual residual validity of delivered token | Pending | Pending | |
| Account-scoped tool and policy compatibility | Pending | Pending | |
| Leave and explicit restore | Pending | Pending | |

Decision after testing: pending. A token-store write or HTTP 200 alone is not a
successful Desktop result. Stop if a full credential export is required on clients,
an incompatible policy would need bypassing, or a user's manual login is overwritten.

Observed during activation: the previous Keyring setter attempted to delete and
recreate an existing native item; macOS rejected it with OSStatus -25244. Its
original fingerprint and recovery backup remained intact. Client 0.1.0a4's
SecItemUpdate correction allowed initial activation in 50.2 s, but failed the
sustained user experience: repeated approval prompts returned. Those timings
must not be treated as evidence of intrinsically slow Keychain reads.

The [Keychain investigation](keychain-access.md) established that each Python
data write reset the partition permission needed by Desktop's security tool.
The native item was repaired through Apple's security tool with identical
credential contents; write and read each took about 0.02 s, and the bundled Code
authentication check completed in 0.14 s. Client 0.1.0a5 was installed and its
LaunchAgent was running. A live guarded sync completed in 0.77 s with zero native
writes for an unchanged assignment. A subsequent authentication check completed
in 0.13 s and a normal-speed native turn returned OK in 2.1 s. The user then confirmed that no new Keychain prompts appeared after this
update. This confirms the observed prompt loop stopped; longer-term sleep/wake
and token-refresh behavior remain pending. The separate shared status menu is
installed and running; the personal selector remains unloaded and disabled.

The live coordinator correctly excluded Account A at 100% weekly utilization and
assigned Account B with available quota. This establishes quota selection, not
an account switch during an existing Desktop conversation. No real provider-token
refresh, sleep/wake cycle, or two-Mac continuity result has been claimed.

Organization enrollment was deployed and verified on October 26, 2026. The
existing directory continued serving its previous code for about six minutes
after the public Worker reported the new version; live enrollment passed after
propagation without resetting data or changing routes. Client 0.1.0a6 linked
Mac A through its existing Share sign-in. A subsequent native authentication
check reported claude.ai / firstParty / max in 0.13 s, and a normal-speed Sonnet
turn returned OK in 2.14 s. This is a bundled Code check; the user's earlier
Desktop conversation-continuity confirmation remains the UI evidence.

All five saved accounts are now identity- and usage-verified in the shared pool.
Three had quota at verification; two were weekly-exhausted. Four old Claude
proxy registrations and the three transferred personal refresh lineages were
retired before coordinator ownership. Codex's proxy stayed running. Retired
credential snapshots are private recovery evidence, not active refresh owners.

The public one-command installer for `v0.1.0a7` was verified on Mac A on October
26, 2026. The downloaded script matched the released source, prepared a private
Python runtime, passed a read-only readiness check, and completed activation.
Running the exact public `curl ... | /bin/bash` command again reused that runtime
and enrollment. Both runs preserved the device ID and token, recovery backup,
native credential fingerprint, Share identity, and original Desktop profile.
The background sync and menu bar had live processes, and a subsequent background
cycle remained active. Native authentication
reported claude.ai / firstParty / max in 0.14 s; a normal-speed Sonnet request
returned OK in 2.41 s. This verifies upgrade and rerun on the existing Mac, not
the Command Line Tools/admin/Keychain prompts on a second fresh Mac.

Client `v0.1.0a8` was released from commit
[`cafccdc`](https://github.com/Paradigm-Study/claude-desktop-swap/commit/cafccdc).
All six CI checks passed across the
[push run](https://github.com/Paradigm-Study/claude-desktop-swap/actions/runs/40425545863)
and [PR run](https://github.com/Paradigm-Study/claude-desktop-swap/actions/runs/40425550152).
Validation included 291 Python tests with one optional skip and 78 Node tests.
The deployed Cloudflare Worker version is
`a7836179-e374-477f-8519-01fe3d1a80d6`.

On Mac A, the public a8 script downloaded its release, passed readiness checking,
completed activation, and passed an exact `curl ... | /bin/bash` rerun. The a8
menu and sync service had live processes, and the menu helper was configured to
use the persistent a8 virtual environment. Device ID, device token, Share
identity, original Desktop profile, recovery backup, and native credential
fingerprint were preserved by the upgrade and rerun.
A later background cycle remained active on the restored original account.

The installed menu bridge returned all five account summaries and quota in
0.24 s initially and 0.39 s after the rerun. Three accounts were eligible and two
were weekly-exhausted. A manual selection through that bridge completed in
0.40 s; native authentication reported claude.ai / firstParty / max, and a
normal-speed Sonnet request returned OK in 2.72 s. Restoring the original account
took 0.41 s, followed by another native OK in 2.19 s. These are live checks of the
installed menu's operation path and native inference. They do not establish
actual GUI interaction, visual presentation, or a quota-triggered switch within
an existing Desktop conversation. Those checks remain open.

Official browser authorization start and cancellation were also exercised
through the installed menu bridge without changing native credentials. This
did not add a new account; the earlier two real contributions remain the live
evidence for completed contribution. No new claim is made about contribution
from a second Mac.

Mac B reported an a7 failure after administrator approval: `Missing _meta.json`.
The installer incorrectly required the optional profile library present on Mac
A. Inspection of the installed Desktop established that ordinary `config.json`
stores app state, while Desktop reads the launcher flag from its profile library
or macOS managed preferences. The a8 fix uses the supported user-scoped managed
preference on an initialized standard installation. It does not manufacture a
profile, change deployment mode, or relocate session data. The a8 change moved
initialization checks before Keychain prompts, administrator approval, app
shutdown, or service interruption, but did not account for inaccessible system
directories left by the failed a7 attempt.

An isolated fixture reproduced the exact a7 failure and confirmed that it left
only the installer lock in the system directory. The a8 retry reused that lock,
installed and verified the native launcher, repeated without changing recovery
state, and uninstalled cleanly. Standard app configuration and conversation
fixtures remained byte-identical, and no profile library was created. The failed
launcher step precedes enrollment, so this specific failure did not create a
pool device or native recovery backup. This fixture ran with ordinary directory
permissions. It missed the bootstrap's inherited restrictive umask, and its
successful retry did not predict the real Mac B outcome.

Follow-up SSH inspection of Mac B, a Mac Studio running macOS 27.2 and Desktop
3.6556.2, found the a8 runtime prepared but no pool enrollment or running sync/menu
services. Its standard Desktop `config.json` was mode 0600 and no optional
`_meta.json` existed. `/Library/Application Support/ClaudeCode` was owned by
`root:admin` with mode 0700. Native preflight reached the `managed-settings.d`
existence check in `team_pool/native.py` line 291, where `pathlib.exists()` raised
`PermissionError` before setup could reach the privileged installer. The
resulting generic setup/Keychain error obscured the filesystem cause.

The bootstrap sets `umask 077`, which the privileged installer inherited.
Creating a directory with requested mode 0755 therefore produced mode 0700.
The installer checked ownership and rejected unsafe write permissions, but did
not require read/traverse access for the client. The failed a7 install left this
inaccessible directory and its private installer lock; the a8 standard-layout
correction could not be reached on retry. The same mechanism could also affect
newly created managed-preference directories.

Client `v0.1.0a9` was released from commit
[`8ef5cee`](https://github.com/Paradigm-Study/claude-desktop-swap/commit/8ef5cee2eee4138f24bebbbfb65a4db58e297055).
All six CI checks passed across the
[push run](https://github.com/Paradigm-Study/claude-desktop-swap/actions/runs/40428664582)
and [PR run](https://github.com/Paradigm-Study/claude-desktop-swap/actions/runs/40428670924).
Validation included 311 Python tests with one optional skip. The installer now
explicitly sets mode 0755 on newly created public directories after validating
their ownership. Its narrow `repair-access` operation validates existing
installer-owned contents and acquires the installer lock before correcting
directory access; unknown contents or unsafe ownership are refused. Private
lock and recovery files retain their permissions. Setup detects this condition
before native inspection and performs the repair only through an administrator
step after Share membership verification.

On the physical Mac B, the tagged a9 script was downloaded and its SHA verified,
and the a9 runtime was prepared. Its `--check` now reports the specific directory
repair requirement before accessing Keychain. This verifies the corrected
diagnostic only. The human administrator command is staged; privileged repair,
full setup, enrollment, running services, and a resumed Desktop conversation
remain pending.
