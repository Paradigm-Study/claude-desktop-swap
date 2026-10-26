import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { afterEach, beforeEach, test } from 'node:test';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

const ADMIN = 'test-admin-token-which-is-more-than-thirty-two-bytes';
const MASTER = Buffer.alloc(32, 0x71).toString('base64');
const fixture = fileURLToPath(new URL('./fixtures/worker.mjs', import.meta.url));
const TOKEN_PATH = '/v1/oauth/token';
const PROFILE_PATH = '/api/oauth/profile';
const USAGE_PATH = '/api/oauth/usage';

function credentials(name, expiresAt) {
  return {
    access_token: `access-secret-${name}`, refresh_token: `refresh-secret-${name}`,
    expires_at: expiresAt, scopes: ['user:inference', 'user:profile'],
    account: {
      accountUuid: name, emailAddress: `${name}@example.test`,
      organizationUuid: `org-${name}`, subscriptionType: 'max',
      rateLimitTier: 'default_claude_max_20x',
    },
  };
}

function header(options, name) {
  const headers = options.headers;
  if (Array.isArray(headers)) {
    for (let i = 0; i < headers.length; i += 2) {
      if (String(headers[i]).toLowerCase() === name) return String(headers[i + 1]);
    }
  } else if (headers?.get) return headers.get(name);
  else for (const [key, value] of Object.entries(headers ?? {})) {
    if (key.toLowerCase() === name) return value;
  }
  return undefined;
}

class FakeAnthropic {
  constructor() {
    this.quotas = new Map();
    this.accesses = new Map();
    this.refreshes = new Map();
    this.grants = new Map();
    this.calls = [];
    this.usageFailure = null;
    this.refreshFailure = null;
    this.identityOverride = null;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const options = { headers: request.headers };
    if (url.origin === 'https://api.anthropic.com' && url.pathname === PROFILE_PATH && request.method === 'GET') {
      this.calls.push({ type: 'profile' });
      const name = this.accesses.get(header(options, 'authorization')?.replace(/^Bearer /, ''));
      if (!name) return this.reply(401, { error: 'unknown-test-token' });
      return this.reply(200, {
        account: { uuid: this.identityOverride ?? name, email: `${name}@example.test` },
        organization: { uuid: `org-${name}`, organization_type: 'claude_max', rate_limit_tier: 'default_claude_max_20x' },
      });
    }
    if (url.origin === 'https://api.anthropic.com' && url.pathname === USAGE_PATH && request.method === 'GET') {
      this.calls.push({ type: 'usage' });
      if (this.usageFailure) return this.reply(this.usageFailure, { error: 'test-error' }, { 'retry-after': '600' });
      const name = this.accesses.get(header(options, 'authorization')?.replace(/^Bearer /, ''));
      if (!name) return this.reply(401, { error: 'unknown-test-token' });
      if (this.rawUsage) return this.reply(200, this.rawUsage);
      const quota = this.quotas.get(name) ?? { five_hour: 10, seven_day: 10 };
      return this.reply(200, Object.fromEntries(Object.entries(quota).map(([key, utilization]) => [key, { utilization }])));
    }
    if (url.origin === 'https://platform.claude.com' && url.pathname === TOKEN_PATH && request.method === 'POST') {
      const body = await request.json();
      await this.beforeGrant?.(body);
      if (body.grant_type === 'authorization_code') {
        this.calls.push({ type: 'exchange', code: body.code, verifier: body.code_verifier, state: body.state });
        const name = this.grants.get(body.code);
        if (!name) return this.reply(400, { error: 'invalid_grant' });
        return this.grant(name, 'contributed');
      }
      assert.equal(body.grant_type, 'refresh_token');
      this.calls.push({ type: 'refresh' });
      if (this.refreshFailure) return this.reply(this.refreshFailure, { error: 'sensitive-provider-error' });
      const name = this.refreshes.get(body.refresh_token);
      if (!name) return this.reply(400, { error: 'invalid_grant' });
      this.refreshes.delete(body.refresh_token);
      return this.grant(name, 'renewed');
    }
    throw new Error(`Unexpected outbound request blocked by test: ${request.method} ${url.origin}${url.pathname}`);
  }

