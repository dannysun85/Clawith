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
const appStyles = readFileSync(
  new URL('../src/index.css', import.meta.url),
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

test('degraded media controls expose a visible business-level warning without provider details', () => {
  assert.match(agentDetail, /media-capability-launcher\$\{capability\.capability_status === 'degraded'/);
  assert.match(agentDetail, /data-capability-state=\{state\.disabled \? 'unavailable' : \(capability\.capability_status \|\| 'available'\)\}/);
  assert.match(appStyles, /\.chat-composer-btn\.media-capability-launcher\.is-degraded/);
  assert.doesNotMatch(agentDetail, /available_providers.*media-capability-launcher/);
});

test('media admin exposes strategy routing, provider receipts, and non-equivalent quality evidence', () => {
  assert.match(saasAdminSource, /routing_mode: 'automatic_failover'/);
  assert.match(saasAdminSource, /provider_order: string\[\]/);
  assert.match(saasAdminSource, /available_providers: string\[\]/);
  assert.match(saasAdminSource, /capability_status: 'available' \| 'degraded' \| 'unavailable'/);
  assert.match(saasAdminSource, /视频优先消耗 MiniMax Plan 每账号每日 3 次额度/);
  assert.match(saasAdminSource, /额度耗尽后自动接续火山 Agent Plan/);
  assert.match(saasAdminSource, /只有供应商明确拒绝且尚未接受任务时才切换/);
  assert.match(saasAdminSource, /accepted\/unknown 均禁止重复提交/);
  assert.match(saasAdminSource, /Lite=Seedance 2\.0-mini\/480P、Pro=2\.0-fast\/720P、Ultra=标准 2\.0\/720P/);
  assert.match(saasAdminSource, /Ultra 只有请求明确指定 1080P 才升级/);
  assert.match(saasAdminSource, /实际 provider\/model 只以任务 receipt 为准/);
  assert.match(saasAdminSource, /MiniMax 日额度/);
  assert.match(saasAdminSource, /火山接续档位/);
  assert.match(saasAdminSource, /首选不可用，改用 \$\{providerLabel\(strategy\.alternate_provider\)\}\$\{strategy\.alternate_confirmation_required \? '需确认' : '自动接续'\}/);
  assert.match(saasAdminSource, /账号线路可路由/);
  assert.match(saasAdminSource, /仅降级线路可路由/);
  assert.match(saasAdminSource, /真实生成成功，质量未评审/);
  assert.match(saasAdminSource, /quality_evidence_status: 'not_reviewed'/);
});
