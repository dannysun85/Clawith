import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const api = read('src/services/api.ts');
const employees = read('src/pages/Employees.tsx');
const okrPage = read('src/pages/OKR.tsx');
const companyAdmin = read('src/pages/CompanyAdmin.tsx');
const app = read('src/App.tsx');
const agentDetail = read('src/pages/agent-detail/AgentDetailPage.tsx');
const briefPanel = read('src/pages/agent-detail/CeoBriefPanel.tsx');
const ceoCard = read('src/pages/enterprise-settings/tabs/CeoCard.tsx');
const okrTab = read('src/pages/enterprise-settings/tabs/OkrTab.tsx');
const en = read('src/i18n/en.json');
const zh = read('src/i18n/zh.json');

test('ceoApi exposes the CEO orchestrator endpoint family', () => {
  for (const route of [
    '/companies/current/ceo/status',
    '/companies/current/ceo/settings',
    '/companies/current/ceo/enable',
    '/companies/current/ceo/disable',
    '/agents/${agentId}/company-brief',
    '/agents/${agentId}/meetings/${kind}/start',
  ]) {
    assert.ok(api.includes(route), `api.ts missing route ${route}`);
  }
});

test('roster renders the system-role badge from is_system', () => {
  assert.match(employees, /node\.is_system/);
  assert.match(employees, /employee-directory__system-badge/);
  assert.match(employees, /系统岗位/);
});

test('CEO entry card is governor- and rollout-gated', () => {
  assert.match(employees, /canGovernAgents && ceoSettings\?\.feature_available/);
  assert.match(employees, /data-testid="ceo-orchestrator-entry"/);
});

test('agent detail page mounts the CEO brief panel and system badge', () => {
  assert.match(agentDetail, /import CeoBriefPanel from '\.\/CeoBriefPanel'/);
  assert.match(agentDetail, /<CeoBriefPanel agentId=\{id\} \/>/);
  assert.match(agentDetail, /data-testid="agent-system-role"/);
});

test('CEO brief panel is invisible unless this Agent is the enabled CEO', () => {
  assert.match(briefPanel, /ceoApi\.status\(\)/);
  assert.match(briefPanel, /ceoStatus\?\.feature_available/);
  assert.match(briefPanel, /ceoStatus\.configured/);
  assert.match(briefPanel, /ceoStatus\.enabled/);
  assert.match(briefPanel, /ceoStatus\.ceo_agent_id === agentId/);
  assert.doesNotMatch(briefPanel, /ceoApi\.settings\(\)/);
  assert.doesNotMatch(briefPanel, /meeting_group_id/);
  assert.match(briefPanel, /ceoApi\.companyBrief/);
  assert.match(briefPanel, /ceoApi\.startMeeting/);
  assert.match(briefPanel, /ceoStatus\.can_start_meeting && <button/);
});

test('CEO settings card keeps only the read-only migration preview when the rollout gate is closed', () => {
  assert.match(ceoCard, /if \(!settings\.feature_available\)/);
  assert.match(ceoCard, /<MigrationPreviewPanel preview=\{migrationPreview\}/);
  assert.match(ceoCard, /cannot be enabled or created/);
  assert.match(ceoCard, /ceoApi\.enable/);
  assert.match(ceoCard, /ceoApi\.updateSettings/);
  assert.match(ceoCard, /daily_credit_cap/);
  assert.match(ceoCard, /meeting_member_agent_ids/);
});

test('CEO card is mounted on the company OKR settings tab as an independent card', () => {
  assert.match(okrTab, /import CeoCard from '\.\/CeoCard'/);
  assert.match(okrTab, /<CeoCard tenantId=\{tenantId\} \/>/);
});

test('CEO and OKR entry points reach the company-settings subview without adding a nav item', () => {
  assert.match(employees, /navigate\('\/company-admin\/settings\/okr'\)/);
  assert.match(okrPage, /navigate\('\/company-admin\/settings\/okr'\)/);
  assert.match(companyAdmin, /settingsView === 'okr'/);
  assert.match(companyAdmin, /<EnterpriseSettings initialTab="okr" embedded \/>/);
  assert.match(app, /okr: 'settings\/okr'/);
});

test('system-role badge has en/zh i18n entries', () => {
  assert.match(en, /"systemRole": "System role"/);
  assert.match(zh, /"systemRole": "系统岗位"/);
});