  reply(statusCode, body, headers = {}) {
    return Response.json(body, { status: statusCode, headers });
  }

  register(value, quota = 10) {
    const name = value.account.accountUuid;
    this.accesses.set(value.access_token, name);
    this.refreshes.set(value.refresh_token, name);
    this.quotas.set(name, { five_hour: quota, seven_day: 10 });
  }

  grant(name, prefix) {
    const access = `${prefix}-access-secret-${name}`;
    const refresh = `${prefix}-refresh-secret-${name}`;
    this.accesses.set(access, name);
    this.refreshes.set(refresh, name);
    return this.reply(200, { access_token: access, refresh_token: refresh,
      expires_in: 3600, token_type: 'Bearer', scope: 'user:inference user:profile' });
  }

  count(type) { return this.calls.filter((call) => call.type === type).length; }
}

class Harness {
  async start() {
    this.path = await mkdtemp(join(tmpdir(), 'claude-pool-do-'));
    this.now = Math.floor(Date.now() / 1000);
    this.provider = new FakeAnthropic();
    await this.boot();
  }

  async boot() {
    const main = this.production ? fileURLToPath(new URL('../src/worker.mjs', import.meta.url)) : fixture;
    this.mf = new Miniflare(convertV4MiniflareOptions({
      name: 'pool-integration',
      modules: await Promise.all([main, fileURLToPath(new URL('../src/pool.mjs', import.meta.url)), fileURLToPath(new URL('../src/provider.mjs', import.meta.url))].map(async (path) => ({ type: 'ESModule', path, contents: await readFile(path, 'utf8') }))),
      compatibilityDate: '2026-04-01',
      durableObjects: { POOL: { className: this.production ? 'PoolObject' : 'FixturePool', useSQLite: true } },
      resourcePersistencePath: this.path,
      bindings: { POOL_MASTER_KEY: MASTER, POOL_ADMIN_TOKEN: ADMIN, POOL_SWITCH_THRESHOLD: '98', FIXTURE_TIME: this.now, ...this.bindingOverrides },
      ratelimits: { ENROLL_LIMIT: { namespace_id: '991122', simple: { limit: 2, period: 60 } } },
      outboundService: (request) => this.provider.fetch(request),
    }));
    await this.mf.ready;
  }

  async restart() { await this.mf.dispose(); await this.boot(); }
  async close() { await this.mf?.dispose(); await rm(this.path, { recursive: true, force: true }); }

  async request(path, body = {}, token, { raw, expected = 200 } = {}) {
    const response = await this.mf.dispatchFetch(`https://pool.example.test${path}`, {
      method: 'POST', headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) },
      body: raw ?? JSON.stringify(body),
    });
    const value = await response.json();
    assert.equal(response.status, expected, `${path}: ${JSON.stringify(value)}`);
    if (!path.startsWith('/__test/')) assert.equal(response.headers.get('cache-control'), 'no-store');
    return value;
  }

  admin(path, body = {}, options) { return this.request(`/admin/${path}`, body, ADMIN, options); }
  async invite() { return (await this.admin('invites', { ttl: 86400 })).invitation; }
  async enroll(name = 'Test Mac') { return this.request('/v1/enroll', { invitation: await this.invite(), device_name: name }); }
  async add(name = 'a', expiry = this.now + 10000, quota = 10) {
    const value = credentials(name, expiry);
    this.provider.register(value, quota);
    await this.admin('accounts/import', { account_id: name, credentials: value, exclusive_owner: true });
    return value;
  }
  assignment(device, options) { return this.request('/v1/assignment', {}, device.device_token, options); }
  async advance(seconds) { this.now += seconds; await this.request('/__test/clock', { now: this.now }); }
  async sql(sql, ...args) { return (await this.request('/__test/sql', { sql, args })).rows; }
  startContribution(device) { return this.request('/v1/contributions/start', {}, device.device_token); }
  code(operation, code = 'valid-manual-code') { return `${code}#${new URL(operation.authorization_url).searchParams.get('state')}`; }
  complete(device, operation, code = this.code(operation), options) {
    return this.request('/v1/contributions/complete', { contribution_id: operation.contribution_id, code }, device.device_token, options);
  }
  receipt(action, device, operation, options) {
    return this.request(`/v1/contributions/${action}`, { contribution_id: operation.contribution_id }, device.device_token, options);
  }
}

