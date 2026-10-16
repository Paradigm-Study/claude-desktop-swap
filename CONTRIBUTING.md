# Contributing

Keep changes small and explain the state or behavior they correct. Run the fixture tests before submitting a pull request and distinguish offline test results from live Desktop verification.

Do not commit account exports, tokens, Keychain contents, session transcripts, personal profile identifiers, or logs containing them. Use synthetic accounts and temporary directories in tests. Do not include Anthropic binaries or decompiled source.

The Desktop installer and optional quota patch have separate lifecycles. Preserve that separation: installing Desktop support must not silently patch an upstream dependency, enable paid features, or change a user's billing preferences.

Before changing the quota patch, reproduce the failing decision with a fake clock and explicit usage states. Respect upstream polling backoff and require a fresh usable destination. Prefer an upstream fix when it becomes available; do not carry the patch across unverified package versions.
