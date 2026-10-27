#!/bin/bash
# One-command native pool setup. Run as the logged-in Mac user, never with sudo.
set -euo pipefail
umask 077

RELEASE=v0.1.0a8
REPOSITORY=Paradigm-Study/claude-desktop-swap
UV_VERSION=0.13.5
bootstrap_stage=''
bootstrap_lock=''

fail() { printf 'Setup stopped: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
cleanup() {
  [[ -z "$bootstrap_stage" ]] || /bin/rm -rf "$bootstrap_stage"
  if [[ -n "$bootstrap_lock" ]]; then
    /bin/rm -f "$bootstrap_lock/pid"
    /bin/rmdir "$bootstrap_lock" 2>/dev/null || true
  fi
}
fetch() {
  /usr/bin/curl --fail --silent --show-error --location --proto '=https' \
    --connect-timeout 15 --max-time 180 "$1" --output "$2"
}
safe_directory() {
  local directory="$1" mode
  if [[ -L "$directory" ]]; then fail "A setup folder is a symbolic link: $directory"; fi
  if [[ ! -e "$directory" ]]; then /bin/mkdir "$directory"; fi
  [[ -d "$directory" && -O "$directory" ]] || fail "Setup folder is not owned by this user: $directory"
  mode=$(/usr/bin/stat -f '%Lp' "$directory")
  (( (8#$mode & 0022) == 0 )) || fail "Setup folder is writable by another user: $directory"
}

setup_main() {
  local account_file="$HOME/.claude/shared-session/account.json" check=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check) check=1; shift ;;
      --account-file)
        [[ $# -ge 2 ]] || fail '--account-file needs an absolute file path.'
        account_file="$2"; shift 2 ;;
      --help|-h)
        note 'Usage: install.sh [--check] [--account-file /absolute/path/account.json]'
        note 'Sign into Paradigm with /team in Claude Share first.'
        note '--check prepares the private runtime and checks readiness without changing Claude.'
        return ;;
      *) fail "Unknown option: $1" ;;
    esac
  done
  [[ "$(/usr/bin/uname -s)" == Darwin ]] || fail 'This installer requires macOS.'
  [[ "$(/usr/bin/id -u)" != 0 ]] || fail 'Run this command as your normal Mac user, without sudo.'
  [[ -z "${CLAUDE_CONFIG_DIR:-}" ]] || fail 'Use the default Claude configuration. A CLAUDE_CONFIG_DIR override is not supported.'
  [[ "$account_file" = /* && -f "$account_file" && ! -L "$account_file" && -O "$account_file" ]] \
    || fail 'Sign into the Paradigm team using /team in Claude Share, then run this command again.'
  local account_mode
  account_mode=$(/usr/bin/stat -f '%Lp' "$account_file")
  (( (8#$account_mode & 0077) == 0 )) || fail 'The saved Share sign-in must be private to your Mac user.'
  [[ -d /Applications/Claude.app || -d "$HOME/Applications/Claude.app" ]] \
    || fail 'Install Claude Desktop and open a local Code conversation first.'

  note 'Setting up the Paradigm native subscription pool using your Claude Share sign-in.'
  note 'Your existing Desktop profile and conversations are retained.'

  # The menu app is built locally. A fresh Mac may need Apple's installer UI;
  # keep this command running so setup continues when that installer finishes.
  if ! /usr/bin/xcode-select -p >/dev/null 2>&1 || ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
    (( check == 0 )) || fail 'Xcode Command Line Tools are missing; run setup without --check to start their installer.'
    note 'Installing Apple Command Line Tools. Complete the macOS installer; this Terminal will continue automatically.'
    /usr/bin/xcode-select --install || fail 'Could not start Command Line Tools installation. Complete Xcode first-run setup, then rerun this command.'
    local elapsed=0
    until /usr/bin/xcode-select -p >/dev/null 2>&1 && /usr/bin/xcrun --find swiftc >/dev/null 2>&1; do
      (( elapsed < 1800 )) || fail 'Command Line Tools are not ready. Finish their installer, then run this command again.'
      /bin/sleep 10
      elapsed=$((elapsed + 10))
    done
  fi

  local root="$HOME/.local/share/claude-desktop-swap" directory
  for directory in "$HOME/.local" "$HOME/.local/share" "$root" "$root/releases"; do
    safe_directory "$directory"
  done
  local lock="$root/.bootstrap-lock" lock_pid
  if ! /bin/mkdir "$lock" 2>/dev/null; then
    [[ -d "$lock" && ! -L "$lock" && -O "$lock" && -f "$lock/pid" && ! -L "$lock/pid" ]] \
      || fail 'Another setup is starting. Wait a moment and rerun this command.'
    safe_directory "$lock"
    lock_pid=$(<"$lock/pid")
    [[ "$lock_pid" =~ ^[0-9]+$ ]] || fail 'The setup lock is invalid; preserve it and ask for help.'
    if kill -0 "$lock_pid" 2>/dev/null; then fail 'Another setup is already running. Finish that Terminal first.'; fi
    /bin/rm "$lock/pid"
    /bin/rmdir "$lock" || fail 'The old setup lock contains unexpected files; preserve it and ask for help.'
    /bin/mkdir "$lock" || fail 'Another setup started. Finish that Terminal first.'
  fi
  printf '%s\n' "$$" > "$lock/pid"
  local stage=''
  # Cleanup is deliberately limited to this invocation's private temporary
  # download folder and lock. Persistent runtimes may be serving live sessions.
  bootstrap_lock="$lock"
  trap cleanup EXIT
  stage=$(/usr/bin/mktemp -d "$root/.download.XXXXXX")
  bootstrap_stage="$stage"

  local release_dir="$root/releases/$RELEASE" uv_bin
  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    safe_directory "$release_dir"
    [[ -f "$release_dir/.owner" && ! -L "$release_dir/.owner" && "$(<"$release_dir/.owner")" == "$REPOSITORY@$RELEASE" ]] \
      || fail 'The release folder is not recognized; it was left unchanged.'
    if [[ ! -f "$release_dir/.ready" ]]; then
      # An interrupted download/dependency install cannot have installed a
      # service: .ready is written before invoking the setup orchestrator.
      /bin/mv "$release_dir" "$stage/incomplete-release"
    fi
  fi
  if [[ ! -d "$release_dir" ]]; then
    /bin/mkdir "$release_dir"
    printf '%s\n' "$REPOSITORY@$RELEASE" > "$release_dir/.owner"
    note "Downloading the tested $RELEASE setup package…"
    fetch "https://codeload.github.com/$REPOSITORY/tar.gz/refs/tags/$RELEASE" "$stage/source.tar.gz"
    # GitHub's archive must contain only one relative source root. Reject path
    # traversal before extracting anything into the persistent release folder.
    /usr/bin/tar -tzf "$stage/source.tar.gz" > "$stage/archive-files"
    if /usr/bin/grep -Eq '(^/|(^|/)\.\.(/|$))' "$stage/archive-files"; then fail 'The downloaded archive contains an unsafe path.'; fi
    /usr/bin/awk -F/ 'NF { if (!root) root=$1; if (!$1 || $1 != root) exit 1 } END { if (!root) exit 1 }' "$stage/archive-files" \
      || fail 'The downloaded archive must contain exactly one source root.'
    /bin/mkdir "$release_dir/source"
    /usr/bin/tar -xzf "$stage/source.tar.gz" --strip-components=1 -C "$release_dir/source"
    [[ -f "$release_dir/source/pyproject.toml" && -f "$release_dir/source/team_pool/setup.py" ]] \
      || fail 'The release package is incomplete. Run this command again after the release is available.'

    uv_bin=$(command -v uv || true)
    if [[ -z "$uv_bin" ]]; then
      safe_directory "$root/tools"
      uv_bin="$root/tools/uv"
      if [[ ! -x "$uv_bin" || -L "$uv_bin" ]]; then
        note 'Installing a private Python environment manager…'
        fetch "https://astral.sh/uv/$UV_VERSION/install.sh" "$stage/uv-install.sh"
        UV_INSTALL_DIR="$root/tools" UV_NO_MODIFY_PATH=1 /bin/sh "$stage/uv-install.sh"
      fi
    fi
    [[ -x "$uv_bin" ]] || fail 'The Python environment manager did not install successfully.'
    "$uv_bin" venv --managed-python --python 3.12 "$release_dir/venv"
    "$uv_bin" pip install --python "$release_dir/venv/bin/python" "$release_dir/source"
    "$release_dir/venv/bin/python" -c 'import team_pool.setup'
    printf '%s\n' "$RELEASE" > "$release_dir/.ready"
  fi
  [[ ! -L "$release_dir/.ready" && "$(<"$release_dir/.ready")" == "$RELEASE" ]] || fail 'The installed release marker is invalid.'
  [[ -x "$release_dir/venv/bin/python" && -d "$release_dir/source" && ! -L "$release_dir/source" ]] \
    || fail 'The installed runtime is incomplete; preserve the release folder and ask for help.'

  # These are scoped to this child process. Terminal/Codex gateway settings and
  # other provider/config-directory overrides remain untouched and are checked
  # by the native client's existing policy guard.
  unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
  cd "$release_dir"
  local arguments=(--source-dir "$release_dir/source" --account-file "$account_file")
  if (( check == 1 )); then arguments+=(--check); fi
  "$release_dir/venv/bin/python" -m team_pool.setup "${arguments[@]}"
  note "Client commands: $release_dir/venv/bin/team-pool-client status"
}

setup_main "$@"
