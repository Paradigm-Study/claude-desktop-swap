// This worker is only loaded by Miniflare tests. Production exports no SQL or
// clock control routes: the fixture inspects the actual Durable Object store.
import { DurableObject } from 'cloudflare:workers';
import { PoolObject } from '../../src/pool.mjs';

export class FixturePool extends PoolObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.fixtureContext = ctx;
    this.fixtureTime = Number(env.FIXTURE_TIME);
  }

  now() {
    return this.fixtureTime ?? Date.now() / 1000;
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path.startsWith('/__test/')) {
      const body = await request.json();
      if (path === '/__test/sql') {
        const rows = [...this.fixtureContext.storage.sql.exec(body.sql, ...(body.args ?? []))];
        await this.fixtureContext.storage.sync();
        return Response.json({ rows });
      }
      if (path === '/__test/clock') {
        this.fixtureTime = body.now;
        return Response.json({ now: this.now() });
      }
      if (path === '/__test/fail-receipt') {
        this.failReceiptOnce = true;
        return Response.json({ ok: true });
      }
      return new Response('Unknown fixture route', { status: 404 });
    }
    return super.fetch(request);
  }

  finishContribution(row, state, accountId) {
    if (state === 'added' && this.failReceiptOnce) {
      this.failReceiptOnce = false;
      throw new Error('Fixture receipt persistence failure');
    }
    return super.finishContribution(row, state, accountId);
  }
}

export class FixtureDirectory extends DurableObject {
  async fetch(request) {
    const path = new URL(request.url).pathname;
    const body = await request.json();
    if (path === '/__test/share/grant') {
      await this.ctx.storage.put(`grant:${body.grant}`, body);
      await this.ctx.storage.put(`member:${body.account_id}:${body.team_id}`, true);
      return Response.json({ ok: true });
    }
    if (path === '/__test/share/member') {
      await this.ctx.storage.put(`member:${body.account_id}:${body.team_id}`, body.member);
      return Response.json({ ok: true });
    }
    if (path === '/__test/share/outage') {
      await this.ctx.storage.put('outage', body.outage);
      return Response.json({ ok: true });
    }
    if (path === '/__test/share/stats') return Response.json({ calls: await this.ctx.storage.get('calls') ?? 0 });
    const outage = await this.ctx.storage.get('outage');
    if (outage === 'timeout') return new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('{')); } }));
    if (outage === 'config-mismatch') return Response.json({ result: { ok: false, status: 503, error: 'native-pool-config-mismatch' } });
    if (outage === 'malformed') return Response.json({ result: { ok: 'false' } });
    if (outage) return Response.json({ result: { ok: false, status: 503, error: 'fixture-unavailable' } });
    if (path === '/rpc/nativePoolMembership') {
      const [account, team, server] = body;
      return Response.json({ result: { ok: server === 'https://pool.example.test' && await this.ctx.storage.get(`member:${account}:${team}`) === true } });
    }
    if (path === '/rpc/consumeNativePoolGrant') {
      await this.ctx.storage.put('calls', (await this.ctx.storage.get('calls') ?? 0) + 1);
      const [value, verifier, server] = body;
      const grant = await this.ctx.storage.get(`grant:${value}`);
      const challenge = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))), byte => byte.toString(16).padStart(2, '0')).join('');
      if (!grant || grant.challenge !== challenge || server !== 'https://pool.example.test'
          || await this.ctx.storage.get(`member:${grant.account_id}:${grant.team_id}`) !== true) {
        return Response.json({ result: { ok: false, status: 401, error: 'invalid-grant' } });
      }
      // Optional replay models a broken/retried bridge; the pool must still
      // enforce its own durable grant consumption across worker restarts.
      if (!grant.replay) await this.ctx.storage.delete(`grant:${value}`);
      return Response.json({ result: { ok: true, account_id: grant.account_id, team_id: grant.team_id,
        device_name: grant.device_name, expires_at: grant.expires_at } });
    }
    return new Response('Unknown fixture directory route', { status: 404 });
  }
}

export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname.startsWith('/__test/share/')) {
      return env.SHARE_DIRECTORY.get(env.SHARE_DIRECTORY.idFromName('directory')).fetch(request);
    }
    const id = env.POOL.idFromName('team-pool-v1');
    return env.POOL.get(id).fetch(request);
  },
};
