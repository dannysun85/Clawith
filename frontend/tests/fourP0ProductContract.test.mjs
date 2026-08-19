import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const layout = readFileSync(new URL('../src/pages/Layout.tsx', import.meta.url), 'utf8');
const dashboard = readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8');
const work = readFileSync(new URL('../src/pages/Work.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const subscription = readFileSync(new URL('../src/pages/SubscriptionDetail.tsx', import.meta.url), 'utf8');
const subscriptionTab = readFileSync(new URL('../src/pages/enterprise-settings/tabs/SubscriptionTab.tsx', import.meta.url), 'utf8');
const okr = readFileSync(new URL('../src/pages/OKR.tsx', import.meta.url), 'utf8');

test('work has one detail route and an authoritative attention view', () => {
  assert.match(app, /path="work\/:taskId"/);
  assert.match(api, /getInbox/);
  assert.match(api, /getInboxCount/);
  assert.match(api, /getTaskDetail/);
  assert.match(work, /待我处理/);
  assert.match(work, /进行中/);
  assert.match(work, /最近完成/);
});

test('company resource metrics are capability-gated without topology reconstruction', () => {
  assert.match(dashboard, /company\.analytics\.view/);
  assert.match(dashboard, /enabled:\s*canViewCompanyAnalytics/);
  assert.doesNotMatch(dashboard, /topology\.nodes\.reduce[\s\S]{0,300}tokens_used_today/);
});

test('billing navigation distinguishes personal usage from company finance', () => {
  assert.match(layout, /我的用量/);
  assert.match(layout, /company\.billing\.view/);
  assert.match(api, /getMyUsage/);
  assert.match(subscription, /enabled:\s*Boolean\(tenantId && canViewCompanyBilling\)/);
  assert.match(subscription, /enabled:\s*Boolean\(tenantId && canManageCompanyBilling\)/);
  assert.match(subscription, /productAccessSignature\(user\)/);
  assert.match(subscriptionTab, /if \(!canManageCompanyBilling\) \{/);
  assert.match(subscriptionTab, /仅公司所有者可购买/);
  assert.match(subscriptionTab, /if \(!canManageCompanyBilling\) \{[\s\S]{0,140}disabled: true/);
});

test('okr projects member-safe data and gates company reporting by tenant capability', () => {
  assert.match(okr, /company\.okr\.manage/);
  assert.match(okr, /company\.okr\.reports\.view_all/);
  assert.match(okr, /productAccessSignature\(currentUser\)/);
  assert.match(okr, /enabled:\s*isAdmin\s*&&\s*view === 'company'/);
  assert.match(okr, /我的日报/);
});
