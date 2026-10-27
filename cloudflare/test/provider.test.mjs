import test from 'node:test';
import assert from 'node:assert/strict';
import { Provider, ProviderError, AUTHORIZE_URL, MANUAL_REDIRECT_URL, TOKEN_URL,
  PROFILE_URL, USAGE_URL, CLIENT_ID, LOGIN_SCOPES, MAX_RESPONSE,
  REQUEST_TIMEOUT_MS } from '../src/provider.mjs';

const VERIFIER = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk';
const STATE = 's'.repeat(43);
const grant = updates => ({ access_token: 'new-access', refresh_token: 'new-refresh',
  expires_in: 3600, token_type: 'Bearer',
  scope: 'user:profile user:inference user:sessions:claude_code', ...updates });
const profile = updates => ({ account: { uuid: 'account-a', email: 'a@example.test' },
  organization: { uuid: 'org-a', organization_type: 'claude_max',
    rate_limit_tier: 'default_claude_max_20x', ...updates } });
const credentials = () => ({ access_token: 'old-access', refresh_token: 'old-refresh',
  scopes: ['old'], expires_at: 1, account: { accountUuid: 'account-a' } });

function fake(values, clock = () => 500) {
  const requests = [];
  const provider = new Provider({ clock, fetcher: async (url, init) => {
    requests.push({ url, ...init });
    assert.ok(values.length, 'unexpected extra provider request');
    const value = values.shift();
    if (value instanceof Error) throw value;
    return value instanceof Response ? value : new Response(JSON.stringify(value));
  } });
  return { provider, requests };
}

async function fails(promise, code, retry = 0) {
  await assert.rejects(promise, error => {
    assert.ok(error instanceof ProviderError);
    assert.equal(error.message, code);
    assert.equal(error.code, code);
    assert.equal(error.retry_after, retry);
    assert.equal(error.cause, undefined);
    return true;
  });
}

test('default transport retains the global fetch receiver required by workerd', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', function (url, init) {
    assert.equal(this, globalThis);
    assert.equal(url, PROFILE_URL);
    assert.equal(init.headers.Authorization, 'Bearer access');
    calls += 1;
    return Promise.resolve(new Response(JSON.stringify(profile())));
  });
  assert.equal((await new Provider().identity('access')).accountUuid, 'account-a');
  assert.equal(calls, 1);
});

test('authorization URL uses native manual callback and the standard PKCE S256 vector', async () => {
  const { provider, requests } = fake([]);
  const url = new URL(await provider.authorizationUrl(VERIFIER, STATE));
  assert.equal(url.origin + url.pathname, AUTHORIZE_URL);
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    code: 'true', client_id: CLIENT_ID, response_type: 'code',
    redirect_uri: MANUAL_REDIRECT_URL, scope: LOGIN_SCOPES.join(' '),
    code_challenge: 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
    code_challenge_method: 'S256', state: STATE,
  });
  assert.ok(!url.toString().includes(VERIFIER));
  assert.equal(requests.length, 0);
});

test('invalid OAuth inputs fail before any network call', async () => {
  for (const args of [
    ['code', 'short', STATE], ['code', VERIFIER, 'short'],
    ['code', 'v'.repeat(129), STATE], ['code', VERIFIER, 's'.repeat(129)],
    ['code', 'é'.repeat(43), STATE], ['code', VERIFIER, 's/'.repeat(22)],
    ['code#state', VERIFIER, STATE], ['', VERIFIER, STATE],
    [undefined, VERIFIER, STATE], [null, VERIFIER, STATE],
    ['code\n', VERIFIER, STATE], ['code\0', VERIFIER, STATE],
    ['é', VERIFIER, STATE], ['c'.repeat(8193), VERIFIER, STATE],
  ]) {
    const { provider, requests } = fake([]);
    await fails(provider.exchangeCode(...args), 'invalid-oauth-input');
    assert.equal(requests.length, 0);
  }
  for (const [verifier, state] of [[VERIFIER + '\n', STATE], [VERIFIER, STATE + '\n']]) {
    const { provider } = fake([]);
    await fails(provider.authorizationUrl(verifier, state), 'invalid-oauth-input');
  }
});

