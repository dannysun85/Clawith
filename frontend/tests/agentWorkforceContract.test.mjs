import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const market = readFileSync(new URL('../src/components/TalentMarketModal.tsx', import.meta.url), 'utf8');
const postHire = readFileSync(new URL('../src/components/PostHireSettingsModal.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');

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
});

test('frontend has a runtime readiness route for an existing Agent', () => {
  assert.match(api, /capabilityReadiness: \(id: string\)/);
  assert.match(api, /\/agents\/\$\{id\}\/capability-readiness/);
});
