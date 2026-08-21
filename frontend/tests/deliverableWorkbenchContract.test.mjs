import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const workbench = readFileSync(
  new URL('../src/components/deliverables/DeliverableWorkbench.tsx', import.meta.url),
  'utf8',
);
const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);

function selectorZIndex(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = styles.match(new RegExp(`${escaped}\\s*\\{[^}]*z-index:\\s*(\\d+)`, 's'));
  assert.ok(match, `missing z-index contract for ${selector}`);
  return Number(match[1]);
}

test('deliverable drawer remains pointer-accessible above the mobile application shell', () => {
  const drawerLayer = selectorZIndex('.deliverable-drawer-layer');
  const mobileAppBar = selectorZIndex('.mobile-app-bar');
  const mobileSidebar = selectorZIndex('.sidebar,\n  .sidebar.collapsed');
  const tenantSwitcher = selectorZIndex('.tenant-switcher-popover');
  const onboardingTour = selectorZIndex('.company-tour-overlay');

  assert.ok(drawerLayer > Math.max(mobileAppBar, mobileSidebar, tenantSwitcher));
  assert.ok(drawerLayer < onboardingTour);
});

test('pointer and keyboard dismissal share the guarded drawer close action', () => {
  assert.match(workbench, /aria-label=\{isZh \? '关闭工作说明'/);
  assert.match(workbench, /aria-label=\{isZh \? '关闭' : 'Close'\}/);
  assert.equal(workbench.match(/onClick=\{closeDrawer\}/g)?.length, 2);
  assert.match(workbench, /event\.key === 'Escape' && !saving/);
});

test('unavailable capability still persists the brief without starting generation', () => {
  assert.doesNotMatch(workbench, /if \(!result \|\| !result\.available\) return;/);
  assert.match(workbench, /if \(!result\) return;/);
  assert.match(workbench, /工作说明仍可保存，不会扣 Credits/);
  assert.match(workbench, /工作说明已保存；当前没有可用线路，未启动生成/);
  assert.match(workbench, /details: result\.next_action/);
  assert.match(workbench, /deliverable-preflight/);
});

test('staged approvals describe the required follow-up instead of claiming production starts', () => {
  assert.match(workbench, /批准分镜（下一步发送消息）/);
  assert.match(workbench, /批准大纲（下一步发送消息）/);
  assert.doesNotMatch(workbench, /批准分镜并开始制作/);
  assert.doesNotMatch(workbench, /批准大纲并开始制作/);
});

test('planning revisions hide production targets and shot redo selects failed units only', () => {
  assert.match(workbench, /storyboardReview \|\| outlineReview/);
  assert.match(workbench, /unit\.status === 'failed'/);
  assert.match(workbench, /stage_key === 'shot_generate' \|\| unit\.stage_key === 'shot_qa'/);
  assert.match(workbench, /setSelectedRevisionUnits\(shotReview \? failedShotKeys : \[\]\)/);
  assert.match(workbench, /请选择至少一个失败镜头/);
});

test('presentation preflight receives the same business goal used by the deliverable brief', () => {
  assert.match(
    workbench,
    /workflow_version:\s*selectedWorkflow\.workflow_version,[\s\S]*goal:\s*goal\.trim\(\),[\s\S]*spec,/,
  );
});

test('task handoff keeps explicit session uploads available to the deliverable brief', () => {
  assert.match(
    agentDetail,
    /attachments=\{attachedFiles\.map\(\(file\) => \(\{[\s\S]*name: file\.name,[\s\S]*path: file\.path/,
  );
  assert.doesNotMatch(
    agentDetail,
    /attachments=\{requestedDeliverableHandoff[\s\S]*\? \[\]/,
  );
});

test('video preview only exposes the fallback message after an actual media error', () => {
  assert.match(workbench, /onError=\{\(\) => setVideoPreviewError\(true\)\}/);
  assert.match(workbench, /videoPreviewError &&/);
  assert.match(workbench, /请下载 MP4 审核/);
  assert.doesNotMatch(workbench, /<video[\s\S]*>\s*\{isZh \? '当前浏览器无法播放此视频。'/);
});