test('exchange preserves actual subscription, tier, identity, and returned scopes', async () => {
  for (const [organization_type, subscriptionType] of [
    ['claude_pro', 'pro'], ['claude_max', 'max'], ['claude_team', 'team'],
    ['claude_enterprise', 'enterprise'],
  ]) {
    const { provider, requests } = fake([grant(), profile({ organization_type })]);
    const result = await provider.exchangeCode('one-time-code', VERIFIER, STATE);
    assert.deepEqual(result, {
      access_token: 'new-access', refresh_token: 'new-refresh', expires_at: 4100,
      scopes: ['user:profile', 'user:inference', 'user:sessions:claude_code'],
      account: { accountUuid: 'account-a', organizationUuid: 'org-a',
        emailAddress: 'a@example.test', subscriptionType, rateLimitTier: 'default_claude_max_20x' },
    });
    assert.equal(requests.length, 2);
    assert.equal(requests[0].url, TOKEN_URL);
    assert.equal(requests[0].method, 'POST');
    assert.deepEqual(JSON.parse(requests[0].body), {
      grant_type: 'authorization_code', code: 'one-time-code',
      redirect_uri: MANUAL_REDIRECT_URL, client_id: CLIENT_ID,
      code_verifier: VERIFIER, state: STATE,
    });
    assert.equal(requests[1].url, PROFILE_URL);
    assert.equal(requests[1].method, 'GET');
    assert.equal(requests[1].headers.Authorization, 'Bearer new-access');
    assert.equal(requests[0].headers['User-Agent'], 'claude-desktop-swap-pilot/1');
    assert.equal(requests[0].headers['Content-Type'], 'application/json');
    assert.equal(requests[0].headers.Accept, 'application/json');
    for (const request of requests) {
      assert.equal(request.redirect, 'manual');
      assert.ok(request.signal instanceof AbortSignal);
    }
  }
});

test('both grant kinds reject incomplete or malformed successors before profile requests', async () => {
  const changes = [
    { refresh_token: null }, { access_token: '' }, { access_token: 'token\nsecret' },
    { refresh_token: 'token\0secret' }, { access_token: 'x'.repeat(16_385) },
    { refresh_token: 'é' }, { expires_in: true }, { expires_in: '3600' },
    { expires_in: 60 }, { expires_in: -1 }, { expires_in: Infinity }, { expires_in: NaN },
    { scope: 'user:inference' }, { scope: 'user:profile' }, { scope: '' },
    { scope: ['user:profile', 'user:inference'] },
    { scope: 'user:profile user:inference invalid\0scope' },
    { scope: `user:profile user:inference ${'x'.repeat(257)}` },
    { scope: `user:profile user:inference ${'x '.repeat(8192)}` },
    { token_type: 'mac' }, { token_type: null }, { token_type: 1 },
  ];
  for (const change of changes) {
    for (const refreshing of [false, true]) {
      const { provider, requests } = fake([grant(change)]);
      await fails(refreshing ? provider.refresh(credentials())
        : provider.exchangeCode('code', VERIFIER, STATE),
      refreshing ? 'incomplete-refresh' : 'incomplete-grant');
      assert.equal(requests.length, 1);
    }
  }
});

test('non-finite absolute expiry fails closed for both grants', async () => {
  for (const clock of [() => Infinity, () => NaN, () => Number.MAX_VALUE]) {
    const { provider } = fake([grant({ expires_in: Number.MAX_VALUE })], clock);
    await fails(provider.exchangeCode('code', VERIFIER, STATE), 'incomplete-grant');
    const refresh = fake([grant({ expires_in: Number.MAX_VALUE })], clock).provider;
    await fails(refresh.refresh(credentials()), 'incomplete-refresh');
  }
});

test('unknown or missing entitlements are never invented from inference scope', async () => {
  for (const update of [
    { organization_type: null }, { organization_type: 'api' },
    { organization_type: 'claude_free' }, { organization_type: [] },
    { organization_type: 'toString' }, { organization_type: '__proto__' },
    { organization_type: 'new_unknown_plan' }, { rate_limit_tier: null },
    { rate_limit_tier: '' }, { rate_limit_tier: ' default ' },
    { rate_limit_tier: 'default\nsecret' }, { rate_limit_tier: 'x'.repeat(513) },
  ]) {
    const { provider } = fake([grant(), profile(update)]);
    await fails(provider.exchangeCode('code', VERIFIER, STATE), 'unverified-subscription');
  }
});

test('profile identities must have complete bounded stable IDs and a truthful email', async () => {
  for (const value of [
    {}, { account: {}, organization: {} }, { account: [], organization: {} },
    { ...profile(), account: { uuid: 'account-a' } },
    { ...profile(), account: { uuid: 'account-a', email: 'a\n@example.test' } },
    { ...profile(), account: { uuid: '\0', email: 'a@example.test' } },
    { ...profile(), organization: { ...profile().organization, uuid: 'x'.repeat(513) } },
  ]) {
    const { provider } = fake([grant(), value]);
    await fails(provider.exchangeCode('code', VERIFIER, STATE), 'bad-profile');
  }
  const aliasProfile = profile();
  aliasProfile.account.email_address = aliasProfile.account.email;
  delete aliasProfile.account.email;
  assert.equal((await fake([grant(), aliasProfile]).provider.exchangeCode('code', VERIFIER, STATE))
    .account.emailAddress, 'a@example.test');
});

