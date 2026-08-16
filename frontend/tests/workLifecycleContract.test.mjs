import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const work = readFileSync(new URL('../src/pages/Work.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');

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
