# Live two-Mac acceptance worksheet

Status: **IN PROGRESS — client 0.1.0a8 on the first Mac**. Offline integration
tests do not complete this worksheet. The second Mac's a7 setup attempt failed;
a successful physical rerun and enrollment remain pending.
Use only non-sensitive labels such as Account A / Mac B. Do not paste credentials,
private prompts, account emails, or session identifiers.

| Check | Mac A | Mac B | Evidence / observed limitation |
| --- | --- | --- | --- |
| macOS / Desktop / bundled Code version | 27.0.1 / 3.6556.2 / 2.1.308 | Pending | Client 0.1.0a8; Cloudflare coordinator. |
| Public one-command setup and repeat run | Passed on enrolled Mac | a7 failed; a8 rerun pending | Public a8 installer downloaded the tagged runtime; readiness check, activation, and exact piped-command rerun preserved device, token, Share identity, backup, native fingerprint, and Desktop profile. |
| Standard Desktop layout without a profile library | Existing profile preserved | Physical rerun pending | Exact a7 missing-_meta.json failure reproduced in fixtures. The a8 retry installs a supported user-scoped launcher policy without creating a profile or modifying conversation data. |
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
profile, change deployment mode, or relocate session data. Initialization is
checked before Keychain prompts, administrator approval, app shutdown, or
service interruption.

An isolated fixture reproduced the exact a7 failure and confirmed that it left
only the installer lock in the system directory. The a8 retry reused that lock,
installed and verified the native launcher, repeated without changing recovery
state, and uninstalled cleanly. Standard app configuration and conversation
fixtures remained byte-identical, and no profile library was created. The failed
launcher step precedes enrollment, so this specific failure did not create a
pool device or native recovery backup. A physical a8 rerun on Mac B and a resumed
Desktop conversation are still required to complete that acceptance check.
