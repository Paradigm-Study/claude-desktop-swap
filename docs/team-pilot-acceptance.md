# Live two-Mac acceptance worksheet

Status: **IN PROGRESS — first Mac, 2026-10-26**. Offline integration tests do not
complete this worksheet. The second Mac has not enrolled yet.
Use only non-sensitive labels such as Account A / Mac B. Do not paste credentials,
private prompts, account emails, or session identifiers.

| Check | Mac A | Mac B | Evidence / observed limitation |
| --- | --- | --- | --- |
| macOS / Desktop / bundled Code version | 27.0.1 / 3.6556.2 / 2.1.308 | Pending | Client 0.1.0a4; Cloudflare coordinator. |
| One-time enrollment | Passed | Pending | One-use invite redeemed; device token stored in Keychain. |
| Browser contribution from each Mac | Passed | Pending | Two real accounts contributed from Mac A through fresh authorization. |
| Contribution preserves native login and Desktop profile | Passed | Pending | Native fingerprint unchanged by contribution; Desktop profile unchanged after activation/restart. |
| Existing provider sessions survive fresh-lineage authorization | Pending | Pending | |
| Account contributed by Mac B serves a native turn on Mac A | Pending | Pending | |
| Contribution status recovers a lost completion response | Pending | Pending | |
| Pending contribution cancellation | Passed | Pending | Live contribution start/cancel exercised before real authorization. |
| Initial native turn | Passed | Pending | Auth reports claude.ai / firstParty / max. Normal-speed Sonnet returned OK in 9.1 s; user also confirmed a successful Desktop message after restart. |
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
original fingerprint and recovery backup remained intact. Client 0.1.0a4 updates
the data in place with SecItemUpdate, and adds only when absent. The first sync
then succeeded in 50.2 s. The background LaunchAgent is active, and the personal
selector is unloaded and disabled. Native Keychain reads remain relatively slow
on Mac A; polling and provider caching are not an immediate per-request failover.

The live coordinator correctly excluded Account A at 100% weekly utilization and
assigned Account B with available quota. This establishes quota selection, not
an account switch during an existing Desktop conversation. No real provider-token
refresh, sleep/wake cycle, or two-Mac continuity result has been claimed.
