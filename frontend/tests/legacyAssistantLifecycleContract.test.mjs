import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const employees = readFileSync(new URL('../src/pages/Employees.tsx', import.meta.url), 'utf8');
const organizer = readFileSync(new URL('../src/components/LegacyAssistantOrganizer.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const productRoles = readFileSync(new URL('../src/utils/productRoles.ts', import.meta.url), 'utf8');

test('retained assistants have an explicit creator-owned lifecycle instead of a destructive shortcut', () => {
  assert.match(employees, /agent\.creator_id === user\?\.id/);
  assert.match(employees, /agent\.legacy_assistant_disposition != null/);
  assert.match(employees, /agentApi\.updateLegacyAssistantDisposition/);
  assert.match(employees, /expected_disposition: disposition/);
  assert.match(employees, /queryKey: \['agents'\]/);
  assert.match(employees, /queryKey: \['workforce-topology'\]/);
  assert.doesNotMatch(organizer, /agentApi\.delete|deleteAgent|删除/);
});

test('seat-limit failures use the typed API envelope and retain the upgrade action', () => {
  assert.match(employees, /nextError: unknown/);
  assert.match(employees, /error instanceof ApiError/);
  assert.match(employees, /\[error\.details, error\.detail\]/);
  assert.match(employees, /error\.status === 402 \? SUBSCRIPTION_UPGRADE_PATH/);
});

test('the compatibility organizer explains preservation, privacy, and seat consequences', () => {
  assert.match(organizer, /历史助理整理/);
  assert.match(organizer, /只有原创建者/);
  assert.match(organizer, /旧对话、文件、Workspace、Agent ID 和深链始终保留/);
  assert.match(organizer, /查看历史/);
  assert.match(organizer, /转为员工/);
  assert.match(organizer, /撤回为历史助理/);
  assert.match(organizer, /不占数字员工名额/);
  assert.match(organizer, /计入数字员工名额/);
});

test('client and sidebar projections honor the server-owned lifecycle state', () => {
  assert.match(api, /\/agents\/\$\{id\}\/legacy-assistant-disposition/);
  assert.match(api, /'archive' \| 'convert_to_employee' \| 'restore_history'/);
  assert.match(productRoles, /legacy_assistant_disposition !== 'archived'/);
});
