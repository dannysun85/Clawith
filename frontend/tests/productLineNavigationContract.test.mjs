import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const layout = readFileSync(new URL('../src/pages/Layout.tsx', import.meta.url), 'utf8');
const onboarding = readFileSync(new URL('../src/pages/Onboarding.tsx', import.meta.url), 'utf8');
const work = readFileSync(new URL('../src/pages/Work.tsx', import.meta.url), 'utf8');
const plaza = readFileSync(new URL('../src/pages/Plaza.tsx', import.meta.url), 'utf8');
const editor = readFileSync(new URL('../src/components/ExperienceDraftEditor.tsx', import.meta.url), 'utf8');

test('the task workbench is the default entry while legacy routes remain available', () => {
  assert.match(app, /<Navigate to="\/work" replace/);
  assert.match(app, /path="work"/);
  assert.match(app, /path="dashboard"/);
  assert.match(app, /path="plaza"/);
  assert.match(onboarding, /navigate\('\/work'/);
  assert.match(onboarding, /createAssistant\(true\)/);
  assert.match(onboarding, /Skip for now, use defaults/);
});

test('navigation names communicate distinct product responsibilities', () => {
  assert.match(layout, /'工作台'/);
  assert.match(layout, /'公司概览'/);
  assert.match(layout, /'发现中心'/);
  assert.match(layout, /'协作群组'/);
  assert.match(layout, /'我的助理'/);
  assert.match(layout, /'Agent 员工'/);
});

test('company administration routes stay behind the company-admin boundary', () => {
  assert.match(
    app,
    /path="enterprise" element={<TenantWorkspaceRoute><CompanyAdminRoute><EnterpriseSettings \/><\/CompanyAdminRoute><\/TenantWorkspaceRoute>}/,
  );
  assert.match(
    app,
    /path="invitations" element={<TenantWorkspaceRoute><CompanyAdminRoute><InvitationCodes \/><\/CompanyAdminRoute><\/TenantWorkspaceRoute>}/,
  );
});

test('ordinary users choose business executors without provider or model controls', () => {
  assert.match(work, /executorKind/);
  assert.match(work, /personal_assistant/);
  assert.match(work, /agent_employee/);
  assert.match(work, /temporary_expert/);
  assert.doesNotMatch(work, /name="provider"|name="model"|setProvider|setModel/);
  assert.match(work, /delivery_mode === 'formal_deliverable'/);
  assert.match(work, /本任务只整理 brief/);
  assert.doesNotMatch(work, /zh: '商业图片'|zh: '人物视频'|zh: '精美 PPT'/);
});

test('discover separates experience from hiring and preserves work provenance', () => {
  assert.match(plaza, /'experience' \| 'talent'/);
  assert.match(plaza, /openTalentMarket/);
  assert.match(plaza, /source_task_id: params\.get\('task'\)/);
  assert.match(plaza, /source_deliverable_request_id: params\.get\('delivery'\)/);
  assert.match(editor, /source_task_id: form\.source_task_id/);
  assert.match(editor, /source_deliverable_request_id: form\.source_deliverable_request_id/);
  assert.match(work, /item\.user_stage === 'delivery'/);
});
