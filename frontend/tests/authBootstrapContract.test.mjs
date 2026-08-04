import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/App.tsx', import.meta.url),
  'utf8',
);

test('auth bootstrap validates a candidate before atomically committing browser identity', () => {
  assert.match(source, /let pendingCrossOriginSession:/);
  assert.match(source, /consumeTenantSwitchSessionFromUrl/);
  assert.match(source, /validateCrossOriginTenantSwitch\(\{/);
  assert.match(source, /resolveCurrentOriginTenant: \(\) => tenantApi\.resolveByDomain\(window\.location\.host\)/);
  assert.match(source, /await setAuth\(authenticatedUser, effectiveToken\)/);
  assert.doesNotMatch(source, /localStorage\.setItem\('token', urlToken\)/);
  assert.match(source, /priorToken !== effectiveToken/);
});
