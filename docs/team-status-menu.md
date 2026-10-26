# Claude Swap shared-pool menu bar

The shared pool uses its own background service to update the native Claude Code
sign-in. The original `cswap` menu bar also rotates accounts, so it must remain
stopped while the shared service owns that sign-in.

This optional **Claude Swap** menu bar reads local status and checks whether the
shared background service is running. It shows the last successful sync, access
expiry, and any paused or degraded state. It never reads Keychain, calls the
coordinator, changes credentials, or starts or stops account rotation. Account
names appear only when the client has included a safe label in its status file.

On macOS, with Xcode Command Line Tools installed, run from this repository:

```sh
python3 scripts/install_team_status.py install
python3 scripts/install_team_status.py status
```

The installer compiles an AppKit app into `~/Applications/Claude Swap Status.app`
and starts it with the separate LaunchAgent
`study.paradigm.claude-desktop-swap.status`. It appears on the menu bar now and at
login. The installer refuses to replace an app or LaunchAgent it does not
recognize. Running `install` again updates this app.

The menu refreshes every 15 seconds and when opened. **Automatic switching
paused** means the shared rotation service is stopped or the client reported a
condition that needs attention. Check `team-pool-client status` and follow the
[shared pool setup guide](cloudflare-pool.md) to resolve the client condition.
The menu does not attempt repairs or ask for a Keychain password.

**Quit Menu Bar** closes only the menu bar for the current login session. It does
not stop automatic account switching. To remove the menu bar and its login item:

```sh
python3 scripts/install_team_status.py uninstall
```

This leaves the shared pool client, its native sign-in, and Claude Desktop alone.
