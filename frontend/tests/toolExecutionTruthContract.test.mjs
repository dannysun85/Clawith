import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);

test('direct chat preserves canonical tool execution outcomes from history and realtime packets', () => {
  assert.match(source, /toolExecutionStatus: message\.toolExecutionStatus/);
  assert.match(source, /toolExecutionStatus: m\.toolExecutionStatus/);
  assert.match(source, /toolExecutionStatus: d\.execution_status/);
  assert.match(source, /toolErrorCode: d\.error_code/);
});

test('failed agent contact is never summarized as a successful contact', () => {
  assert.match(source, /item\.executionStatus === 'failed'/);
  assert.match(source, /blockedAgents \+= 1/);
  assert.match(source, /agent\.chat\.blockedAgents/);
  assert.match(source, /Message not sent/);
});

const deliverableWorkbench = readFileSync(
  new URL('../src/components/deliverables/DeliverableWorkbench.tsx', import.meta.url),
  'utf8',
);

test('failed deliverables expose a revision-backed regeneration path', () => {
  assert.match(deliverableWorkbench, /const failedRequest = request\.status === 'failed'/);
  assert.match(deliverableWorkbench, /failedRequest\) && revisionOpen/);
  assert.match(deliverableWorkbench, /重新生成/);
  assert.match(deliverableWorkbench, /创建重试版本/);
});