let h;
beforeEach(async () => { h = new Harness(); await h.start(); });
afterEach(async () => { await h?.close(); });

test('single-use invitation redemption is atomic under concurrent devices', async () => {
  const invitation = await h.invite();
  const responses = await Promise.all(Array.from({ length: 16 }, (_, i) => h.mf.dispatchFetch('https://pool.example.test/v1/enroll', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ invitation, device_name: `Mac ${i}` }),
  })));
  assert.equal(responses.filter((response) => response.status === 200).length, 1);
  assert.ok(responses.every((response) => [200, 401].includes(response.status)));
  assert.equal((await h.admin('devices/list')).devices.length, 1);
  await Promise.all(responses.map((response) => response.arrayBuffer()));
  const expired = await h.admin('invites', { ttl: 30 });
  await h.advance(31);
  await h.request('/v1/enroll', { invitation: expired.invitation, device_name: 'Late Mac' }, undefined, { expected: 401 });
});

test('two Macs balance fresh accounts, keep sticky assignments, and switch at configured quota', async () => {
  await h.add('a', undefined, 10); await h.add('b', undefined, 20);
  const first = await h.enroll('First Mac'); const second = await h.enroll('Second Mac');
  assert.equal((await h.assignment(first)).account_id, 'a');
  assert.equal((await h.assignment(first)).account_id, 'a');
  assert.equal((await h.assignment(second)).account_id, 'b');
  assert.equal(h.provider.count('usage'), 2);
  h.provider.quotas.set('a', { five_hour: 98, seven_day: 10 });
  await h.advance(121);
  const switched = await h.assignment(first);
  assert.equal(switched.account_id, 'b');
  assert.equal(switched.state, 'fresh');
  assert.equal(Object.hasOwn(switched, 'refresh_token'), false);
  assert.deepEqual(Object.keys(switched.account).sort(), Object.keys(credentials('b', 1).account).sort());
});

