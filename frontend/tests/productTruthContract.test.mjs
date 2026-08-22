import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const subscriptionTab = readFileSync(
  new URL('../src/pages/enterprise-settings/tabs/SubscriptionTab.tsx', import.meta.url),
  'utf8',
);
const okrTab = readFileSync(
  new URL('../src/pages/enterprise-settings/tabs/OkrTab.tsx', import.meta.url),
  'utf8',
);
const settingsTab = readFileSync(
  new URL('../src/pages/agent-detail/tabs/SettingsTab.tsx', import.meta.url),
  'utf8',
);
const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);
const dashboard = readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8');
const workbench = readFileSync(
  new URL('../src/components/deliverables/DeliverableWorkbench.tsx', import.meta.url),
  'utf8',
);
const releaseNotes = readFileSync(new URL('../../RELEASE_NOTES.md', import.meta.url), 'utf8');
const login = readFileSync(new URL('../src/pages/Login.tsx', import.meta.url), 'utf8');
const enterpriseSettings = readFileSync(new URL('../src/pages/EnterpriseSettings.tsx', import.meta.url), 'utf8');
const channelConfig = readFileSync(new URL('../src/components/ChannelConfig.tsx', import.meta.url), 'utf8');
const saasAdmin = readFileSync(new URL('../src/pages/SaasAdmin.tsx', import.meta.url), 'utf8');
const subscriptionDetail = readFileSync(
  new URL('../src/pages/SubscriptionDetail.tsx', import.meta.url),
  'utf8',
);
const ceoCard = readFileSync(
  new URL('../src/pages/enterprise-settings/tabs/CeoCard.tsx', import.meta.url),
  'utf8',
);
const ceoBriefPanel = readFileSync(
  new URL('../src/pages/agent-detail/CeoBriefPanel.tsx', import.meta.url),
  'utf8',
);
const mindTab = readFileSync(new URL('../src/pages/agent-detail/tabs/MindTab.tsx', import.meta.url), 'utf8');
const skillsTab = readFileSync(new URL('../src/pages/agent-detail/tabs/SkillsTab.tsx', import.meta.url), 'utf8');
const layout = readFileSync(new URL('../src/pages/Layout.tsx', import.meta.url), 'utf8');
const zh = JSON.parse(readFileSync(new URL('../src/i18n/zh.json', import.meta.url), 'utf8'));
const en = JSON.parse(readFileSync(new URL('../src/i18n/en.json', import.meta.url), 'utf8'));

test('every subscription translation key exists in both supported locales', () => {
  const keys = new Set(
    [...subscriptionTab.matchAll(/enterprise\.subscription\.([A-Za-z0-9]+)/g)]
      .map((match) => match[1]),
  );
  assert.ok(keys.size > 40);
  for (const key of keys) {
    assert.equal(typeof zh.enterprise.subscription[key], 'string', `missing zh subscription.${key}`);
    assert.equal(typeof en.enterprise.subscription[key], 'string', `missing en subscription.${key}`);
  }
});

test('subscription copy preserves yearly credit and period-end downgrade semantics', () => {
  assert.match(subscriptionTab, /creditsYearly/);
  assert.match(zh.enterprise.subscription.creditsYearly, /年.*一次发放/);
  assert.match(en.enterprise.subscription.creditsYearly, /year.*granted once/i);
  assert.match(zh.enterprise.subscription.marketDesc, /降级.*周期结束后/);
  assert.doesNotMatch(subscriptionTab, /price_cents\s*\*\s*10/);
  assert.doesNotMatch(subscriptionTab, /随时可切换/);
  assert.doesNotMatch(subscriptionTab, /首年 · 按年计费/);
});

test('tenant order history localizes every settled order status', () => {
  assert.match(subscriptionDetail, /refunded:\s*'已退款'/);
  assert.match(subscriptionDetail, /partially_refunded:\s*'部分退款'/);
});

test('disabled OKR and heartbeat automation remain visible but read-only', () => {
  assert.match(okrTab, /automation_available/);
  assert.match(okrTab, /disabled=\{!automationAvailable\}/);
  assert.match(okrTab, /平台当前未开放自动收集/);
  assert.match(settingsTab, /execution_capabilities\?\.heartbeat_execution === true/);
  assert.match(settingsTab, /disabled=\{!canManage \|\| !heartbeatAvailable\}/);
  assert.match(settingsTab, /platformDisabled/);
});