test('optional grant identity is cross-checked against token-authenticated profile', async () => {
  for (const update of [
    { account: { uuid: 'other' } }, { organization: { uuid: 'other' } },
    { account: {} }, { organization: 'org-a' }, { account: [] },
  ]) {
    await fails(fake([grant(update), profile()]).provider.exchangeCode('code', VERIFIER, STATE),
      'grant-profile-mismatch');
  }
  const { provider } = fake([grant({ account: { uuid: 'account-a' },
    organization: { uuid: 'org-a' } }), profile()]);
  assert.equal((await provider.exchangeCode('code', VERIFIER, STATE)).account.accountUuid, 'account-a');
});

test('usage validates all subscription windows and leaves extra usage out', async () => {
  const { provider, requests } = fake([{
    five_hour: { utilization: 20 }, seven_day: { utilization: 30 },
    seven_day_opus: { utilization: 70 }, seven_day_sonnet: null,
    extra_usage: { is_enabled: true, utilization: 100 },
  }]);
  assert.deepEqual(await provider.usage('access-secret'), { five_hour: 20, seven_day: 30, seven_day_opus: 70 });
  assert.equal(requests[0].url, USAGE_URL);
  assert.equal(requests[0].headers.Authorization, 'Bearer access-secret');
  assert.equal(requests[0].headers['anthropic-beta'], 'oauth-2025-04-20');
});

test('weekly row aggregates are metadata rather than quota windows', async () => {
  const aggregate = { as_of: '2026-10-26T12:00:00Z', rows: [],
    window_started_at: '2026-10-20T12:00:00Z' };
  const { provider } = fake([{
    five_hour: { utilization: 40 }, seven_day: { utilization: 78 },
    seven_day_opus: null, seven_day_sonnet: null,
    seven_day_breakdown: aggregate,
    seven_day_other_aggregate: { ...aggregate, rows: [{ percent: 99 }] },
    extra_usage: { is_enabled: true, utilization: 100 },
  }]);
  assert.deepEqual(await provider.usage('access'), { five_hour: 40, seven_day: 78 });
});

test('metadata classification never hides required or malformed actual windows', async () => {
  const base = { five_hour: { utilization: 40 }, seven_day: { utilization: 78 } };
  for (const value of [
    { ...base, five_hour: { rows: [] } },
    { ...base, seven_day: { rows: [] } },
    { ...base, seven_day_opus: {} },
    { ...base, seven_day_opus: { resets_at: '2026-10-27T00:00:00Z' } },
    { ...base, seven_day_opus: { rows: [], utilization: null } },
    { ...base, seven_day_opus: { rows: [], utilization: '70' } },
    { ...base, seven_day_opus: { rows: [], resets_at: null } },
    { ...base, seven_day_breakdown: { rows: 'invalid' } },
  ]) await fails(fake([value]).provider.usage('access'), 'bad-usage');
  assert.equal((await fake([{ ...base, seven_day_opus: { rows: [], utilization: 95 } }])
    .provider.usage('access')).seven_day_opus, 95);
});

test('current limits shape includes nonbinding per-model quotas and excludes spending', async () => {
  const scoped = { kind: 'weekly_scoped', group: 'weekly', is_active: false, percent: 0,
    scope: { model: { id: null, display_name: 'Fable' }, surface: null } };
  const base = { five_hour: { utilization: 40 }, seven_day: { utilization: 78 },
    seven_day_breakdown: { as_of: '2026-10-26T12:00:00Z', rows: [],
      window_started_at: '2026-10-20T12:00:00Z' },
    limits: [
      { kind: 'session', group: 'session', is_active: false, percent: 40, scope: null },
      { kind: 'weekly_all', group: 'weekly', is_active: true, percent: 78, scope: {} },
      scoped,
      { kind: 'extra_usage', group: 'spend', percent: 100, scope: { model: { display_name: 'Fable' } } },
    ],
    extra_usage: { is_enabled: true, utilization: 100 },
  };
  assert.deepEqual(await fake([base]).provider.usage('access'), {
    five_hour: 40, seven_day: 78, 'weekly_scoped:fable': 0,
  });
  base.limits.push({ ...scoped, percent: 95 },
    { ...scoped, percent: 10, scope: { model: { display_name: 'FABLE' } } });
  assert.equal((await fake([base]).provider.usage('access'))['weekly_scoped:fable'], 95);
});