test('admin account diagnostics expose cached actual usage and a safe label without credentials or provider calls', async () => {
  const value = await h.add('measured');
  const before = (await h.admin('accounts/list')).accounts[0];
  assert.equal(before.usage, null);
  assert.deepEqual(before.account, { emailAddress: 'measured@example.test', subscriptionType: 'max' });
  assert.equal(h.provider.calls.length, 0, 'listing imported accounts must not contact the provider');
  h.provider.rawUsage = {
    five_hour: { utilization: 40, resets_at: '2026-10-27T02:00:00Z' },
    seven_day: { utilization: 78, resets_at: '2026-10-30T02:00:00Z' },
    seven_day_breakdown: { as_of: '2026-10-26T20:00:00Z', window_started_at: '2026-10-23T02:00:00Z', rows: [] },
    limits: [{ kind: 'weekly_scoped', group: 'weekly', percent: 7.8,
      is_active: false, scope: { model: { display_name: 'Fable' } } }],
  };
  const device = await h.enroll();
  assert.equal((await h.assignment(device)).account_id, 'measured');
  const calls = h.provider.calls.length;
  const response = await h.admin('accounts/list');
  const account = response.accounts[0];
  assert.deepEqual(account.usage, { five_hour: 40, seven_day: 78, 'weekly_scoped:fable': 7.8 });
  assert.deepEqual(account.account, { emailAddress: 'measured@example.test', subscriptionType: 'max' });
  assert.equal(account.verified, 1);
  assert.equal(account.state, 'ready');
  assert.equal(account.fetched, h.now);
  assert.deepEqual(Object.keys(account).sort(), [
    'account', 'error', 'expires_at', 'fetched', 'generation', 'id', 'next_poll', 'state', 'usage', 'verified',
  ]);
  assert.equal(h.provider.calls.length, calls, 'admin diagnostics must use cached observations');
  const raw = JSON.stringify(response);
  const encrypted = (await h.sql('SELECT secret FROM accounts WHERE id=?', 'measured'))[0].secret;
  for (const forbidden of [value.access_token, value.refresh_token, encrypted, 'access_token', 'refresh_token',
    'accountUuid', 'organizationUuid', 'rateLimitTier', 'scopes', 'secret']) {
    assert.equal(raw.includes(forbidden), false, 'admin diagnostics must not return credential material or additional identity fields');
  }
  await h.request('/admin/accounts/list', {}, device.device_token, { expected: 401 });
  const health = await h.mf.dispatchFetch('https://pool.example.test/health');
  assert.deepEqual(await health.json(), { ok: true, mode: 'native-subscription', version: '0.1.0a2' });
  assert.equal(h.provider.calls.length, calls);
});

test('stale quota preserves an existing assignment and refuses new device assignment', async () => {
  await h.add(); const first = await h.enroll(); await h.assignment(first);
  h.provider.usageFailure = 429; await h.advance(301);
  assert.equal((await h.assignment(first)).state, 'quota_stale');
  const calls = h.provider.count('usage'); await h.advance(10);
  await h.assignment(first);
  assert.equal(h.provider.count('usage'), calls, 'retry-after should prevent another usage request');
  await h.assignment(await h.enroll(), { expected: 503 });
});

test('exhausted quota is explicit and prevents a new assignment, including model windows', async () => {
  await h.add(); const first = await h.enroll(); await h.assignment(first);
  h.provider.quotas.set('a', { five_hour: 10, seven_day: 10, seven_day_sonnet: 100 });
  await h.advance(121);
  assert.equal((await h.assignment(first)).state, 'quota_exhausted');
  await h.assignment(await h.enroll(), { expected: 503 });
});

test('concurrent assignments refresh once and the successor survives real SQLite restart', async () => {
  await h.add('a', h.now + 200);
  const first = await h.enroll(); const second = await h.enroll();
  const assignments = await Promise.all(Array.from({ length: 12 }, (_, i) => h.assignment(i % 2 ? first : second)));
  assert.equal(h.provider.count('refresh'), 1);
  for (const assignment of assignments) {
    assert.equal(assignment.access_token, 'renewed-access-secret-a');
    assert.equal(assignment.generation, 2);
  }
  await h.restart();
  assert.equal((await h.assignment(first)).generation, 2);
  assert.equal(h.provider.count('refresh'), 1);
});

test('an ambiguous refresh quarantines its lineage and is never replayed after restart', async () => {
  await h.add('a', h.now + 200); const first = await h.enroll();
  h.provider.refreshFailure = 500;
  await h.assignment(first, { expected: 503 });
  assert.equal(h.provider.count('refresh'), 1);
  assert.equal((await h.admin('accounts/list')).accounts[0].state, 'quarantined');
  await h.restart(); h.provider.refreshFailure = null;
  await h.assignment(first, { expected: 503 });
  assert.equal(h.provider.count('refresh'), 1);
  assert.doesNotMatch(JSON.stringify(await h.sql('SELECT * FROM accounts')), /sensitive-provider-error/);
});

test('constructor recovery quarantines an interrupted durable refresh before another grant', async () => {
  await h.add(); const first = await h.enroll();
  await h.sql("UPDATE accounts SET state='refreshing'");
  await h.restart();
  await h.assignment(first, { expected: 503 });
  assert.equal((await h.admin('accounts/list')).accounts[0].state, 'quarantined');
  assert.equal(h.provider.count('refresh'), 0);
});

