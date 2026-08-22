import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const work = readFileSync(new URL('../src/pages/Work.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const detail = readFileSync(new URL('../src/pages/WorkDetail.tsx', import.meta.url), 'utf8');

test('task confirmation preserves one idempotency identity across ambiguous retries', () => {
  assert.match(work, /client_request_id: clientRequestId/);
  assert.match(work, /confirmation_fingerprint: confirmedPreflight\.confirmation_fingerprint/);
  assert.match(work, /setClientRequestId\(createRandomUUID\(\)\)/);
  assert.match(work, /!createTask\.isPending/);
  assert.match(work, /saveWorkDraft\(window\.sessionStorage/);
  assert.match(work, /clientRequestId,/);
});

test('stale confirmation and changed capability require a fresh preflight', () => {
  assert.match(work, /'work_confirmation_stale', 'work_capability_changed'/);
  assert.match(work, /String\(error\?\.code \|\| ''\)/);
  assert.match(work, /setPreflight\(null\)/);
  assert.doesNotMatch(work, /message[^\n]*includes\('work_confirmation_stale'\)/);
});

test('ordinary users submit business intent without provider, model, skill, or tool fields', () => {
  assert.match(api, /export type WorkTaskDraft/);
  const draftContract = api.slice(
    api.indexOf('export type WorkTaskDraft'),
    api.indexOf('export type WorkTaskPreflight'),
  );
  assert.doesNotMatch(draftContract, /provider|model|skill|tool/i);
  assert.match(work, /模型、Provider、Skill 和 Tool 由平台治理/);
});

test('commercial Work requires a confirmed acceptance contract and owner decision', () => {
  assert.match(work, /acceptance_contract:/);
  assert.match(work, /owner_review_required: true/);
  assert.match(work, /业务验收标准/);
  assert.match(work, /超长报告不要放在任务消息里/);
  assert.match(api, /task_result_review/);
  assert.match(api, /reviewTaskResult/);
  assert.match(detail, /验收任务结果/);
  assert.match(detail, /action: 'request_changes'/);
  assert.match(detail, /action: 'approve'/);
});

test('unknown Work tool outcomes require an explicit owner fact before resume', () => {
  assert.match(api, /tool-executions\/\$\{executionId\}\/reconcile/);
  assert.match(api, /outcome: 'applied' \| 'not_applied'/);
  assert.match(detail, /tool_reconciliation/);
  assert.match(detail, /填写你在目标系统中核对到的事实（必填）/);
  assert.match(detail, /submitToolReconciliation\(action, 'applied'\)/);
  assert.match(detail, /submitToolReconciliation\(action, 'not_applied'\)/);
  assert.match(detail, /在你确认前不会重复执行/);
});
