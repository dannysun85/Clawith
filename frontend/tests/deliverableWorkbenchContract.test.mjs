import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const workbench = readFileSync(
  new URL('../src/components/deliverables/DeliverableWorkbench.tsx', import.meta.url),
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