test('malformed scoped quotas fail closed instead of disappearing from model-all selection', async () => {
  const scoped = { kind: 'weekly_scoped', group: 'weekly', is_active: false, percent: 0,
    scope: { model: { display_name: 'Fable' } } };
  const base = { five_hour: { utilization: 40 }, seven_day: { utilization: 78 } };
  for (const change of [
    { percent: null }, { percent: true }, { percent: '95' }, { percent: -1 },
    { percent: NaN }, { percent: Infinity }, { percent: undefined },
    { group: null }, { group: 'spend' }, { scope: null }, { scope: {} },
    { scope: { model: { display_name: '' } } },
    { scope: { model: { display_name: 'Fable\n' } } },
  ]) await fails(fake([{ ...base, limits: [{ ...scoped, ...change }] }]).provider.usage('access'), 'bad-usage');
  for (const limits of [{}, 'unknown', [null]]) {
    await fails(fake([{ ...base, limits }]).provider.usage('access'), 'bad-usage');
  }
  assert.deepEqual(await fake([{ ...base, limits: null }]).provider.usage('access'), { five_hour: 40, seven_day: 78 });
});

test('missing or invalid usage is unknown, while over-100 utilization stays exhausted', async () => {
  for (const value of [{}, { five_hour: null, seven_day: { utilization: 1 } },
    ...[true, -1, '0', NaN, Infinity, null].map(utilization => ({
      five_hour: { utilization }, seven_day: { utilization: 1 },
    })), { five_hour: { utilization: 1 }, seven_day: { utilization: 1 }, seven_day_opus: {} },
  ]) {
    await fails(fake([value]).provider.usage('access'), 'bad-usage');
  }
  assert.equal((await fake([{ five_hour: { utilization: 101 }, seven_day: { utilization: 100 } }])
    .provider.usage('access')).five_hour, 101);
});

test('refresh preserves imported fields without mutating its input or replaying the predecessor', async () => {
  const source = credentials();
  const { provider, requests } = fake([grant()]);
  const result = await provider.refresh(source);
  assert.equal(result.expires_at, 4100);
  assert.equal(result.refresh_token, 'new-refresh');
  assert.deepEqual(result.account, source.account);
  assert.equal(source.refresh_token, 'old-refresh');
  assert.deepEqual(JSON.parse(requests[0].body), {
    grant_type: 'refresh_token', refresh_token: 'old-refresh', client_id: CLIENT_ID,
  });
  assert.equal(requests.length, 1);
});

test('invalid refresh predecessors and bearer inputs never reach the provider', async () => {
  for (const source of [null, [], {}, { refresh_token: '' }, { refresh_token: 'secret\n' }]) {
    const { provider, requests } = fake([]);
    await fails(provider.refresh(source), 'incomplete-refresh');
    assert.equal(requests.length, 0);
  }
  for (const token of [null, '', 'secret\n', 'é', 'x'.repeat(16_385)]) {
    const { provider, requests } = fake([]);
    await fails(provider.identity(token), 'invalid-access-token');
    assert.equal(requests.length, 0);
  }
});

test('transport errors expose only a fixed code and never retry grants', async () => {
  const { provider, requests } = fake([new Error('secret-code token private body')]);
  await fails(provider.exchangeCode('secret-code', VERIFIER, STATE), 'transport-or-response-error');
  assert.equal(requests.length, 1);
});

test('HTTP errors discard their bodies and bound numeric Retry-After', async () => {
  for (const [header, expected] of [['123', 123], ['-20', 0], ['1000000', 86400],
    ['NaN', 0], ['Infinity', 0], ['not a number', 0], ['', 0], ['0x10', 0],
    ['Wed, 21 Oct 2026 07:28:00 GMT', 0], ['1.5', 1.5]]) {
    const response = new Response('secret-response-with-access-token', {
      status: 429, headers: { 'Retry-After': header },
    });
    await fails(fake([response]).provider.usage('secret-bearer'), 'http-429', expected);
    assert.ok(response.bodyUsed);
  }
  await fails(fake([new Response('private', { status: 401 })]).provider.identity('secret'), 'http-401');
});

