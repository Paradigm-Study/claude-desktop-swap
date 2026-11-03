import test from 'node:test';
import assert from 'node:assert/strict';
import { Provider, USAGE_URL, usageDiagnostic } from '../src/provider.mjs';

test('usage diagnostics expose structural failures and numeric quota without unrelated data', () => {
  const raw = { access_token: 'DO-NOT-RETURN', account: { email: 'private@example.test' },
    five_hour: null, seven_day: { utilization: 100, resets_at: 'private-string' },
    seven_day_sonnet: { utilization: 'DO-NOT-RETURN', rows: [] },
    limits: [{ kind: 'weekly_scoped', group: 'weekly', percent: null, is_active: false,
      scope: { model: { display_name: 'Private model label' } }, secret: 'DO-NOT-RETURN' }] };
  const result = usageDiagnostic(raw);
  assert.equal(result.windows.five_hour.type, 'null');
  assert.deepEqual(result.windows.seven_day.utilization, { type: 'number', value: 100 });
  assert.deepEqual(result.windows.seven_day_sonnet.utilization, { type: 'string' });
  assert.deepEqual(result.limits[0].percent, { type: 'null' });
  assert.equal(result.limits[0].is_active, false);
  assert.equal(result.limits[0].display_name_valid, true);
  for (const secret of ['DO-NOT-RETURN', 'private@example.test', 'private-string', 'Private model label']) {
    assert.ok(!JSON.stringify(result).includes(secret));
  }
});

test('usage diagnostics are bounded and tolerate malformed structures', () => {
  const result = usageDiagnostic({ five_hour: 7, limits: Array(1000).fill(null),
    ...Object.fromEntries(Array.from({ length: 100 }, (_, n) => [`seven_day_${n}`, []])) });
  assert.equal(Object.keys(result.windows).length, 32);
  assert.equal(result.limits.length, 50);
  assert.equal(result.limits_count, 1000);
  assert.deepEqual(result.limits[0], { type: 'null' });
  assert.equal(usageDiagnostic({ limits: 'private' }).limits_type, 'string');
});

test('usage diagnostic transport requests only usage and never refreshes credentials', async () => {
  const calls = [];
  const provider = new Provider({ fetcher: async (url, options) => {
    calls.push({ url, options });
    return Response.json({ five_hour: { utilization: 0 }, seven_day: { utilization: 100 } });
  } });
  const result = await provider.usageDiagnostic('synthetic-token');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, USAGE_URL);
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(result.windows.seven_day.utilization.value, 100);
  assert.ok(!JSON.stringify(result).includes('synthetic-token'));
});
