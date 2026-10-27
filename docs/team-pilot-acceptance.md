# Live two-Mac acceptance worksheet

Status: **IN PROGRESS — client 0.1.0a8 active on Mac A; a9 active on Mac B**.
Mac B completed setup in its own GUI Terminal after the directory repair and
failed SSH attempts. Its native request and background sync passed, and the user
reported that it works. Existing-conversation continuity on Mac B, actual menu
clicks, and the remaining lifecycle checks below are still unverified.
Use only non-sensitive labels such as Account A / Mac B. Do not paste credentials,
private prompts, account emails, or session identifiers.

| Check | Mac A | Mac B | Evidence / observed limitation |
| --- | --- | --- | --- |
| macOS / Desktop / bundled Code version | 27.0.1 / 3.6556.2 / 2.1.308 | 27.2 / 3.6556.2 / Pending | Cloudflare coordinator; a8 active on Mac A, a9 active on Mac B. Mac B's bundled Code version remains unrecorded. |
| Public one-command setup and repeat run | Passed on enrolled Mac | Initial setup passed; repeat pending | Mac A's public a8 activation and exact piped-command rerun preserved device, token, Share identity, backup, native fingerprint, and Desktop profile. Mac B's tagged public a9 installer completed in its own GUI Terminal after the repairs below. |
| Standard Desktop layout without a profile library | Existing profile preserved | Passed | Mac B's native wrapper verified with its original standard Desktop data retained and no optional `_meta.json` created. The system and user policy directories were mode 0755, and the launcher policy was mode 0644. |
| Installer directory access and repair | Existing installation accessible | Passed | a9 diagnosed the inaccessible root-owned ClaudeCode directory before Keychain access. An authorized repair changed its mode from 0700 to 0755, confirmed on Mac B. |
| One-time enrollment | Passed | Passed | Mac A's original invite enrollment linked to live Paradigm Share membership with the same token. Mac B enrolled through Share and became active, managed, and linked, without a pending write. |
| Share membership enrollment without an invitation | Passed | Passed | Mac A's temporary-device check delivered an access-only assignment, rejected grant replay, and rejected delivery after revocation. Mac B completed real enrollment using its saved Share sign-in. |
| Membership linking preserves native login and Desktop profile | Passed | Not exercised separately | Mac A's native fingerprint, device ID, and Desktop profile were unchanged by linking. Mac B used fresh enrollment, retained its standard Desktop data, and has its original native backup. |
| Background synchronization after setup | Passed | Passed | Both sync services ran. On Mac B, the recorded sync time advanced by about 59 seconds in a subsequent cycle while the client remained active. |
| Browser contribution from each Mac | Passed | Pending | Two real accounts contributed from Mac A through fresh authorization. |
| Contribution preserves native login and Desktop profile | Passed | Pending | Native fingerprint unchanged by contribution; Desktop profile unchanged after activation/restart. |
| Existing provider sessions survive fresh-lineage authorization | Pending | Pending | |
| Account contributed by Mac B serves a native turn on Mac A | Pending | Pending | |
| Contribution status recovers a lost completion response | Pending | Pending | |
| Pending contribution cancellation | Passed | Pending | Live contribution start/cancel exercised before real authorization. |
| Full pool catalog and quota through installed menu helper | Passed | Passed | Mac A returned five accounts in 0.24 s initially and 0.39 s after rerun; three eligible and two weekly-exhausted. Mac B's installed a9 helper returned five accounts, three eligible, in 0.16 s from its GUI context. Actual menu appearance and clicks remain unverified. |
| Manual account selection through installed menu helper | Passed | Pending | Selection completed in 0.40 s, followed by a native Sonnet OK in 2.72 s. Restoring the original account completed in 0.41 s, followed by native OK in 2.19 s. Auth reported claude.ai / firstParty / max. |
| Menu contribution start and cancellation | Passed through helper | Pending | Official OAuth start and cancellation succeeded without changing native credentials. No new account was added through the menu in this check; actual GUI clicks remain unverified. |
| Initial native turn | Passed | Passed | Auth reports claude.ai / firstParty / max. Mac A's normal-speed Sonnet returned OK in 2.1 s after the a5 repair. Mac B's real normal-speed Sonnet request returned exact OK with exit 0 in 2.35 s. |
| Existing conversation retained | Passed | Pending | Mac A's user confirmed that a resumed conversation worked. The user reported Mac B working, but did not explicitly confirm a resumed existing conversation. |
| No refresh/trusted-device token delivered | Passed | Passed | Assignment and active native-store verification confirm access-only credentials. Mac B's native fingerprint matched client state, and its original recovery backup was present. |
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

Follow-up SSH inspection of Mac B, running macOS 27.2 and Desktop
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
and the a9 runtime was prepared. Its `--check` reported the specific directory
repair requirement before accessing Keychain. The first normal a9 attempt timed
out waiting for the administrator step; the system directory remained mode 0700
and no enrollment was created. A subsequent interactive SSH attempt obtained
administrator authorization with `sudo -v` before setup. The repair then
succeeded, and the system directory's mode was confirmed as 0755.

That attempt reached native credential inspection but stopped before launcher
installation or enrollment with this message: `Unlock your login Keychain and
run setup in Terminal to approve the expected access request. Readiness checks
never request approval.` This is the setup handler for
`NativeKeychainAccessError`. It does not establish which Keychain item failed or
whether the underlying cause was a locked Keychain, denied interaction, or a
native helper failure. Normal setup does not enable the noninteractive guard
used by `--check`. The SSH execution context is a hypothesis for this failure,
not a demonstrated item-specific cause.

The existing a9 setup command was then opened using `/usr/bin/open -a Terminal`
on Mac B itself. The public installer completed in that logged-in GUI context
without further code changes. The native wrapper verified, the system and user
policy directories were mode 0755, and the launcher policy was mode 0644. Its
ordinary Desktop data remained in place; no optional `_meta.json` was created.
Enrollment was active, managed, and linked to Share, with no pending native
write. The sync and menu services had live processes, and the menu helper used
the persistent a9 Python runtime.

A verification attempt over SSH still failed at native ownership inspection
with `NativeKeychainAccessError` while the GUI services remained healthy. The
same verification succeeded when run through Mac B's GUI Terminal: the native
fingerprint matched client state, its original recovery backup was present, and
the active native credentials were access-only. The installed menu helper
returned all five pool accounts, three eligible, in 0.16 s. Authentication
reported logged in through claude.ai / firstParty / max. A real normal-speed
Sonnet request returned exact `OK` with exit 0 in 2.35 s. A later background
cycle advanced the recorded sync time by about 59 seconds while remaining
active.

These results establish that GUI execution resolved this Mac's initial
authorization and verification failures; they do not identify the exact item or
underlying Keychain error in the failed SSH attempts. No credential export or
permission bypass was used. The user subsequently reported, "ok it works now".
That is confirmation of working setup, not an explicit existing-conversation
continuity test or evidence of actual menu clicks. Those checks remain pending.
