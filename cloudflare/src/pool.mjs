import { DurableObject } from 'cloudflare:workers';
import { Provider, ProviderError } from './provider.mjs';

// One named Durable Object owns every refresh lineage in the pool. Inference
// never passes through this service; enrolled Macs receive access tokens only.
const FRESH_SECONDS = 300;
const POLL_SECONDS = 120;
const EXPIRY_MARGIN = 60;
const REFRESH_MARGIN = 300;
const DEVICE_ACTIVE_SECONDS = 300;
const CONTRIBUTION_TTL = 600;
const METADATA = ['accountUuid', 'emailAddress', 'organizationUuid', 'subscriptionType', 'rateLimitTier'];
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });

export class PoolError extends Error {
  constructor(code, status = 400) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function keys(value, required, optional = []) {
  return object(value) && required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
}

function base64(bytes) {
  const array = new Uint8Array(bytes);
  let binary = '';
  for (let offset = 0; offset < array.length; offset += 16384) {
    binary += String.fromCharCode(...array.subarray(offset, offset + 16384));
  }
  return btoa(binary);
}

function unbase64(value) {
  return Uint8Array.from(atob(value), char => char.charCodeAt(0));
}

function randomToken(bytes) {
  return base64(crypto.getRandomValues(new Uint8Array(bytes)))
    .replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

async function digest(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value)));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

async function secretEqual(left, right) {
  // Compare fixed-size digests, including when the input lengths differ.
  const [a, b] = await Promise.all([digest(left), digest(right)]);
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a.charCodeAt(index) ^ b.charCodeAt(index);
  return difference === 0;
}

export function validateCredentials(value) {
  if (!keys(value, ['access_token', 'refresh_token', 'expires_at', 'scopes', 'account'])) {
    throw new PoolError('invalid-credential-schema');
  }
  for (const field of ['access_token', 'refresh_token']) {
    if (typeof value[field] !== 'string' || value[field].length < 1 || value[field].length > 16384 || /\s/u.test(value[field])) {
      throw new PoolError('invalid-credential-schema');
    }
  }
  if (typeof value.expires_at !== 'number' || !Number.isFinite(value.expires_at) || value.expires_at <= 0) {
    throw new PoolError('invalid-credential-expiry');
  }
  if (!Array.isArray(value.scopes) || !value.scopes.includes('user:inference')
      || value.scopes.some(scope => typeof scope !== 'string' || !scope || scope.length > 256)) {
    throw new PoolError('invalid-credential-scopes');
  }
  if (!keys(value.account, METADATA)
      || METADATA.some(key => typeof value.account[key] !== 'string' || !value.account[key] || value.account[key].length > 512)) {
    throw new PoolError('invalid-account-metadata');
  }
  return value;
}

function reply(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' },
  });
}

// JSON.parse accepts duplicate object keys. Reject those before decoding so
// authorization and credential schemas have the same interpretation everywhere.
function parseBody(text) {
  let value;
  try { value = JSON.parse(text); } catch { throw new PoolError('invalid-body'); }
  let position = 0;
  const whitespace = () => { while (/\s/.test(text[position] || '\0')) position++; };
  const string = () => {
    const start = position++;
    while (position < text.length) {
      const char = text[position++];
      if (char === '\\') position++;
      else if (char === '"') return JSON.parse(text.slice(start, position));
    }
    throw new PoolError('invalid-body');
  };
  const walk = (depth = 0) => {
    if (depth > 32) throw new PoolError('invalid-body');
    whitespace();
    const char = text[position];
    if (char === '"') { string(); return; }
    if (char === '{') {
      position++;
      whitespace();
      if (text[position] === '}') { position++; return; }
      const seen = new Set();
      while (position < text.length) {
        whitespace();
        const key = string();
        if (seen.has(key)) throw new PoolError('invalid-body');
        seen.add(key);
        whitespace();
        position++; // colon (syntax already checked by JSON.parse)
        walk(depth + 1);
        whitespace();
        if (text[position++] === '}') return;
      }
    } else if (char === '[') {
      position++;
      whitespace();
      if (text[position] === ']') { position++; return; }
      while (position < text.length) {
        walk(depth + 1);
        whitespace();
        if (text[position++] === ']') return;
      }
    } else {
      while (position < text.length && !/[\s,}\]]/.test(text[position])) position++;
      return;
    }
    throw new PoolError('invalid-body');
  };
  walk();
  if (!object(value)) throw new PoolError('invalid-body');
  return value;
}

