import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const billingHook = readFileSync(`${root}/src/hooks/useBillingConfig.ts`, 'utf8');
const subscriptionTab = readFileSync(
  `${root}/src/pages/enterprise-settings/tabs/SubscriptionTab.tsx`,
  'utf8',
);
const companyAdmin = readFileSync(`${root}/src/pages/CompanyAdmin.tsx`, 'utf8');

test('billing config keeps provider selection separate from verified checkout readiness', () => {
  assert.match(billingHook, /checkout_enabled: boolean/);
  assert.match(billingHook, /native_payment_enabled: boolean/);
  assert.match(billingHook, /webhook_ready: boolean/);
  assert.match(billingHook, /missing_config: string\[\]/);
  assert.match(billingHook, /next_action: string/);
});

test('marketplace fails closed and labels manual orders as offline requests', () => {
  assert.match(subscriptionTab, /if \(!billingConfig\.checkout_enabled\)/);
  assert.match(subscriptionTab, /支付通道尚未就绪，系统已阻止创建订单/);
  assert.match(subscriptionTab, /提交人工订单/);
  assert.match(subscriptionTab, /人工订单模式：提交后由平台管理员线下处理/);
  assert.match(subscriptionTab, /billingConfig\?\.native_payment_enabled/);
  assert.match(companyAdmin, /根据当前支付配置显示在线支付或人工订单流程/);
  assert.doesNotMatch(companyAdmin, /支付仅允许在官方支付域名上发起/);
});
