/** Direct OAuth and usage transport. This module never proxies inference.
 *
 * Refresh conventions follow claude-swap's oauth.py (MIT, Copyright (c) 2026
 * Onur Cetinkol); see THIRD_PARTY_NOTICES.md. Endpoints, scopes, and metadata
 * mappings were checked against native Claude Code 2.1.300. This is an observed
 * interoperability contract, not a supported public API.
 */

export const AUTHORIZE_URL = 'https://claude.com/cai/oauth/authorize';
export const MANUAL_REDIRECT_URL = 'https://platform.claude.com/oauth/code/callback';
export const TOKEN_URL = 'https://platform.claude.com/v1/oauth/token';
export const USAGE_URL = 'https://api.anthropic.com/api/oauth/usage';
export const PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile';
export const CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
export const MAX_RESPONSE = 1_048_576;
export const REQUEST_TIMEOUT_MS = 10_000;
export const LOGIN_SCOPES = Object.freeze([
  'org:create_api_key', 'user:profile', 'user:inference',
  'user:sessions:claude_code', 'user:mcp_servers', 'user:file_upload', 'user:plugins',
]);
const SUBSCRIPTION_TYPES = new Map([
  ['claude_pro', 'pro'], ['claude_max', 'max'],
  ['claude_team', 'team'], ['claude_enterprise', 'enterprise'],
]);

export class ProviderError extends Error {
  constructor(code, retry_after = 0) {
    super(code);
    this.name = 'ProviderError';
    this.code = code;
    this.retry_after = retry_after;
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function validToken(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= 16_384
    && !/[^\x21-\x7e]/.test(value);
}

function metadataString(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= 512
    && value === value.trim() && !/[\x00-\x1f\x7f]/.test(value);
}

function oauthInput(verifier, state, code) {
  if (typeof verifier !== 'string' || verifier.length < 43 || verifier.length > 128
    || /[^A-Za-z0-9._~-]/.test(verifier)
    || typeof state !== 'string' || state.length < 32 || state.length > 128
    || /[^A-Za-z0-9_-]/.test(state)
    || (code !== undefined && (typeof code !== 'string' || code.length < 1
      || code.length > 8192 || /[^\x21-\x7e]/.test(code) || code.includes('#')))) {
    throw new ProviderError('invalid-oauth-input');
  }
}

function profileIdentity(data) {
  if (!isRecord(data.account) || !isRecord(data.organization)
    || !metadataString(data.account.uuid) || !metadataString(data.organization.uuid)) {
    throw new ProviderError('bad-profile');
  }
  const result = {
    accountUuid: data.account.uuid,
    organizationUuid: data.organization.uuid,
  };
  const email = data.account.email || data.account.email_address;
  if (email !== undefined && email !== null && email !== '') {
    if (!metadataString(email)) throw new ProviderError('bad-profile');
    result.emailAddress = email;
  }
  return result;
}

function grantMaterial(data, now, errorCode) {
  const expiry = data.expires_in;
  const scope = data.scope;
  const scopes = typeof scope === 'string' ? scope.trim().split(/\s+/u) : [];
  if (!validToken(data.access_token) || !validToken(data.refresh_token)
    || typeof expiry !== 'number' || !Number.isFinite(expiry) || expiry <= 60
    || typeof now !== 'number' || !Number.isFinite(now + expiry)
    || typeof scope !== 'string' || scope.length > 16_384
    || !scopes.includes('user:inference') || !scopes.includes('user:profile')
    || scopes.some(part => !part.length || part.length > 256 || /[^\x21-\x7e]/.test(part))
    || (data.token_type !== undefined
      && (typeof data.token_type !== 'string' || data.token_type.toLowerCase() !== 'bearer'))) {
    throw new ProviderError(errorCode);
  }
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: now + expiry,
    scopes,
  };
}

function retryAfter(headers) {
  const value = headers.get('Retry-After');
  // The observed protocol uses delta seconds, not a date. Invalid values must
  // not turn into an unbounded or immediate retry loop.
  if (value === null || value.trim() === '') return 0;
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) return 0;
  const seconds = Number(value);
  return Number.isFinite(seconds) ? Math.min(86400, Math.max(0, seconds)) : 0;
}

export class Provider {
  constructor({ clock = () => Date.now() / 1000,
    fetcher = (...args) => globalThis.fetch(...args) } = {}) {
    this.clock = clock;
    this.fetcher = fetcher;
  }

  async authorizationUrl(verifier, state) {
    oauthInput(verifier, state);
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    const challenge = btoa(String.fromCharCode(...new Uint8Array(digest)))
      .replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
    const url = new URL(AUTHORIZE_URL);
    url.search = new URLSearchParams({
      code: 'true', client_id: CLIENT_ID, response_type: 'code',
      redirect_uri: MANUAL_REDIRECT_URL, scope: LOGIN_SCOPES.join(' '),
      code_challenge: challenge, code_challenge_method: 'S256', state,
    }).toString();
    return url.toString();
  }

