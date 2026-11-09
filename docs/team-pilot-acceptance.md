# Live two-Mac acceptance worksheet

Status: **IN PROGRESS — client 0.1.0a14 active for the SSH user on Mac B; a13 last verified on Mac A**.
Mac B's a14 upgrade preserved enrollment, native ownership, shared connector
credentials and existing conversation files. A newly created helper started in
plain SSH context authenticated and completed a native request through the GUI
execution service. The user's earlier conversation-continuity confirmation
predates a14; confirmation after this upgrade and the remaining lifecycle checks
below are still pending. Older rows retain their historical version-specific
results; see the a14 record at the end for the replacement SSH mechanism.
Use only non-sensitive labels such as Account A / Mac B. Do not paste credentials,
private prompts, account emails, or session identifiers.

| Check | Mac A | Mac B | Evidence / observed limitation |
| --- | --- | --- | --- |
| macOS / Desktop / bundled Code version | 27.0.1 / 3.6556.2 / 2.1.309 | 27.2 / 3.6556.2 / Pending | Cloudflare coordinator; a10 active on Mac A, a9 last verified on Mac B. Mac B's bundled Code version remains unrecorded. |
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
| Existing conversation retained | Passed | Passed for second user's SSH session | Mac A's user confirmed that a resumed conversation worked. The second Mac B user confirmed that their original SSH conversation responds after the a11 helper repair; future helper replacement remains unverified. |
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


## November 2: weekly-only quota response and a10 activation

Four ready accounts stopped updating because the provider began returning a null
`five_hour` window alongside a measured `seven_day` and canonical `limits[]`
weekly meters. The parser required both legacy windows to be numeric. Its
`bad-usage` backoff retained old readings, which became stale and correctly
disabled manual selection under the existing freshness rule. An account showing
100% weekly usage actually returned 11% in a fresh provider response.

An administrator-only structure diagnostic established the response shape without
returning credentials or arbitrary provider fields. Native Code 2.1.309 also
accepts nullable legacy windows. Release `v0.1.0a10` (commit `a383061`) normalizes
canonical session and weekly meters, represents an explicitly unreported session
meter as null, and still requires a measured weekly budget. Malformed or unknown
usage is not converted to zero. The coordinator recovered only prior parser
backoff once, preserving HTTP rate-limit backoff and quarantined refresh state.

The menu opts into catalog schema 2 and shows **Not reported** for absent session
meters, with explicit stale labels on old readings. Legacy menus receive a valid
schema-1 catalog with unavailable nullable rows until updated; their automatic
assignment protocol remains compatible. All four parser-error accounts obtained
fresh readings after deployment; one genuinely exhausted account stayed blocked.
Two separately quarantined refresh lineages were not changed.

The a10 tagged installer completed on Mac A, reused its existing enrollment and
verified background sync, and installed the new menu/helper. The catalog contains
eight accounts. Manual selection of the formerly stale account through the
installed helper committed in 0.55 seconds; a real normal-speed native Sonnet
request returned exact OK in 1.77 seconds with `claude.ai / firstParty / max`.
The preceding healthy assignment was restored in 0.66 seconds. Device identity,
device token, Share identity, recovery backup, profile metadata and connector
credentials were preserved; native credentials remained access-only and matched
the client state. The earlier exhausted account was not forced back into use.

Local validation completed 320 Python tests with one optional dependency skip,
91 Cloudflare tests, Swift compilation and Wrangler's deployment dry-run. The
deployed worker version is `17d93825-9191-42bd-a307-15c92e6f8522`. Direct GUI clicks
and same-conversation switching remain distinct, unverified acceptance checks;
the live selection test exercised the installed menu's helper/action path.

## November 3: shared-Mac ownership and SSH Keychain context

A Desktop SSH conversation on Mac B failed immediately with the organization's
launcher/no-credential error. The host's global launcher belonged to its earlier
macOS user, while the actual SSH user had no pool enrollment or native account
credential. Shipped Desktop 4.16336.0 client code confirmed that detecting a
remote managed launcher withholds Desktop's usual OAuth handoff before the
wrapper runs. Keeping another user's environment unchanged inside the wrapper
therefore does not make that remote user transparent to launcher detection.

A separate, non-secret Keychain probe established a second cause: the same
`security` executable and item were readable in the logged-in user's GUI audit
context but failed with exit 36 in plain SSH. The temporary probe was removed.
Matching default Keychain paths alone did not establish credential access.

Release `v0.1.0a11` (commit `13c4b33`) adds validated multi-user launcher ownership.
Second-user installation migrates legacy recovery state while preserving each
user's original policy and the machine's original settings. Removing one user
retains the others; final removal restores the machine originals. Older pool
clients can read the resulting launcher, but installer maintenance requires a11
or newer. The missing, previously recorded first user's managed flag was
restored; its paused enrollment was not resumed or rebound.

