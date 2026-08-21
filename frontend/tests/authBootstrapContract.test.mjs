import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/App.tsx', import.meta.url),
  'utf8',
);
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');

test('auth bootstrap validates a candidate before atomically committing browser identity', () => {
  assert.match(source, /let pendingCrossOriginSession:/);
  assert.match(source, /consumeTenantSwitchSessionFromUrl/);
  assert.match(source, /validateCrossOriginTenantSwitch\(\{/);
  assert.match(source, /resolveCurrentOriginTenant: \(\) => tenantApi\.resolveByDomain\(window\.location\.host\)/);
  assert.match(source, /await setAuth\(authenticatedUser, effectiveToken\)/);
  assert.doesNotMatch(source, /localStorage\.setItem\('token', urlToken\)/);
  assert.match(source, /priorToken !== effectiveToken/);
});

test('auth bootstrap fails closed without deleting a valid session during an outage', () => {
  assert.match(source, /withAuthBootstrapTimeout/);
  assert.match(source, /isTransientAuthBootstrapFailure/);
  assert.match(source, /setBootstrapUnavailable\(true\)/);
  assert.match(source, /登录状态和本地工作不会被清除/);
  assert.match(source, /重新检查连接/);
  assert.match(source, /setBootstrapAttempt\(\(value\) => value \+ 1\)/);
  assert.match(api, /me: \(accessToken\?: string, signal\?: AbortSignal\)/);
});