  async exchangeCode(code, verifier, state) {
    // The caller persists its exchanging marker before invoking this method.
    // No request is retried: even a failed response may have consumed the code.
    if (code === undefined) throw new ProviderError('invalid-oauth-input');
    oauthInput(verifier, state, code);
    const data = await this.request(TOKEN_URL, { body: {
      grant_type: 'authorization_code', code, redirect_uri: MANUAL_REDIRECT_URL,
      client_id: CLIENT_ID, code_verifier: verifier, state,
    } });
    const material = grantMaterial(data, this.clock(), 'incomplete-grant');
    const profile = await this.request(PROFILE_URL, { token: material.access_token });
    const account = profileIdentity(profile);
    const subscription = SUBSCRIPTION_TYPES.get(profile.organization.organization_type);
    const tier = profile.organization.rate_limit_tier;
    if (!subscription || !metadataString(tier)) {
      throw new ProviderError('unverified-subscription');
    }
    if (!metadataString(account.emailAddress)) throw new ProviderError('bad-profile');
    for (const [source, target] of [['account', 'accountUuid'], ['organization', 'organizationUuid']]) {
      const supplied = data[source];
      if (supplied !== undefined && supplied !== null
        && (!isRecord(supplied) || supplied.uuid !== account[target])) {
        throw new ProviderError('grant-profile-mismatch');
      }
    }
    return { ...material, account: {
      ...account, subscriptionType: subscription, rateLimitTier: tier,
    } };
  }

  async request(url, { token, body } = {}) {
    // Keep all destinations fixed even if a future caller accidentally passes a
    // URL derived from an upstream response. Redirects never receive credentials.
    if (![TOKEN_URL, PROFILE_URL, USAGE_URL].includes(url)) {
      throw new ProviderError('invalid-provider-endpoint');
    }
    if (token !== undefined && !validToken(token)) throw new ProviderError('invalid-access-token');
    const headers = { Accept: 'application/json', 'User-Agent': 'claude-desktop-swap-pilot/1' };
    if (token !== undefined) headers.Authorization = `Bearer ${token}`;
    if (url === USAGE_URL) headers['anthropic-beta'] = 'oauth-2025-04-20';
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let reader;
    try {
      const response = await this.fetcher(url, {
        method: body === undefined ? 'GET' : 'POST', headers,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        redirect: 'manual', signal: controller.signal,
      });
      if (!Number.isInteger(response.status) || response.status < 100 || response.status > 599) {
        throw new ProviderError('bad-response');
      }
      if (response.status < 200 || response.status >= 300) {
        // Never include the provider's error body or URL in diagnostics.
        if (response.body) await response.body.cancel();
        throw new ProviderError(`http-${response.status}`, retryAfter(response.headers));
      }
      // A conforming fetcher honors redirect:manual. Fail closed if an injected
      // transport returned a response it had already followed.
      if (response.redirected) {
        if (response.body) await response.body.cancel();
        throw new ProviderError('bad-response');
      }
      const contentLength = response.headers.get('Content-Length');
      if (contentLength !== null && Number(contentLength) > MAX_RESPONSE) {
        if (response.body) await response.body.cancel();
        throw new ProviderError('bad-response');
      }
      if (!response.body) throw new ProviderError('bad-response');
      reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (!(value instanceof Uint8Array)) throw new ProviderError('bad-response');
        size += value.byteLength;
        if (size > MAX_RESPONSE) {
          await reader.cancel();
          throw new ProviderError('bad-response');
        }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
      }
      const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      if (!isRecord(value)) throw new ProviderError('bad-response');
      return value;
    } catch (error) {
      if (error instanceof ProviderError) throw error;
      throw new ProviderError('transport-or-response-error');
    } finally {
      clearTimeout(timer);
      if (reader) reader.releaseLock();
    }
  }

  async identity(accessToken) {
    return profileIdentity(await this.request(PROFILE_URL, { token: accessToken }));
  }

  async usage(accessToken) {
    const raw = await this.request(USAGE_URL, { token: accessToken });
    const result = {};
    const keys = new Set(['five_hour', 'seven_day', ...Object.keys(raw).filter(k => k.startsWith('seven_day_'))]);
    for (const key of keys) {
      const value = raw[key];
      if ((value === null || value === undefined) && key !== 'five_hour' && key !== 'seven_day') continue;
      const pct = isRecord(value) ? value.utilization : undefined;
      if (typeof pct !== 'number' || !Number.isFinite(pct) || pct < 0) {
        throw new ProviderError('bad-usage');
      }
      result[key] = pct;
    }
    return result;
  }

  async refresh(credentials) {
    if (!isRecord(credentials) || !validToken(credentials.refresh_token)) {
      throw new ProviderError('incomplete-refresh');
    }
    const data = await this.request(TOKEN_URL, { body: {
      grant_type: 'refresh_token', refresh_token: credentials.refresh_token, client_id: CLIENT_ID,
    } });
    // A missing/malformed successor is ambiguous for rotating tokens. The
    // coordinator quarantines this lineage instead of replaying its predecessor.
    return { ...credentials, ...grantMaterial(data, this.clock(), 'incomplete-refresh') };
  }
}
