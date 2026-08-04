import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/pages/AdminCompanies.tsx', import.meta.url),
  'utf8',
);

test('platform console exposes a tenant-scoped workspace switch for dual-role identities', () => {
  assert.match(source, /authApi\.getMyTenants\(\)/);
  assert.match(source, /authApi\.switchTenant\(tenantId\)/);
  assert.match(source, /result\.target_tenant_id !== tenantId/);
  assert.match(source, /validateTenantSwitchCandidate\(\{/);
  assert.match(source, /commitSameOriginTenantSwitch\(\{/);
  assert.match(source, /validateToken: authApi\.me/);
  assert.match(source, /resolvedTenantId: \(user\) => user\.tenant_id/);
  assert.match(source, /navigate\('\/work', \{ replace: true \}\)/);
  assert.match(source, /进入公司工作区/);
});
