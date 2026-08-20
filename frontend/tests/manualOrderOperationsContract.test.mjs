import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const saasAdmin = readFileSync(new URL('../src/pages/SaasAdmin.tsx', import.meta.url), 'utf8');
const subscriptionDetail = readFileSync(new URL('../src/pages/SubscriptionDetail.tsx', import.meta.url), 'utf8');
const saasApi = readFileSync(new URL('../../backend/app/api/saas.py', import.meta.url), 'utf8');

test('platform operations exposes a complete manual-order processing surface', () => {
  assert.match(saasAdmin, /key: 'orders', label: '人工订单'/);
  assert.match(saasAdmin, /fetchJson<PaymentOrder\[]>\([\s\S]*?`\/saas\/orders\?limit=100/);
  assert.match(saasAdmin, /`\/saas\/orders\/\$\{orderId\}\/mark-paid`/);
  assert.match(saasAdmin, /order\.provider === 'manual' && order\.status === 'pending'/);
  assert.match(saasAdmin, /仅可用于已到账款项或明确标记的本地测试数据/);
  assert.match(saasAdmin, /等待供应商凭证/);
});

test('company order history distinguishes manual review from online payment', () => {
  assert.match(subscriptionDetail, /order\.status === 'pending' && order\.provider === 'manual'/);
  assert.match(subscriptionDetail, /return '待人工处理'/);
});

test('the server refuses to manually finalize provider-backed orders', () => {
  assert.match(saasApi, /if order\.provider != "manual"/);
  assert.match(saasApi, /Provider-backed orders can only be finalized by a verified provider event/);
});
