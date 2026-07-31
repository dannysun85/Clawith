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
const modelSwitcher = readFileSync(
  new URL('../src/components/ModelSwitcher.tsx', import.meta.url),
  'utf8',
);
const modelCacheEvents = readFileSync(
  new URL('../src/services/modelCacheEvents.ts', import.meta.url),
  'utf8',
);
const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);
const source = readFileSync(
  new URL('../src/pages/enterprise-settings/tabs/LlmTab.tsx', import.meta.url),
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
  assert.match(saasAdminSource, /候选模型必须已启用并保留当前配置的连接验证 evidence/);
  assert.match(saasAdminSource, /原生工具调用是否支持属于独立诊断/);
  assert.match(saasAdminSource, /运行时模型配置已更新并立即生效/);
  assert.match(enterpriseSettingsSource, /navigate\(`\/admin\/saas\?tab=\$\{activeTab === 'llm' \? 'model-routes' : 'plans'\}`/);
});

test('stale runtime model ids stay unselected instead of selecting the first option', () => {
  assert.match(source, /planning_source: 'database' \| 'environment' \| 'unavailable'/);
  assert.match(source, /planning_model_id: runtimeModelSettings\.planning_model_id \|\| ''/);
  assert.match(source, /compact_model_id: runtimeModelSettings\.compact_model_id \|\| ''/);
  assert.match(source, /<option value="" disabled>/);
});

test('chat model choices allow every enabled model and refresh across tabs', () => {
  assert.match(
    modelSwitcher,
    /filter\(m => m\.enabled !== false\)/,
  );
  assert.match(modelSwitcher, /subscribeModelCacheInvalidation/);
  assert.match(modelSwitcher, /void refetchModels\(\)/);
  assert.match(source, /notifyModelCacheInvalidated\(\)/);
  assert.match(modelCacheEvents, /window\.addEventListener\('storage'/);
  assert.match(modelCacheEvents, /window\.dispatchEvent\(new Event\(MODEL_CACHE_EVENT\)\)/);
});

test('chat routes through enabled SaaS tiers instead of stale direct model ids', () => {
  assert.match(agentDetail, /const effectiveChatTier = resolveChatSessionTier\(/);
  assert.match(agentDetail, /const effectiveTierReady = !!effectiveChatTier/);
  assert.match(agentDetail, /queryKey: \['agent-media-capabilities', id, effectiveChatTier\]/);
  assert.doesNotMatch(agentDetail, /const effectiveChatModelId = overrideModelId/);
});

test('media admin separates managed routing from non-equivalent emergency quality', () => {
  assert.match(saasAdminSource, /routing_mode: 'automatic_failover'/);
  assert.match(saasAdminSource, /provider_order: string\[\]/);
  assert.match(saasAdminSource, /available_providers: string\[\]/);
  assert.match(saasAdminSource, /capability_status: 'available' \| 'degraded' \| 'unavailable'/);
  assert.match(saasAdminSource, /图片、语音和视频按“火山 Agent Plan → MiniMax”/);
  assert.match(saasAdminSource, /音乐目前使用 MiniMax/);
  assert.match(saasAdminSource, /MiniMax 应急路径/);
  assert.match(saasAdminSource, /仅剩 MiniMax 时仍属于非等价降级/);
  assert.match(saasAdminSource, /账号线路可路由/);
  assert.match(saasAdminSource, /仅降级线路可路由/);
  assert.match(saasAdminSource, /真实生成成功，质量未评审/);
  assert.match(saasAdminSource, /商用质量已通过/);
});
