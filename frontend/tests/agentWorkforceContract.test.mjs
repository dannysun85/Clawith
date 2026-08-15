import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const market = readFileSync(new URL('../src/components/TalentMarketModal.tsx', import.meta.url), 'utf8');
const postHire = readFileSync(new URL('../src/components/PostHireSettingsModal.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const agentDetail = readFileSync(new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url), 'utf8');

test('talent market exposes only server-returned roles with a visible capability contract', () => {
  assert.match(market, /agentApi\.templates\(\)/);
  assert.match(market, /capability_contract\?\.contract_ready/);
  assert.match(market, /能力合同已注册/);
  assert.match(market, /role_revision/);
});

test('post-hire review shows deliverables and limits before creating an Agent', () => {
  assert.match(postHire, /岗位能力合同/);
  assert.match(postHire, /template\.deliverables\.slice\(0, 3\)/);
  assert.match(postHire, /template\.limitations\.slice\(0, 2\)/);
  assert.match(postHire, /template\.source_provenance\.repository/);
  assert.match(postHire, /source_provenance\.commit/);
  assert.match(postHire, /template_id: template\.id/);
  assert.match(postHire, /canCreateCompanyWide/);
  assert.match(postHire, /permission_access_level: 'use'/);
  assert.match(postHire, /localizedTemplateName/);
  assert.doesNotMatch(postHire, /TierSelector/);
  assert.doesNotMatch(postHire, /preferred_tier:/);
});

test('frontend has a runtime readiness route for an existing Agent', () => {
  assert.match(api, /capabilityReadiness: \(id: string\)/);
  assert.match(api, /\/agents\/\$\{id\}\/capability-readiness/);
});

test('an open Agent settings page continuously revalidates manage authority', () => {
  const activeSettingsRefetches = agentDetail.match(
    /refetchInterval: activeTab === 'settings' \? 3000 : false/g,
  ) ?? [];
  assert.equal(activeSettingsRefetches.length, 2);
  assert.match(agentDetail, /isError: agentAccessError/);
  assert.match(agentDetail, /retry: activeTab === 'settings' \? false : 3/);
  assert.match(agentDetail, /queryKey: \['agent-permissions', id\]/);
  assert.match(agentDetail, /isError: permissionAccessError/);
  assert.match(agentDetail, /permissionAccessError=\{permissionAccessError\}/);
  assert.match(
    agentDetail,
    /const canManagePermissions = canManage\s+&& !permissionAccessError\s+&& \(permData\?\.can_manage \?\? canManage\)/,
  );
  assert.match(agentDetail, /activeTab === 'settings' && agentAccessError/);
  assert.match(agentDetail, /Agent 访问权限已失效，请返回数字员工中心/);
});
