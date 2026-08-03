import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const workbench = readFileSync(
  new URL('../src/components/deliverables/DeliverableWorkbench.tsx', import.meta.url),
  'utf8',
);
const reviewPage = readFileSync(
  new URL('../src/pages/QualityReview.tsx', import.meta.url),
  'utf8',
);
const agentDetailPage = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);

test('agent message owns the compact result summary and the full workflow stays in a drawer', () => {
  assert.match(workbench, /className="deliverable-summary-card"/);
  assert.match(workbench, /'查看交付详情'/);
  assert.match(workbench, /className="deliverable-drawer deliverable-detail-drawer"/);
  assert.match(workbench, /className="deliverable-review-card deliverable-review-card--drawer"/);
  assert.match(workbench, /'预览文件'/);
  assert.match(workbench, /'质量检查'/);
  assert.match(workbench, /'确认交付'/);
  assert.match(workbench, /'在线预览'/);
  assert.match(workbench, /className="deliverable-review-card__preview"/);
  assert.match(workbench, /<video[\s\S]*?controls[\s\S]*?playsInline[\s\S]*?preload="metadata"/);
  assert.match(workbench, /title=\{isZh \? 'PPT 逐页预览'/);
  assert.match(workbench, /alt=\{isZh \? '交付图片预览'/);
  assert.match(workbench, /'下载 PPTX'/);
  assert.match(workbench, /'管理企业成员'/);
  assert.doesNotMatch(workbench, /artifact\.status/);
});

test('completed deliverable is rendered in the chat timeline, never inside the composer', () => {
  const resultCard = agentDetailPage.indexOf('{trackedDeliverables.map((request) => (');
  const composer = agentDetailPage.indexOf('<div ref={chatInputAreaRef} className="chat-input-area"');
  const pendingBrief = agentDetailPage.indexOf('{pendingDeliverable && (');

  assert.ok(resultCard >= 0, 'tracked deliverable result must be rendered');
  assert.ok(composer >= 0, 'chat composer must exist');
  assert.ok(pendingBrief >= 0, 'pending deliverable brief must be rendered');
  assert.ok(resultCard < composer, 'completed result belongs to the conversation timeline');
  assert.ok(pendingBrief > composer, 'only the pre-send brief belongs to the composer');
  assert.match(agentDetailPage, /className="chat-msg-row chat-msg-row--deliverable"/);
  assert.match(agentDetailPage, /request=\{request\}/);
});

test('reviewer workspace guides one irreversible review through three steps', () => {
  assert.match(reviewPage, /'查看文件'/);
  assert.match(reviewPage, /'逐项检查'/);
  assert.match(reviewPage, /'评分并提交'/);
  assert.match(reviewPage, /'提交后不能修改/);
  assert.match(reviewPage, /<details className="quality-review-page__admin">/);
  assert.match(reviewPage, /<details className="quality-review-page__technical">/);
});

test('creative artifact failures explain the actionable PPT coverage issue', () => {
  assert.match(workbench, /presentation_picture_coverage_below_minimum/);
  assert.match(workbench, /PPT 图片覆盖不足，请增加大幅主视觉或场景图后重新生成/);
  assert.match(workbench, /deliverableErrorLabel\(request\.last_error_code, isZh\)/);
});
