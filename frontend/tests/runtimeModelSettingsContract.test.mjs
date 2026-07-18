import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const saasAdminSource = readFileSync(
  new URL('../src/pages/SaasAdmin.tsx', import.meta.url),
  'utf8',
);
const enterpriseSettingsSource = readFileSync(
  new URL('../src/pages/EnterpriseSettings.tsx', import.meta.url),
  'utf8',
);

test('the centralized SaaS owner can select planning and group context models per tenant', () => {
  assert.match(saasAdminSource, /\/enterprise\/runtime-model-settings\?tenant_id=/);
  assert.match(saasAdminSource, /planning_model_id/);
  assert.match(saasAdminSource, /compact_model_id/);
  assert.match(saasAdminSource, /\/saas\/tenants/);
  assert.match(saasAdminSource, /群聊规划模型/);
  assert.match(saasAdminSource, /群聊上下文模型/);
  assert.match(saasAdminSource, /模型对象或密钥/);
});

test('runtime model choices use tenant-safe backend candidates without restoring tenant model CRUD', () => {
  assert.match(saasAdminSource, /settingsQuery\.data\?\.candidates/);
  assert.match(saasAdminSource, /候选模型必须已启用并通过原生工具调用测试/);
  assert.match(saasAdminSource, /运行时模型配置已更新并立即生效/);
  assert.match(enterpriseSettingsSource, /navigate\(`\/admin\/saas\?tab=\$\{activeTab === 'llm' \? 'model-routes' : 'plans'\}`/);
});