async function readBody(request, limit) {
  const length = request.headers.get('Content-Length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) < 1 || Number(length) > limit)) {
    throw new PoolError('invalid-body-size', 413);
  }
  if (!request.body) throw new PoolError('invalid-body-size', 413);
  const reader = request.body.getReader();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => { reject(new PoolError('request-timeout', 408)); void reader.cancel().catch(() => {}); }, 15000);
  });
  try {
    const chunks = [];
    let size = 0;
    for (;;) {
      const { done, value } = await Promise.race([reader.read(), timeout]);
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        void reader.cancel().catch(() => {});
        throw new PoolError('invalid-body-size', 413);
      }
      chunks.push(value);
    }
    if (!size || (length !== null && size !== Number(length))) throw new PoolError('invalid-body-size', 413);
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    let text;
    try { text = decoder.decode(bytes); } catch { throw new PoolError('invalid-body'); }
    return parseBody(text);
  } finally {
    clearTimeout(timer);
    reader.releaseLock();
  }
}

export class PoolObject extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
    this.sql = ctx.storage.sql;
    this.provider = new Provider();
    this.lane = Promise.resolve();
    this.ready = ctx.blockConcurrencyWhile(async () => {
      let master;
      try { master = unbase64(env.POOL_MASTER_KEY); } catch { throw new Error('invalid-pool-configuration'); }
      if (master.byteLength !== 32 || typeof env.POOL_ADMIN_TOKEN !== 'string' || env.POOL_ADMIN_TOKEN.length < 32) {
        throw new Error('invalid-pool-configuration');
      }
      this.threshold = env.POOL_SWITCH_THRESHOLD === undefined ? 90 : Number(env.POOL_SWITCH_THRESHOLD);
      if (!Number.isFinite(this.threshold) || this.threshold < 1 || this.threshold > 100) throw new Error('invalid-pool-configuration');
      this.cipher = await crypto.subtle.importKey('raw', master, 'AES-GCM', false, ['encrypt', 'decrypt']);
      const schema = [
        `CREATE TABLE IF NOT EXISTS accounts (
          id TEXT PRIMARY KEY, secret TEXT NOT NULL, generation INTEGER NOT NULL,
          state TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
          usage TEXT, fetched REAL, next_poll REAL NOT NULL DEFAULT 0,
          failures INTEGER NOT NULL DEFAULT 0, error TEXT)`,
        `CREATE TABLE IF NOT EXISTS invitations (
          token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)`,
        `CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0, account_id TEXT REFERENCES accounts(id), last_seen REAL NOT NULL)`,
        `CREATE TABLE IF NOT EXISTS contributions (
          id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
          created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
          secret TEXT, invalid_attempts INTEGER NOT NULL DEFAULT 0, account_id TEXT REFERENCES accounts(id))`,
        'CREATE INDEX IF NOT EXISTS contribution_device_created ON contributions(device_id, created)',
        'CREATE TABLE IF NOT EXISTS audit (at REAL NOT NULL, event TEXT NOT NULL, subject TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS enrollment_limits (id TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL)',
      ];
      this.transaction(() => {
        for (const statement of schema) this.query(statement);
        this.query("UPDATE accounts SET state='quarantined',error='interrupted-refresh' WHERE state='refreshing'");
        this.query("UPDATE contributions SET state='uncertain',secret=NULL WHERE state='exchanging'");
        this.expireContributions();
      });
      await this.ctx.storage.sync();
    });
  }

  now() { return Date.now() / 1000; }
  query(statement, ...bindings) { return this.sql.exec(statement, ...bindings).toArray(); }
  one(statement, ...bindings) { return this.query(statement, ...bindings)[0]; }
  transaction(callback) { return this.ctx.storage.transactionSync(callback); }
  audit(event, subject) { this.query('INSERT INTO audit VALUES (?,?,?)', this.now(), event, subject); }

  async encrypt(value) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const bytes = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, this.cipher, encoder.encode(JSON.stringify(value)));
    return `v1:${base64(iv)}:${base64(bytes)}`;
  }

  async decrypt(value) {
    const [version, iv, ciphertext, extra] = value.split(':');
    if (version !== 'v1' || extra !== undefined) throw new Error('invalid-encrypted-state');
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: unbase64(iv) }, this.cipher, unbase64(ciphertext));
    return JSON.parse(decoder.decode(plain));
  }

  fetch(request) {
    // DOs serialize synchronous input, but allow interleaving at awaits. Keep
    // the entire account decision/grant/commit in a lane across network waits.
    const result = this.lane.then(async () => {
      await this.ready;
      try {
        return await this.dispatch(request);
      } catch (error) {
        // No upstream message, request body or credential ever reaches a log.
        return reply(error instanceof PoolError ? error.status : 500,
          { error: error instanceof PoolError ? error.code : 'internal-error' });
      }
    });
    this.lane = result.then(() => {}, () => {});
    return result;
  }

  async dispatch(request) {
    const url = new URL(request.url);
    if (url.search) throw new PoolError('not-found', 404);
    if (request.method === 'GET' && url.pathname === '/health') {
      return reply(200, { ok: true, mode: 'native-subscription', version: '0.1.0a2' });
    }
    if (request.method !== 'POST') throw new PoolError('not-found', 404);
    const routes = new Set([
      '/v1/enroll', '/v1/assignment', '/v1/contributions/start', '/v1/contributions/complete',
      '/v1/contributions/status', '/v1/contributions/cancel', '/admin/invites',
      '/admin/accounts/list', '/admin/accounts/import', '/admin/devices/list', '/admin/devices/revoke',
    ]);
    const path = url.pathname;
    if (!routes.has(path)) throw new PoolError('not-found', 404);
    let device;
    if (path.startsWith('/admin/')) {
      const token = this.bearer(request);
      if (!(await secretEqual(token, this.env.POOL_ADMIN_TOKEN))) throw new PoolError('unauthorized', 401);
    } else if (path !== '/v1/enroll') {
      device = await this.authenticateDevice(this.bearer(request));
    } else {
      await this.limitEnrollment(request.headers.get('CF-Connecting-IP') || 'unknown');
    }
    const body = await readBody(request, path === '/admin/accounts/import' ? 65536 : 8192);
    let result;
    switch (path) {
      case '/v1/enroll':
        this.requireKeys(body, ['invitation', 'device_name']);
        result = await this.enroll(body.invitation, body.device_name);
        break;
      case '/v1/assignment':
        this.requireKeys(body, []);
        result = await this.assignment(device);
        break;
      case '/v1/contributions/start':
        this.requireKeys(body, []);
        result = await this.contributionStart(device);
        break;
      case '/v1/contributions/complete':
        this.requireKeys(body, ['contribution_id', 'code']);
        result = await this.contributionComplete(device, body.contribution_id, body.code);
        break;
      case '/v1/contributions/status':
      case '/v1/contributions/cancel':
        this.requireKeys(body, ['contribution_id']);
        result = path.endsWith('/cancel') ? this.contributionCancel(device, body.contribution_id)
          : this.contributionStatus(device, body.contribution_id);
        break;
      case '/admin/invites':
        this.requireKeys(body, ['ttl']);
        result = await this.invite(body.ttl);
        break;
      case '/admin/accounts/list':
        this.requireKeys(body, []);
        result = { accounts: await this.listAccounts() };
        break;
      case '/admin/devices/list':
        this.requireKeys(body, []);
        result = { devices: this.query('SELECT id,name,revoked,account_id,last_seen FROM devices ORDER BY last_seen DESC') };
        break;
      case '/admin/devices/revoke':
        this.requireKeys(body, ['device_id']);
        this.revoke(body.device_id);
        result = { ok: true };
        break;
      case '/admin/accounts/import':
        this.requireKeys(body, ['account_id', 'credentials', 'exclusive_owner'], ['replace_quarantined']);
        if (body.replace_quarantined !== undefined && typeof body.replace_quarantined !== 'boolean') throw new PoolError('invalid-body');
        await this.addAccount(body.account_id, body.credentials, body.exclusive_owner, body.replace_quarantined || false);
        result = { ok: true };
        break;
    }
    await this.ctx.storage.sync();
    return reply(200, result);
  }

  requireKeys(body, required, optional = []) {
    if (!keys(body, required, optional)) throw new PoolError('invalid-body');
  }

  bearer(request) {
    const auth = request.headers.get('Authorization') || '';
    if (!/^Bearer [A-Za-z0-9_-]{32,256}$/.test(auth)) throw new PoolError('unauthorized', 401);
    return auth.slice(7);
  }

  async authenticateDevice(token) {
    const device = this.one('SELECT * FROM devices WHERE token_hash=? AND revoked=0', await digest(token));
    if (!device) throw new PoolError('unauthorized', 401);
    return device;
  }

  async limitEnrollment(address) {
    const source = await digest(address);
    const now = this.now();
    this.transaction(() => {
      this.query('DELETE FROM enrollment_limits WHERE expires<=?', now);
      for (const [seconds, maximum] of [[60, 20], [3600, 100]]) {
        const window = Math.floor(now / seconds);
        const id = `${source}:${seconds}:${window}`;
        const row = this.one('SELECT count FROM enrollment_limits WHERE id=?', id);
        if ((row?.count || 0) >= maximum) throw new PoolError('enrollment-rate-limit', 429);
        this.query('INSERT INTO enrollment_limits VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET count=count+1', id, 1, (window + 1) * seconds);
      }
    });
  }

  async invite(ttl) {
    if (typeof ttl !== 'number' || !Number.isFinite(ttl) || ttl < 30 || ttl > 86400) throw new PoolError('invitation-ttl-out-of-range');
    const token = randomToken(32);
    const hashed = await digest(token);
    const expires = this.now() + ttl;
    this.transaction(() => {
      this.query('INSERT INTO invitations VALUES (?,?,0)', hashed, expires);
      this.audit('invitation-created', '');
    });
    return { invitation: token, expires_at: expires };
  }

  async enroll(invitation, name) {
    if (typeof invitation !== 'string' || invitation.length < 32 || invitation.length > 256) throw new PoolError('invalid-invitation', 401);
    if (typeof name !== 'string' || name.length < 1 || name.length > 80 || /[\x00-\x1f]/.test(name)) throw new PoolError('invalid-device-name');
    const invitationHash = await digest(invitation);
    const device = randomToken(16), token = randomToken(32), tokenHash = await digest(token);
    return this.transaction(() => {
      const row = this.one('SELECT * FROM invitations WHERE token_hash=? AND used=0 AND expires>?', invitationHash, this.now());
      if (!row) throw new PoolError('invalid-invitation', 401);
      this.query('UPDATE invitations SET used=1 WHERE token_hash=?', invitationHash);
      this.query('INSERT INTO devices(id,token_hash,name,last_seen) VALUES (?,?,?,?)', device, tokenHash, name, this.now());
      this.audit('device-enrolled', device);
      return { device_id: device, device_token: token };
    });
  }

  revoke(device) {
    if (typeof device !== 'string' || !this.one('SELECT id FROM devices WHERE id=?', device)) throw new PoolError('unknown-device', 404);
    this.transaction(() => {
      this.query('UPDATE devices SET revoked=1 WHERE id=?', device);
      this.audit('device-revoked', device);
    });
  }

  async listAccounts() {
    const accounts = [];
    for (const row of this.query('SELECT id,generation,state,verified,fetched,next_poll,error,usage,secret FROM accounts ORDER BY id')) {
      const credentials = await this.decrypt(row.secret);
      // Only cached quota and human-readable labels leave this admin endpoint.
      // Explicit fields keep grant material and provider IDs out of responses.
      accounts.push({
        id: row.id, generation: row.generation, state: row.state,
        verified: row.verified, fetched: row.fetched, next_poll: row.next_poll,
        expires_at: credentials.expires_at,
        error: row.error, usage: row.usage === null ? null : JSON.parse(row.usage),
        account: {
          emailAddress: credentials.account.emailAddress,
          subscriptionType: credentials.account.subscriptionType,
        },
      });
    }
    return accounts;
  }

  async addAccount(id, credentials, exclusiveOwner = false, replaceQuarantined = false) {
    if (exclusiveOwner !== true) throw new PoolError('exclusive-refresh-ownership-required');
    if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,64}(?![\s\S])/.test(id)) throw new PoolError('invalid-account-id');
    validateCredentials(credentials);
    const encrypted = await this.encrypt(credentials);
    const existing = this.one('SELECT * FROM accounts WHERE id=?', id);
    if (existing) {
      if (!replaceQuarantined || existing.state !== 'quarantined') throw new PoolError('account-already-exists', 409);
      const previous = await this.decrypt(existing.secret);
      if (previous.account.accountUuid !== credentials.account.accountUuid) throw new PoolError('replacement-account-identity-mismatch', 409);
      if (previous.refresh_token === credentials.refresh_token) throw new PoolError('replacement-requires-fresh-credential-lineage', 409);
      this.transaction(() => {
        this.query("UPDATE accounts SET secret=?,generation=generation+1,state='ready',verified=0,usage=NULL,fetched=NULL,next_poll=0,failures=0,error=NULL WHERE id=?", encrypted, id);
        this.audit('quarantined-account-replaced', id);
      });
      return;
    }
    if (replaceQuarantined) throw new PoolError('unknown-quarantined-account', 404);
    for (const row of this.query('SELECT secret FROM accounts')) {
      const previous = await this.decrypt(row.secret);
      if (previous.account.accountUuid === credentials.account.accountUuid) throw new PoolError('provider-account-already-exists', 409);
    }
    this.transaction(() => {
      this.query("INSERT INTO accounts(id,secret,generation,state) VALUES (?,?,1,'ready')", id, encrypted);
      this.audit('account-added', id);
    });
  }

  expireContributions() {
    this.query("UPDATE contributions SET state='expired',secret=NULL WHERE state='pending' AND expires<=?", this.now());
  }

  contribution(device, id) {
    if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{16,64}(?![\s\S])/.test(id)) throw new PoolError('unknown-contribution', 404);
    const row = this.one('SELECT * FROM contributions WHERE id=? AND device_id=?', id, device.id);
    if (!row) throw new PoolError('unknown-contribution', 404);
    return row;
  }

  receipt(row) {
    const result = { contribution_id: row.id, state: row.state, expires_at: row.expires };
    if (row.account_id !== null) result.account_id = row.account_id;
    return result;
  }

  finishContribution(row, state, accountId = null) {
    // Caller owns the synchronous transaction containing account + receipt.
    this.query('UPDATE contributions SET state=?,secret=NULL,account_id=? WHERE id=?', state, accountId, row.id);
    this.audit(`contribution-${state}`, row.id);
    return this.receipt(this.one('SELECT * FROM contributions WHERE id=?', row.id));
  }

  async contributionStart(device) {
    this.expireContributions();
    let row = this.one("SELECT * FROM contributions WHERE device_id=? AND state='pending'", device.id);
    let authorizationUrl;
    if (!row) {
      const now = this.now();
      const counts = this.one('SELECT sum(created>?) hourly,count(*) daily FROM contributions WHERE device_id=? AND created>?', now - 3600, device.id, now - 86400);
      if ((counts.hourly || 0) >= 5 || counts.daily >= 20) throw new PoolError('contribution-start-limit', 429);
      const id = randomToken(24);
      const secret = { verifier: randomToken(48), state: randomToken(32) };
      const encrypted = await this.encrypt(secret);
      authorizationUrl = await this.provider.authorizationUrl(secret.verifier, secret.state);
      this.transaction(() => {
        this.query("INSERT INTO contributions(id,device_id,created,expires,state,secret) VALUES (?,?,?,?,'pending',?)", id, device.id, now, now + CONTRIBUTION_TTL, encrypted);
        this.audit('contribution-started', id);
      });
      row = this.contribution(device, id);
    } else {
      const secret = await this.decrypt(row.secret);
      authorizationUrl = await this.provider.authorizationUrl(secret.verifier, secret.state);
    }
    return { ...this.receipt(row), authorization_url: authorizationUrl };
  }

  contributionStatus(device, id) {
    this.expireContributions();
    return this.receipt(this.contribution(device, id));
  }

  contributionCancel(device, id) {
    this.expireContributions();
    const row = this.contribution(device, id);
    return row.state === 'pending' ? this.transaction(() => this.finishContribution(row, 'cancelled')) : this.receipt(row);
  }

  async contributionComplete(device, id, code) {
    this.expireContributions();
    const row = this.contribution(device, id);
    if (row.state !== 'pending') return this.receipt(row);
    const secret = await this.decrypt(row.secret);
    const match = typeof code === 'string' && code.length <= 4225 ? code.match(/^([\x21-\x22\x24-\x7e]{1,4096})#([A-Za-z0-9_-]{32,128})$/) : null;
    if (!match || match[0] !== code || !(await secretEqual(match[2], secret.state))) {
      const result = this.transaction(() => {
        const attempts = row.invalid_attempts + 1;
        this.query('UPDATE contributions SET invalid_attempts=? WHERE id=?', attempts, row.id);
        return attempts >= 3 ? this.finishContribution(row, 'failed') : null;
      });
      if (result) return result;
      throw new PoolError('invalid-authorization-code');
    }
    this.transaction(() => {
      this.query("UPDATE contributions SET state='exchanging' WHERE id=?", row.id);
      this.audit('contribution-exchanging', row.id);
    });
    // Explicitly establish durable ownership before sending a one-use code.
    await this.ctx.storage.sync();
    try {
      const credentials = validateCredentials(await this.provider.exchangeCode(match[1], secret.verifier, secret.state));
      if (credentials.expires_at <= this.now() + EXPIRY_MARGIN || !['pro', 'max', 'team', 'enterprise'].includes(credentials.account.subscriptionType)) {
        throw new PoolError('ineligible-subscription');
      }
      for (const account of this.query('SELECT id,secret FROM accounts')) {
        const previous = await this.decrypt(account.secret);
        if (previous.account.accountUuid === credentials.account.accountUuid) {
          return this.transaction(() => this.finishContribution(row, 'duplicate', account.id));
        }
      }
      const accountId = `contrib-${Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')}`;
      const encrypted = await this.encrypt(credentials);
      return this.transaction(() => {
        this.query("INSERT INTO accounts(id,secret,generation,state,verified) VALUES (?,?,1,'ready',1)", accountId, encrypted);
        this.audit('account-added', accountId);
        return this.finishContribution(row, 'added', accountId);
      });
    } catch {
      // A failed response may follow successful consumption. Never replay it.
      return this.transaction(() => this.finishContribution(row, 'uncertain'));
    }
  }

  quarantine(id, error) {
    this.transaction(() => {
      this.query("UPDATE accounts SET state='quarantined',error=? WHERE id=?", error, id);
      this.audit('account-quarantined', id);
    });
  }

  async poll() {
    const rows = this.query("SELECT * FROM accounts WHERE state='ready' ORDER BY id");
    for (let row of rows) {
      const now = this.now();
      let credentials = await this.decrypt(row.secret);
      if (credentials.expires_at <= now + REFRESH_MARGIN) {
        this.transaction(() => {
          this.query("UPDATE accounts SET state='refreshing' WHERE id=?", row.id);
          this.audit('refresh-started', row.id);
        });
        await this.ctx.storage.sync();
        try {
          const successor = validateCredentials(await this.provider.refresh(credentials));
          const encrypted = await this.encrypt(successor);
          this.transaction(() => {
            this.query("UPDATE accounts SET secret=?,generation=generation+1,state='ready',verified=0,next_poll=0 WHERE id=?", encrypted, row.id);
            this.audit('refresh-completed', row.id);
          });
          await this.ctx.storage.sync();
          credentials = successor;
          row = this.one('SELECT * FROM accounts WHERE id=?', row.id);
        } catch {
          this.quarantine(row.id, 'refresh-uncertain-reimport-fresh-lineage');
          continue;
        }
      }
      if (now < row.next_poll) continue;
      try {
        if (!row.verified) {
          const identity = await this.provider.identity(credentials.access_token);
          if (['accountUuid', 'organizationUuid'].some(key => identity[key] !== credentials.account[key])) {
            this.quarantine(row.id, 'identity-mismatch');
            continue;
          }
          if (identity.emailAddress) credentials.account.emailAddress = identity.emailAddress;
          const encrypted = await this.encrypt(credentials);
          this.query('UPDATE accounts SET verified=1,secret=? WHERE id=?', encrypted, row.id);
        }
        const usage = await this.provider.usage(credentials.access_token);
        if (!object(usage) || !Object.hasOwn(usage, 'five_hour') || !Object.hasOwn(usage, 'seven_day')
            || Object.values(usage).some(value => typeof value !== 'number' || !Number.isFinite(value) || value < 0)) throw new ProviderError('bad-usage');
        this.query('UPDATE accounts SET usage=?,fetched=?,next_poll=?,failures=0,error=NULL WHERE id=?', JSON.stringify(usage), this.now(), this.now() + POLL_SECONDS, row.id);
      } catch (error) {
        let code = error instanceof ProviderError ? error.code : 'provider-error';
        if (!['http-429', 'http-401', 'http-403', 'bad-usage', 'bad-profile', 'transport-or-response-error'].includes(code)) code = 'provider-error';
        if (['http-401', 'http-403'].includes(code)) {
          this.quarantine(row.id, code);
          continue;
        }
        const failures = row.failures + 1;
        const delay = Math.min(3600, (code === 'http-429' ? 600 : 120) * 2 ** Math.min(failures - 1, 5));
        const retry = error instanceof ProviderError ? (error.retry_after ?? error.retryAfter ?? 0) : 0;
        this.query('UPDATE accounts SET next_poll=?,failures=?,error=? WHERE id=?', this.now() + Math.max(delay, Number.isFinite(retry) ? Math.min(86400, Math.max(0, retry)) : 0), failures, code, row.id);
      }
    }
  }

  async assignment(device) {
    await this.poll();
    const now = this.now();
    const rows = this.query("SELECT * FROM accounts WHERE state='ready' AND verified=1");
    const loads = new Map(this.query('SELECT account_id,count(*) n FROM devices WHERE revoked=0 AND last_seen>? GROUP BY account_id', now - DEVICE_ACTIVE_SECONDS).map(row => [row.account_id, row.n]));
    const usable = new Map(), eligible = [];
    for (const row of rows) {
      const credentials = await this.decrypt(row.secret);
      if (credentials.expires_at <= now + EXPIRY_MARGIN) continue;
      usable.set(row.id, { row, credentials });
      if (row.fetched !== null && now - row.fetched >= 0 && now - row.fetched <= FRESH_SECONDS) {
        const usage = JSON.parse(row.usage);
        if (Object.values(usage).every(value => value < this.threshold)) eligible.push([loads.get(row.id) || 0, Math.max(...Object.values(usage)), row.id]);
      }
    }
    const current = device.account_id;
    let chosen, state;
    if (eligible.some(candidate => candidate[2] === current)) {
      chosen = current;
      state = 'fresh';
    } else if (eligible.length) {
      eligible.sort((a, b) => a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0));
      chosen = eligible[0][2];
      state = 'fresh';
    } else if (usable.has(current)) {
      chosen = current;
      const row = usable.get(current).row;
      state = row.fetched === null || now - row.fetched > FRESH_SECONDS ? 'quota_stale' : 'quota_exhausted';
    } else {
      throw new PoolError('no-eligible-account', 503);
    }
    const { row, credentials } = usable.get(chosen);
    this.transaction(() => {
      this.query('UPDATE devices SET account_id=?,last_seen=? WHERE id=?', chosen, now, device.id);
      if (current !== chosen) this.audit('assignment-changed', device.id);
    });
    return {
      account_id: chosen, generation: row.generation, access_token: credentials.access_token,
      expires_at: credentials.expires_at, scopes: credentials.scopes,
      account: Object.fromEntries(METADATA.map(key => [key, credentials.account[key]])),
      renew_after: Math.min(60, Math.max(1, credentials.expires_at - now - EXPIRY_MARGIN)), state,
    };
  }
}