test('dashboard keeps an OKR entry point for disabled and empty states', () => {
  assert.match(dashboard, /OKR 尚未启用/);
  assert.match(dashboard, /本周期还没有 OKR/);
  assert.match(dashboard, /\/company-admin\/settings\/okr/);
});

test('report and spreadsheet are brief-only while formal delivery stays media-scoped', () => {
  assert.match(workbench, /isFormalDeliverableWorkType/);
  assert.match(workbench, /报告工作说明/);
  assert.match(workbench, /表格工作说明/);
  assert.match(workbench, /当前版本不提供正式生成/);
  assert.doesNotMatch(workbench, /report: isZh \? '报告交付任务'/);
  assert.doesNotMatch(workbench, /spreadsheet: isZh \? '表格交付任务'/);
});

test('legacy schedule UI state is absent from the Agent detail page', () => {
  assert.doesNotMatch(agentDetail, /scheduleApi/);
  assert.doesNotMatch(agentDetail, /schedToCron/);
  assert.doesNotMatch(agentDetail, /showScheduleForm/);
});

test('v1.12.0 release notes distinguish packaged code from released capabilities', () => {
  const section = releaseNotes.split('# v1.11.42')[0];
  assert.match(section, /Packaged code and database schema do\s+not make any of them a released customer feature/);
  assert.match(section, /CEO orchestration, creative v2 pipelines, Code\/AgentBay execution, Heartbeat/);
  assert.match(section, /all v2 canaries remain off/);
  assert.match(section, /read-only Company Settings panel/);
  assert.match(section, /managed three-reviewer quality gate also remains off/);
});

test('Chinese login and discovery navigation use the digital-employee product vocabulary', () => {
  const hero = zh.login.hero;
  assert.equal(hero.subtitle, '你的数字员工公司。');
  assert.equal(hero.founder, '公司创建者');
  assert.equal(hero.features.agentPlaza.title, '团队知识');
  assert.doesNotMatch(JSON.stringify(hero), /Agent Company|Founder|智能体/);
  assert.doesNotMatch(login, /欢迎，创始人/);
  assert.equal(zh.nav.plaza, '团队知识');
  assert.equal(en.nav.plaza, 'Team knowledge');
  assert.match(layout, /团队知识/);
});

test('Douyin UI treats direct publish as a separate server capability', () => {
  assert.match(enterpriseSettings, /direct_publish_available === true/);
  assert.match(enterpriseSettings, /directPublishUnavailable/);
  assert.match(zh.enterprise.douyin.directPublishUnavailable, /不会后台直发/);
  assert.doesNotMatch(zh.enterprise.douyin.publishWork, /^发布作品$/);
});

test('visible settings copy is localized instead of hard-coded English', () => {
  assert.doesNotMatch(enterpriseSettings, />Apply<|>Reset</);
  assert.match(enterpriseSettings, /common\.apply/);
  assert.match(channelConfig, /channelGuide\.wechat\.sessionExpired/);
  assert.doesNotMatch(channelConfig, /\? 'Session expired, reconnect required'/);
});

test('SaaS media routes render both supported languages', () => {
  assert.match(saasAdmin, /Media generation routes \(platform-managed\)/);
  assert.match(saasAdmin, /媒体生成路由（平台统一配置）/);
  assert.match(saasAdmin, /function MediaRouteRow\(\{ route, isZh \}/);
  assert.match(saasAdmin, /Account readiness/);
  assert.match(saasAdmin, /账号 readiness/);
});

test('CEO enablement requires explicit cadence intent and governed migration review', () => {
  assert.match(ceoCard, /observer_only_confirmed/);
  assert.match(ceoCard, /cadenceIntentReady/);
  assert.match(ceoCard, /meetingMembersReady/);
  assert.match(ceoCard, /migrationPreview/);
  assert.match(ceoCard, /历史 CEO 迁移预览（只读）/);
});

test('CEO panorama queries and meeting actions follow member-safe authority projection', () => {
  assert.match(ceoBriefPanel, /visible && Boolean\(ceoStatus\?\.can_read_brief\)/);
  assert.match(ceoBriefPanel, /ceoStatus\.can_start_meeting/);
  assert.match(ceoBriefPanel, /不会发起无权限的数据请求/);
});

test('use-only Agent access is visibly read-only for mind and skills', () => {
  assert.match(mindTab, /manageRequiredForMind/);
  assert.match(skillsTab, /manageRequiredForSkills/);
  assert.match(zh.agent.permissions.manageRequiredForMind, /使用权限.*管理权限/);
  assert.match(en.agent.permissions.manageRequiredForSkills, /use access.*manage access/i);
});
