import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const employees = readFileSync(new URL('../src/pages/Employees.tsx', import.meta.url), 'utf8');
const dashboard = readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const topology = readFileSync(
  new URL('../src/components/WorkforceTopology/WorkforceTopology.tsx', import.meta.url),
  'utf8',
);

test('employee topology keeps health separate from authoritative execution state', () => {
  assert.match(api, /execution\?: WorkforceTopologyExecution \| null/);
  assert.match(api, /waiting_user/);
  assert.match(topology, /topologyExecutionWorkGroup\(node\)/);
  assert.match(topology, /executionStatusLabel\(execution\.status/);
  assert.match(topology, /executionSourceLabel\(execution\.source_type/);
});

test('employee and dashboard projections recover after navigation, focus and reconnect', () => {
  assert.match(employees, /document\.visibilityState === 'hidden'/);
  assert.match(employees, /: 15_000/);
  for (const source of [employees, dashboard]) {
    assert.match(source, /refetchIntervalInBackground: false/);
    assert.match(source, /refetchOnMount: 'always'/);
    assert.match(source, /refetchOnWindowFocus: 'always'/);
    assert.match(source, /refetchOnReconnect: 'always'/);
  }
});

test('workforce topology declares and explains its three visibility scopes', () => {
  assert.match(api, /execution: 'company_visible_redacted'/);
  assert.match(api, /work: 'viewer_owned'/);
  assert.match(api, /analytics: 'governor_or_managed'/);
  assert.match(employees, /data-testid="workforce-scope-contract"/);
  assert.match(employees, /执行状态：公司可见，敏感详情按权限脱敏/);
  assert.match(employees, /当前工作：仅显示你拥有或可见的工作/);
  assert.match(employees, /关系与活动：仅管理员或受托管理范围/);
  assert.match(employees, /scope === 'governance'/);
  assert.match(employees, /node\.visibility !== 'private'/);
});
