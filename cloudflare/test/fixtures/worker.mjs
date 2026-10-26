// This worker is only loaded by Miniflare tests. Production exports no SQL or
// clock control routes: the fixture inspects the actual Durable Object store.
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

export default {
  async fetch(request, env) {
    const id = env.POOL.idFromName('team-pool-v1');
    return env.POOL.get(id).fetch(request);
  },
};
