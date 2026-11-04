# Repeated Keychain approval in the native pilot

Client 0.1.0a4 had a live integration defect: every sync rewrote the native
credential through Python, including unchanged assignments. A successful write
and inference request did not establish that later Desktop requests would remain
silent. The user reported repeated approvals even after choosing Always Allow.

## Demonstrated cause

On the affected Mac, both account-switching agents were stopped. Process tracing
still observed Desktop's bundled Claude Code launching `/usr/bin/security`.
The native item's ordinary application ACL trusted both the current Python
interpreter and `/usr/bin/security`, and the latter's code requirement validated.
However, the separate partition ACL allowed only Python's current code hash.

Apple's data-update implementation creates a new secure-storage group and removes
the previous partition and integrity ACLs. The new partition belongs to the
writer. Thus `SecItemUpdate` preserved the ordinary app list but undid the
partition approval that Desktop needed on every write. A separately signed helper
would reproduce this conflict under its own identity.

Sources: [Apple's data update implementation](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/Item.cpp#L903-L985),
[the security tool's apple-tool identity](https://github.com/apple-oss-distributions/Security/blob/main/securityd/src/clientid.cpp#L260-L285),
and [partition approval](https://github.com/apple-oss-distributions/Security/blob/main/securityd/src/acls.cpp#L206-L243).

## Correction in 0.1.0a5

Only the `Claude Code-credentials` item uses Apple's `/usr/bin/security` for reads
and writes, matching Desktop. Private pool device credentials and recovery backups
remain in their own Python-owned Keychain items. Unchanged assignments update
operational status without rewriting either native credentials or `.claude.json`,
after the normal policy, drift, expiry and backup checks.

Native writes use one quoted command through stdin, never a secret in process
arguments, logs, or a temporary file. The complete command must fit 4032 bytes.
The client validates this before creating a backup or write journal; larger
MCP/plugin credential sets pause with `native_store_capacity`. No fields are
discarded to make the command fit. Writes are verified by exact readback.

Background private-item operations disable Keychain interaction. Native operations
first inspect lock state, partition permissions, and the trusted security tool
without decrypting the item. An authorization failure or timeout pauses the
client instead of retrying. The security command has no no-UI flag: a lock or ACL
change between preflight and access can still produce a prompt before the timeout
stops the attempt. This is not a promise that macOS can never request approval.

## Upgrade and recovery

Stop the old shared service before installing the new version. Do not run the
personal `cswap` selector alongside the team client.

```sh
.venv/bin/team-pool-client service-remove
git pull --ff-only
uv pip install --python .venv/bin/python .
.venv/bin/team-pool-client resume
.venv/bin/team-pool-client sync --once
.venv/bin/team-pool-client service-install
python3 scripts/install_team_status.py install
```

Use a terminal without gateway/API-key overrides. During the explicit foreground
step, macOS may request approval for `security` to access `Claude Code-credentials`.
Choose Always Allow only for that expected request. Never paste a Mac password
into chat or put it in a command argument. The client does not request or store
the Keychain password, unlock Keychain, or enable access for all applications.

The shared menu bar displays `keychain_access_required` as a paused condition.
After correcting access, use `resume`, `sync --once`, and restart the service.
If prompts recur, stop the service and inspect the actual caller and item before
making further changes. Do not restore the Python native writer from 0.1.0a4.
