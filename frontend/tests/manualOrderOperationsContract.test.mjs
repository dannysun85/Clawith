import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const saasAdmin = readFileSync(new URL('../src/pages/SaasAdmin.tsx', import.meta.url), 'utf8');
const subscriptionDetail = readFileSync(new URL('../src/pages/SubscriptionDetail.tsx', import.meta.url), 'utf8');
const saasApi = readFileSync(new URL('../../backend/app/api/saas.py', import.meta.url), 'utf8');
const governance = readFileSync(new URL('../../backend/app/services/manual_order_governance.py', import.meta.url), 'utf8');

test('platform operations exposes a complete manual-order processing surface', () => {
  assert.match(saasAdmin, /key: 'orders', label: '人工订单'/);
  assert.match(saasAdmin, /fetchJson<PaymentOrder\[]>\([\s\S]*?`\/saas\/orders\?limit=100/);
  assert.match(saasAdmin, /`\/saas\/orders\/\$\{request\.order\.id\}\/mark-paid`/);
  assert.match(saasAdmin, /`\/saas\/orders\/\$\{request\.order\.id\}\/operator-decisions`/);
  assert.match(saasAdmin, /order\.provider === 'manual' && order\.status === 'pending'/);
  assert.match(saasAdmin, /'Idempotency-Key': request\.idempotencyKey/);
  assert.match(saasAdmin, /保留待处理/);
  assert.match(saasAdmin, /取消过期/);
  assert.match(saasAdmin, /取消测试单/);
  assert.match(saasAdmin, /撤销取消/);
  assert.match(saasAdmin, /使用同一个幂等键重试/);
  assert.match(saasAdmin, /等待供应商凭证/);
  assert.match(saasAdmin, /复核人工订单处置/);
  assert.match(saasAdmin, /凭证编号或工单引用/);
  assert.match(saasAdmin, /我已核对企业、金额、当前状态和凭证/);
  assert.match(saasAdmin, /审计凭证 \{lastDecisionResult\.decision\.id\}/);
  assert.doesNotMatch(saasAdmin, /window\.prompt/);
});

test('company order history distinguishes manual review from online payment', () => {
  assert.match(subscriptionDetail, /order\.status === 'pending' && order\.provider === 'manual'/);
  assert.match(subscriptionDetail, /return '待人工处理'/);
});

test('the server refuses to manually finalize provider-backed orders', () => {
  assert.match(governance, /if order\.provider != "manual"/);
  assert.match(governance, /Provider-backed orders can only be finalized by a verified provider event/);
});

test('manual-order decisions are tenant fenced, evidence backed, auditable, and reversible', () => {
  assert.match(saasApi, /expected_tenant_id=data\.expected_tenant_id/);
  assert.match(saasApi, /action="saas_manual_order_decision"/);
  assert.match(saasApi, /"idempotency_key_hash": result\.decision\.idempotency_key_hash/);
  assert.match(governance, /Idempotency-Key must contain between 8 and 128/);
  assert.match(governance, /Only the latest operator cancellation can be restored/);
  assert.match(governance, /Order tenant does not match expected_tenant_id/);
});