test('redirects are refused without sending credentials to a new destination', async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    const { provider, requests } = fake([new Response('private', {
      status, headers: { Location: 'https://evil.example/steal' },
    })]);
    await fails(provider.identity('secret'), `http-${status}`);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, PROFILE_URL);
    assert.equal(requests[0].redirect, 'manual');
  }
  const response = new Response(JSON.stringify(profile()));
  Object.defineProperty(response, 'redirected', { value: true });
  await fails(fake([response]).provider.identity('secret'), 'bad-response');
});

test('only fixed provider endpoints can receive secrets', async () => {
  const { provider, requests } = fake([]);
  await fails(provider.request('https://evil.example', { token: 'secret' }), 'invalid-provider-endpoint');
  assert.equal(requests.length, 0);
});

test('streamed responses are bounded even without Content-Length', async () => {
  let reads = 0;
  let cancelled = false;
  const response = new Response(new ReadableStream({
    pull(controller) {
      reads += 1;
      controller.enqueue(new Uint8Array(64 * 1024));
    },
    cancel() { cancelled = true; },
  }));
  await fails(fake([response]).provider.identity('secret'), 'bad-response');
  assert.ok(cancelled);
  assert.ok(reads <= MAX_RESPONSE / (64 * 1024) + 2);
});

test('advertised oversized responses are cancelled before decoding', async () => {
  const response = new Response('{}', { headers: { 'Content-Length': String(MAX_RESPONSE + 1) } });
  await fails(fake([response]).provider.identity('secret'), 'bad-response');
  assert.ok(response.bodyUsed);
});

test('the full response-size allowance can be decoded across arbitrary chunk boundaries', async () => {
  const bytes = new TextEncoder().encode(JSON.stringify(profile()).padEnd(MAX_RESPONSE));
  let offset = 0;
  const response = new Response(new ReadableStream({
    pull(controller) {
      if (offset === bytes.length) {
        controller.close();
        return;
      }
      const end = Math.min(offset + 8191, bytes.length);
      controller.enqueue(bytes.subarray(offset, end));
      offset = end;
    },
  }));
  assert.equal((await fake([response]).provider.identity('secret')).accountUuid, 'account-a');
});

test('JSON decoding rejects scalar, array, empty, corrupt, and invalid UTF-8 bodies', async () => {
  for (const value of [null, [], 'secret-string', 4, false]) {
    await fails(fake([value]).provider.identity('secret'), 'bad-response');
  }
  for (const body of ['', '{broken secret', new Uint8Array([0xff, 0xfe])]) {
    await fails(fake([new Response(body)]).provider.identity('secret'), 'transport-or-response-error');
  }
  await fails(fake([new Response(null, { status: 204 })]).provider.identity('secret'), 'bad-response');
});

test('stream failures expose no private body text', async () => {
  const response = new Response(new ReadableStream({
    start(controller) { controller.error(new Error('private refresh token')); },
  }));
  await fails(fake([response]).provider.identity('secret'), 'transport-or-response-error');
});

test('request timeout aborts the transport and emits a fixed error', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let signal;
  const provider = new Provider({ fetcher: (_url, init) => {
    signal = init.signal;
    return new Promise((_, reject) => signal.addEventListener('abort', () => {
      reject(new Error('aborted private request'));
    }, { once: true }));
  } });
  const result = provider.identity('secret');
  assert.equal(signal.aborted, false);
  t.mock.timers.tick(REQUEST_TIMEOUT_MS - 1);
  assert.equal(signal.aborted, false);
  t.mock.timers.tick(1);
  await fails(result, 'transport-or-response-error');
  assert.equal(signal.aborted, true);
});

test('the timeout remains active while a response body is streaming', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let started;
  const bodyStarted = new Promise(resolve => { started = resolve; });
  const provider = new Provider({ fetcher: async (_url, { signal }) => new Response(new ReadableStream({
    start(controller) {
      signal.addEventListener('abort', () => controller.error(new Error('private stalled body')), { once: true });
    },
    pull() { started(); },
  })) });
  const result = provider.identity('secret');
  await bodyStarted;
  t.mock.timers.tick(REQUEST_TIMEOUT_MS);
  await fails(result, 'transport-or-response-error');
});

test('identity reads only actual stable IDs and optional email without inventing entitlements', async () => {
  const { provider } = fake([{ account: { uuid: 'a', email_address: 'a@example.test' },
    organization: { uuid: 'org-a' } }]);
  assert.deepEqual(await provider.identity('secret'), {
    accountUuid: 'a', organizationUuid: 'org-a', emailAddress: 'a@example.test',
  });
  await fails(fake([{ account: { uuid: 'a' } }]).provider.identity('secret'), 'bad-profile');
});
