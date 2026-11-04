export { PoolObject } from './pool.mjs';

function reply(status, error) {
  return Response.json({ error }, {
    status, headers: { 'Cache-Control': 'no-store' },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.protocol !== 'https:') return reply(400, 'https-required');
    if (url.search) return reply(400, 'query-not-supported');
    if (!env.POOL_MASTER_KEY || !env.POOL_ADMIN_TOKEN) {
      return reply(503, 'coordinator-not-configured');
    }
    if (['/v1/enroll', '/v1/enroll/share'].includes(url.pathname)) {
      // Keep unauthenticated invitation guessing outside the singleton queue.
      const limited = await env.ENROLL_LIMIT.limit({
        key: request.headers.get('CF-Connecting-IP') || 'unknown',
      });
      if (!limited.success) return reply(429, 'enrollment-rate-limit');
    }
    // This name is persistent identity: changing it creates a different pool.
    const id = env.POOL.idFromName('team-pool-v1');
    try {
      return await env.POOL.get(id).fetch(request);
    } catch {
      return reply(503, 'coordinator-unavailable');
    }
  },
};
