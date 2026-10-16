# Optional stale-quota correction

This patch is only for **claude-swap 0.26.0** and the exact source hashes recorded
in `scripts/cswap_quota_patch.py`. It is not required for the Desktop launcher.
Do not downgrade a newer installation to apply it; newer upstream versions need
their own review.

In the tested version, a rate-limited usage endpoint can leave a low cached
reading trusted while inference consumes the remaining quota. The patch treats
an active account's last good reading as unknown once it is over 300 seconds old
and its latest quota error is `http-429`. Upstream's existing unhealthy-account
policy then permits failover after three ticks by default, but only to a freshly
measured eligible peer. A candidate must still be fresh after credential
preparation. Fresh recovery cancels the stale-data failover.

It preserves display data, the quota API's existing backoff, and the upstream
polling machinery. It does not directly add API calls or retry inference. Existing
failover can nominate a peer refresh under the usual scheduling and backoff rules.
If no fresh eligible peer exists, it does not rotate blindly. Existing inference
retries may still use their earlier account until a new turn begins.

Run this from the repository using the Python interpreter that owns your `cswap`
installation. For the usual `uv tool install` layout on macOS:

```sh
CSWAP_PY="$HOME/.local/share/uv/tools/claude-swap/bin/python"
"$CSWAP_PY" scripts/cswap_quota_patch.py status
```

Stop any foreground `cswap auto` process and quit the menu bar app before applying
or reverting. Do not upgrade cswap at the same time. If the menu bar LaunchAgent
is installed, unload it temporarily so it cannot relaunch during the operation:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.cswap.menubar.plist"
"$CSWAP_PY" scripts/cswap_quota_patch.py apply
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.cswap.menubar.plist"
```

Only use the LaunchAgent commands if that plist exists. If you do not use the
service, relaunch `cswap auto` or the menu bar app normally afterward. An upgrade
may replace this optional patch. Check status again, but do not force it onto an
unsupported version.

To undo, stop the same processes, run `revert`, then restart them:

```sh
"$CSWAP_PY" scripts/cswap_quota_patch.py revert
```

The utility keeps a verified original backup next to `autoswitch.py`, refuses
unknown source/backup hashes, preserves file permissions and ownership, and
atomically replaces only that source file. It never installs packages, reads
credentials, or calls a quota endpoint. A patched file without this utility's
verified backup can be recognized by `status`, but cannot be reverted by it.

For offline regression checks, use a separate environment:

```sh
uv venv --python 3.14 /tmp/claude-desktop-swap-tests
uv pip install --python /tmp/claude-desktop-swap-tests/bin/python 'claude-swap==0.26.0'
/tmp/claude-desktop-swap-tests/bin/python -m unittest discover -s tests -v
```

Tests apply the diff to a temporary copy with fake accounts and a fake clock;
they do not modify installed cswap, inspect credentials, or make inference calls.