The new user's three MCP sign-ins were migrated from an MCP-only credential-file
fallback into the native Keychain, with an unchanged private recovery copy.
Their own existing Share membership authorized a new enrollment. The tagged a11
setup completed, verified background synchronization, and installed the menu.
Native ownership matched the client state, all three MCP records matched the
recovery copy, and no refresh token was delivered to the Mac.

The affected user's remote helper was restarted once in its GUI audit context,
using its existing socket and a private, consumed copy of its authentication
token. Other users' helpers were not restarted. An isolated child launched
through that actual helper and the managed launcher reported
`claude.ai / firstParty / max`. A normal-speed Sonnet request through the same
helper returned exact OK in 7.8 seconds, exit 0, without persisting a new chat.
The original conversation transcript remains present. The user confirmed that
the affected session was opened over SSH from their laptop and that the original
conversation responds after the repair. This completes the existing-conversation
recovery check; it does not establish survival across future helper replacement.

This establishes current helper-child authentication and inference. It does not
establish persistent SSH lifecycle support: Desktop can replace the helper in
an SSH context during an upgrade or cold start. No competing KeepAlive loop was
installed. The GUI-context restart remains a bounded mitigation, with that
lifecycle gap open. Validation completed 332 Python tests with one optional
dependency skip; 53 installer tests and an independent ownership review passed.

## November 3: targeted account recovery and refresh diagnostics

Three older migrated accounts were quarantined after uncertain credential
renewals. Their quota readings became stale because quarantined entries are
excluded from polling. The old catch discarded the underlying exception, so
those historical failures cannot be attributed to provider rejection, transport,
successor validation, encryption, or persistence from the stored evidence.
Ordinary contribution also rejected every existing account as a duplicate after
exchanging its authorization code; it could not recover quarantined entries.

Release `v0.1.0a12` (commit `4ad7dc6`) adds explicit targeted browser recovery.
The coordinator captures the selected account and generation before sign-in,
requires matching account and organization identities plus fresh credentials,
and commits replacement and the `recovered` receipt together. The account ID is
retained; old quota and backoff are cleared. Both coordinators implement the
contract, and ordinary additions keep their previous duplicate behavior.
The menu offers **Reconnect Account…** for quarantined entries and retains the
target through pending, cancelled, failed, expired, and uncertain receipts.

The Cloudflare coordinator records an admin-only allowlisted stage, reason, and
timestamp for future refresh failures, without raw errors or provider responses.
Historical failures remain unknown. Transport failures, timeout, and malformed
JSON still share the transport/response category; these diagnostics do not
justify replaying a potentially consumed refresh credential.

Validation completed 357 Python tests with one optional dependency skip, 111
Cloudflare tests, Swift compilation, Wrangler dry-run, and an independent review
of replacement, membership, rollback, and diagnostic privacy. Deployment
`cf5051a5-f979-41b6-a4dd-f694d33c5635` reports a12 in native subscription mode.
Before deployment, the last near-expiry healthy account completed its normal
refresh; no refresh was in progress and every ready token had over 100 minutes
remaining. The deployed migration preserved five ready and three quarantined
accounts without fabricating historical failure reasons.

The tagged a12 installer completed on Mac A with verified background sync and
the updated menu. Device and Share identities, native recovery backup, connector
credentials, and native ownership all matched the pre-update checks; no refresh
token was delivered to the Mac. The installed menu helper successfully started
recovery for the selected quarantined account and returned a matching pending
receipt. The user completed all three fresh authorizations in the local hidden
Terminal prompt. Each returned `recovered`, retained its original pool ID, and
advanced its generation once (18 to 19, 23 to 24, and 18 to 19). All three are
ready with cleared errors and fresh quota; initial weekly usage was 91%, 85%,
and 0%. The installed menu helper subsequently showed all three eligible, with
the third account's measured usage advancing to 2%. Six of the eight accounts
are eligible; two remain unavailable at the configured 98% quota threshold.

Post-recovery checks again confirmed the same device and Share identities,
unchanged recovery backup and connector credentials, active synchronization,
matching native ownership, and no native refresh token. At that check Mac A
was assigned to the third recovered account. Recovery repairs shared pool
credentials; it does not establish future renewal success or resolve the
separate SSH helper lifecycle limitation.

## November 8: pool loading during background sync

Repeated reports of an unloaded pool outlasted the earlier one-time resyncs.
At inspection the coordinator and installed helper returned all ten accounts;
nine had fresh quota and one separately required account recovery after HTTP 401.
The exact transient failure reported by the user was no longer present and had
not been retained in the old logs. It cannot be attributed retrospectively.

A reproducible local defect was established: the menu's two-second mutation-lock
timeout could fail catalog loading while background synchronization held that
lock across network requests. Release `v0.1.0a13` removes the read path's lock
dependency. It reads atomically published state, checks enrollment and token
identity around the request, and displays the latest committed assignment.
Revocation writes still acquire the mutation lock. A paused leave/re-enroll
regression verifies that a replacement token cannot be sent to the old origin.