test('identity mismatch quarantines before access token delivery', async () => {
  await h.add(); h.provider.identityOverride = 'wrong-account';
  await h.assignment(await h.enroll(), { expected: 503 });
  assert.equal((await h.admin('accounts/list')).accounts[0].state, 'quarantined');
});

test('authentication and revocation reject malformed request bodies before provider work', async () => {
  const paths = ['/v1/assignment', '/v1/contributions/start', '/v1/contributions/complete', '/v1/contributions/status', '/v1/contributions/cancel'];
  for (const path of paths) await h.request(path, {}, 'unknown-token', { raw: '{ invalid JSON', expected: 401 });
  await h.request('/admin/accounts/import', {}, 'unknown-token', { raw: 'x'.repeat(9000), expected: 401 });
  const device = await h.enroll();
  await h.admin('devices/revoke', { device_id: device.device_id });
  for (const path of paths) await h.request(path, {}, device.device_token, { raw: '{ invalid JSON', expected: 401 });
  assert.deepEqual(h.provider.calls, []);
});

test('contributed account is available to both Macs with idempotent replay and status after restart', async () => {
  const first = await h.enroll('First Mac'); const second = await h.enroll('Second Mac');
  h.provider.grants.set('valid-manual-code', 'donated');
  const operation = await h.startContribution(first);
  assert.equal(operation.state, 'pending');
  assert.equal(operation.expires_at, h.now + 600);
  assert.equal(new URL(operation.authorization_url).origin, 'https://claude.com');
  assert.deepEqual(await h.startContribution(first), operation);
  const receipts = await Promise.all(Array.from({ length: 12 }, () => h.complete(first, operation)));
  assert.equal(receipts[0].state, 'added');
  for (const receipt of receipts) assert.deepEqual(receipt, receipts[0]);
  assert.equal(h.provider.count('exchange'), 1);
  assert.deepEqual(Object.keys(receipts[0]).sort(), ['account_id', 'contribution_id', 'expires_at', 'state']);
  for (const device of [first, second]) {
    const assignment = await h.assignment(device);
    assert.equal(assignment.account.accountUuid, 'donated');
    assert.equal(assignment.account_id, receipts[0].account_id);
    assert.equal(Object.hasOwn(assignment, 'refresh_token'), false);
  }
  await h.restart();
  assert.deepEqual(await h.receipt('status', first, operation), receipts[0]);
  assert.deepEqual(await h.complete(first, operation), receipts[0]);
  assert.equal(h.provider.count('exchange'), 1);
  assert.equal((await h.sql('SELECT secret FROM contributions'))[0].secret, null);
});

test('contribution ownership, cancellation, expiry and revoked credentials cannot consume a grant', async () => {
  const first = await h.enroll(); const second = await h.enroll();
  const operation = await h.startContribution(first);
  for (const action of ['status', 'cancel']) await h.receipt(action, second, operation, { expected: 404 });
  await h.complete(second, operation, h.code(operation), { expected: 404 });
  const cancelled = await h.receipt('cancel', first, operation);
  assert.equal(cancelled.state, 'cancelled');
  assert.deepEqual(await h.complete(first, operation), cancelled);
  const expired = await h.startContribution(first); await h.advance(601);
  assert.equal((await h.complete(first, expired)).state, 'expired');
  const revoked = await h.startContribution(first);
  await h.admin('devices/revoke', { device_id: first.device_id });
  await h.complete(first, revoked, h.code(revoked), { expected: 401 });
  assert.equal(h.provider.count('exchange'), 0);
});

