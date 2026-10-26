# Third-party notices

The optional `patches/claude-swap-0.26.0-stale-quota.patch` contains context from
[claude-swap](https://github.com/realiti4/claude-swap), by Onur Cetinkol and its
contributors. The account management, quota polling, and auto-switch engine are
upstream's work. This repository supplies an optional correction for version
0.26.0 and a separate Claude Desktop launcher integration.

The experimental `team_pool` package is a separate coordinator and client. Its
OAuth endpoint conventions, native credential format, and cooperative lock
paths were informed by claude-swap's `oauth.py`, `credentials.py`, and
`claude_locks.py`. The same upstream attribution and MIT notice apply to that
reference material. Contribution authorization endpoints, PKCE parameters,
scopes, and profile metadata mappings were checked against the native Claude
Code 2.1.300 flow. These are observed interoperability details, not a supported
public integration API. The pilot does not vendor claude-swap or modify its install.

The pilot also depends on `cryptography` and, on macOS, `keyring`. Their licenses
are included in the installed distributions; see [cryptography](https://github.com/pyca/cryptography)
and [keyring](https://github.com/jaraco/keyring). No dependency credentials or
private configuration are included in this repository.

The upstream [MIT license](https://github.com/realiti4/claude-swap/blob/main/LICENSE)
is reproduced below. No Claude application code or binaries are distributed here.

```text
MIT License

Copyright (c) 2026 Onur Cetinkol

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