Quota freshness now follows its actual timestamp and coordinator reason instead
of labeling every cached row stale after any catalog failure. The failure banner
and disabled selection controls remain. Fixed-category diagnostics retain menu
loading events and background request failures/recoveries; they exclude tokens,
authorization codes, messages, URLs, response bodies, and account details.
This improves evidence for any remaining network or coordinator failure rather
than claiming all historical failures had the same cause.

Validation: 380 tests completed successfully with one optional dependency skip,
full Swift compilation, shell syntax checks, and independent read-only review.
The tagged installer completed on Mac A. Its actual running menu recorded
`loaded`; the installed helper returned all ten accounts in 0.182 seconds while
the mutation lock was deliberately held. A normal-speed native Sonnet request
returned exact OK in 3.81 seconds, and background sync remained active.

Device/token and Share identities, recovery backup, matching native ownership,
and access-only credentials passed post-install checks. The selected account
and shared-credential hash differed from the baseline captured earlier in the
work; their cause was not established, so unchanged connector values and account
selection are not claimed. Four MCP records were present afterward, and no
rollback of those live credentials was attempted. Visible menu confirmation is
pending the user. The coordinator was not redeployed, Claude Desktop was not
restarted, and Mac B's logged-out GUI service/SSH lifecycle gap remains open.

## a14: persistent launcher and SSH execution lifecycle

The November 8, 2026 incident had three demonstrated local causes. The enrolled
macOS user had logged out, stopping its GUI sync service until the assignment
expired. A generated managed-preference cache no longer contained the launcher's
flag. A connector-only fallback file had also appeared. After the user signed
in, a bounded native repair restored sync; restarting the existing SSH helper in
GUI audit context restored its Keychain access. The user confirmed that the
original remote conversation responded. That helper restart was a temporary
repair, not a persistent lifecycle solution.

Client a14 moves the installer-owned flag to Desktop's persistent local
configuration library, with journaled migration and restoration. An owned,
redundant MCP-only fallback can be archived intact after verifying its
credentials against Keychain. Changed or new connector credentials, ownership
drift and conflicting policy still stop recovery. Remote bundled Code processes
now execute through a per-user GUI LaunchAgent; the helper itself remains owned
by Desktop. The relay carries process I/O and control, never provider credentials.
See [the SSH contract](ssh-native-mode.md) for the exact login requirement.

The release candidate completed the ordinary setup orchestrator in Mac B's GUI
context in **7.85 seconds**, including migration, foreground and background sync,
GUI execution service installation and menu installation. The legacy policy
file was removed; another user's already-missing legacy cache remained absent
and did not block the migration. Native credential fingerprint, recovery backup,
device identity and token, Share identity, all three shared connector records,
Desktop data-directory identity and conversation-file paths were unchanged.
The resulting native assignment was active and access-only, with no credential
fallback file present. Repeating setup completed in **4.98 seconds** and reused
the same healthy GUI execution service process.

A separate disposable Desktop helper was started from **plain SSH**, without
moving that helper into GUI context. A child launched through the installed
wrapper reported `claude.ai / firstParty / max` in **0.35 seconds**. Another
fresh SSH helper completed a normal-speed Sonnet request with exact `OK`, exit
zero and one turn in **7.72 seconds**. Only those disposable helpers and their
probe processes were removed afterward. `doctor --ssh` reported a ready GUI
bridge and correctly skipped requiring Keychain access from the SSH caller.

Local validation completed **505 Python tests**, with one optional dependency
skip, plus installer shell syntax and whitespace checks. Independent reviews
covered credential preservation, migration ownership, recovery interruption,
relay identity checks and service lifecycle. Regression tests include missing
other-user generated caches, journals above 1 MiB, rejection before oversized
writes, MCP fallback races, signal/disconnect cleanup, active-job refusal and a
lost service-drain reply. These tests do not establish future provider renewal,
all Desktop updates, logout/login, sleep/wake, menu clicks or quota switching
within a running conversation. A GUI login is still required; logging out ends
its services and processes. Coordinator code was unchanged by a14.

After publication, the public branch installer matched the tested script and
its exact `curl ... | /bin/bash` invocation completed on Mac B. Desktop helper
processes continued using the original `Claude` user-data directory, with no
`Claude-3p` user-data override. A fresh child of the existing SSH helper also
reported native Max authentication in 0.46 seconds. Temporary verification jobs
were removed; the permanent sync and GUI execution services remained active.
The [release CI run](https://github.com/Paradigm-Study/claude-desktop-swap/actions/runs/42573240344)
passed on macOS and Linux, both with and without the optional upstream package,
and passed the unchanged Cloudflare coordinator checks. A test-only follow-up
bounds the relay environment fixture's wait and handles failure/EOF explicitly;
it does not change the released runtime.