test('wrong callback state has a durable attempt limit and never reaches provider exchange', async () => {
  const first = await h.enroll(); const operation = await h.startContribution(first);
  const badCode = `valid-manual-code#${'x'.repeat(43)}`;
  await h.complete(first, operation, badCode, { expected: 400 });
  await h.restart();
  await h.complete(first, operation, badCode, { expected: 400 });
  const failed = await h.complete(first, operation, badCode);
  assert.equal(failed.state, 'failed');
  assert.deepEqual(await h.complete(first, operation), failed);
  assert.equal(h.provider.count('exchange'), 0);
  assert.equal((await h.sql('SELECT secret FROM contributions'))[0].secret, null);
});

test('contribution start limits persist across restarts and are per device', async () => {
  const first = await h.enroll(); const second = await h.enroll();
  for (let i = 0; i < 5; i++) await h.receipt('cancel', first, await h.startContribution(first));
  await h.restart();
  await h.request('/v1/contributions/start', {}, first.device_token, { expected: 429 });
  assert.equal((await h.startContribution(second)).state, 'pending');
});

test('duplicate contribution does not replace existing rotating credential lineage', async () => {
  await h.add('donated'); const first = await h.enroll();
  const before = await h.sql('SELECT secret,generation FROM accounts WHERE id=?', 'donated');
  h.provider.grants.set('valid-manual-code', 'donated');
  const operation = await h.startContribution(first);
  const receipt = await h.complete(first, operation);
  assert.equal(receipt.state, 'duplicate'); assert.equal(receipt.account_id, 'donated');
  assert.deepEqual(await h.sql('SELECT secret,generation FROM accounts WHERE id=?', 'donated'), before);
  assert.equal((await h.admin('accounts/list')).accounts.length, 1);
  assert.deepEqual(await h.complete(first, operation), receipt);
  assert.equal(h.provider.count('exchange'), 1);
});

test('interrupted exchange becomes uncertain after restart and cannot be replayed or cancelled', async () => {
  const first = await h.enroll(); const operation = await h.startContribution(first);
  await h.sql("UPDATE contributions SET state='exchanging' WHERE id=?", operation.contribution_id);
  await h.restart();
  const receipt = await h.complete(first, operation);
  assert.equal(receipt.state, 'uncertain');
  assert.deepEqual(await h.receipt('cancel', first, operation), receipt);
  assert.equal(h.provider.count('exchange'), 0);
  assert.equal((await h.sql('SELECT secret FROM contributions'))[0].secret, null);
});

test('an OAuth exchange failure produces a secret-free terminal receipt with no replay', async () => {
  const first = await h.enroll(); const operation = await h.startContribution(first);
  const receipt = await h.complete(first, operation);
  assert.equal(receipt.state, 'uncertain');
  assert.equal(h.provider.count('exchange'), 1);
  await h.restart();
  assert.deepEqual(await h.complete(first, operation), receipt);
  assert.equal(h.provider.count('exchange'), 1);
  assert.equal((await h.admin('accounts/list')).accounts.length, 0);
});

async function persistedPoolQuery(path, sql) {
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const file = join(path, entry.name);
    if (entry.isDirectory()) {
      const value = await persistedPoolQuery(file, sql);
      if (value !== undefined) return value;
    } else if (entry.name.endsWith('.sqlite')) {
      const database = new DatabaseSync(file, { readOnly: true });
      try {
        if (database.prepare("SELECT 1 FROM sqlite_master WHERE name='accounts'").get()) {
          return database.prepare(sql).all();
        }
      } finally { database.close(); }
    }
  }
  return undefined;
}

test('grant ownership markers are committed on disk before either provider grant request', async () => {
  await h.add('a', h.now + 200); const first = await h.enroll();
  h.provider.beforeGrant = async (body) => {
    const table = body.grant_type === 'refresh_token' ? 'accounts' : 'contributions';
    const expected = body.grant_type === 'refresh_token' ? 'refreshing' : 'exchanging';
    // Read through a separate native SQLite connection while the worker is
    // awaiting its HTTP response. No fixture SQL call can flush the marker.
    const rows = await persistedPoolQuery(h.path, `SELECT state FROM ${table}`);
    assert.ok(rows?.some((row) => row.state === expected));
  };
  await h.assignment(first);
  h.provider.grants.set('valid-manual-code', 'donated');
  assert.equal((await h.complete(first, await h.startContribution(first))).state, 'added');
  assert.equal(h.provider.count('refresh'), 1);
  assert.equal(h.provider.count('exchange'), 1);
});

