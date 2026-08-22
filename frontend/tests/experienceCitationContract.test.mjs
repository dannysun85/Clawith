import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);
const detailDrawer = readFileSync(
  new URL('../src/components/ExperienceDetailDrawer.tsx', import.meta.url),
  'utf8',
);

test('missing experience citations render as unavailable instead of live links', () => {
  assert.match(agentDetail, /isPending: citationPending,[\s\S]*?isError: citationError/);
  assert.match(agentDetail, /经验已删除或不可访问/);
  assert.match(agentDetail, /disabled=\{!data\}/);
});

test('the experience drawer distinguishes load failure from loading', () => {
  assert.match(detailDrawer, /isPending: entryPending,[\s\S]*?isError: entryError/);
  assert.match(detailDrawer, /if \(entryPending\)/);
  assert.match(detailDrawer, /经验已删除或不可访问/);
  assert.match(detailDrawer, /enabled: Boolean\(entry\)/);
});

test('published work knowledge exposes its durable task and delivery provenance', () => {
  assert.match(detailDrawer, /entry\.source_task_id \|\| entry\.source_deliverable_request_id/);
  assert.match(detailDrawer, /href=\{`\/work\/\$\{entry\.source_task_id\}`\}/);
  assert.match(detailDrawer, /查看来源任务/);
  assert.match(detailDrawer, /交付请求 \{entry\.source_deliverable_request_id\}/);
});