test('failed receipt persistence rolls back the account insertion atomically and never replays the grant', async () => {
  const first = await h.enroll(); const operation = await h.startContribution(first);
  h.provider.grants.set('valid-manual-code', 'donated');
  await h.request('/__test/fail-receipt');
  const receipt = await h.complete(first, operation);
  assert.equal(receipt.state, 'uncertain');
  assert.equal((await h.admin('accounts/list')).accounts.length, 0);
  assert.equal(h.provider.count('exchange'), 1);
  await h.restart();
  assert.deepEqual(await h.complete(first, operation), receipt);
  assert.equal(h.provider.count('exchange'), 1);
});

async function fileContents(path) {
  const result = [];
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const file = join(path, entry.name);
    if (entry.isDirectory()) result.push(...await fileContents(file));
    else result.push(await readFile(file));
  }
  return result;
}

test('SQLite persistence contains ciphertext and hashes, never credentials, PKCE or enrollment tokens', async () => {
  const value = await h.add(); const invitation = await h.invite();
  const first = await h.request('/v1/enroll', { invitation, device_name: 'Private Mac' });
  await h.assignment(first);
  const operation = await h.startContribution(first);
  const state = new URL(operation.authorization_url).searchParams.get('state');
  const accounts = await h.sql('SELECT * FROM accounts');
  const contributions = await h.sql('SELECT * FROM contributions');
  assert.match(accounts[0].secret, /^v1:/);
  assert.match(contributions[0].secret, /^v1:/);
  await h.restart();
  const persisted = Buffer.concat(await fileContents(h.path));
  for (const secret of [value.access_token, value.refresh_token, value.account.emailAddress, invitation, first.device_token, state, MASTER]) {
    assert.equal(persisted.includes(Buffer.from(secret)), false, 'secret must not appear in durable SQLite files');
  }
});

test('production entry enforces HTTPS, configuration and enrollment throttling through the actual Durable Object', async () => {
  h.production = true; await h.restart();
  const health = await h.mf.dispatchFetch('https://pool.example.test/health');
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true, mode: 'native-subscription', version: '0.1.0a2' });
  assert.equal(health.headers.get('cache-control'), 'no-store');
  const insecure = await h.mf.dispatchFetch('http://pool.example.test/health');
  assert.equal(insecure.status, 400); assert.equal((await insecure.json()).error, 'https-required');
  const query = await h.mf.dispatchFetch('https://pool.example.test/health?token=bad');
  assert.equal(query.status, 400); assert.equal((await query.json()).error, 'query-not-supported');
  const testRoute = await h.mf.dispatchFetch('https://pool.example.test/__test/sql', { method: 'POST', body: '{}' });
  assert.equal(testRoute.status, 404); await testRoute.arrayBuffer();
  const first = await h.enroll('Production entry Mac');
  assert.ok(first.device_token);
  await h.request('/v1/enroll', { invitation: 'invalid-invitation', device_name: 'Unknown' }, undefined, { expected: 401 });
  await h.request('/v1/enroll', { invitation: 'invalid-invitation', device_name: 'Throttled' }, undefined, { expected: 429 });
  assert.equal((await h.admin('devices/list')).devices.length, 1);
  h.bindingOverrides = { POOL_ADMIN_TOKEN: '' }; await h.restart();
  const missing = await h.mf.dispatchFetch('https://pool.example.test/health');
  assert.equal(missing.status, 503); assert.equal((await missing.json()).error, 'coordinator-not-configured');
  assert.deepEqual(h.provider.calls, []);
});
