import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const modal = readFileSync(new URL('../src/pages/groups/CreateGroupTaskModal.tsx', import.meta.url), 'utf8');
const stream = readFileSync(new URL('../src/pages/groups/MessageStream.tsx', import.meta.url), 'utf8');
const page = readFileSync(new URL('../src/pages/groups/GroupsPage.tsx', import.meta.url), 'utf8');
const panel = readFileSync(new URL('../src/pages/groups/GroupSidePanel.tsx', import.meta.url), 'utf8');
const taskTab = readFileSync(new URL('../src/pages/groups/GroupTasksTab.tsx', import.meta.url), 'utf8');
const groupApi = readFileSync(new URL('../src/services/groupApi.ts', import.meta.url), 'utf8');
const workApi = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const workDetail = readFileSync(new URL('../src/pages/WorkDetail.tsx', import.meta.url), 'utf8');
const layout = readFileSync(new URL('../src/pages/Layout.tsx', import.meta.url), 'utf8');
const groupStyles = readFileSync(new URL('../src/pages/groups/groups.css', import.meta.url), 'utf8');

test('a group message only becomes a formal task through an explicit confirmed action', () => {
  assert.match(stream, /onCreateTask\(message\)/);
  assert.match(stream, /从消息创建正式任务/);
  assert.match(modal, /source_kind:\s*'group_message'/);
  assert.match(modal, /source_message_id:\s*sourceMessage\.id/);
  assert.doesNotMatch(modal, /source_message_cursor\s*:/);
  assert.match(modal, /workApi\.preflightTask\(draft\)/);
  assert.match(modal, /workApi\.createTask/);
  assert.match(modal, /确认后才会创建 Task；原消息仍保留在当前会话/);
  assert.doesNotMatch(page, /sendMessage[\s\S]{0,500}createTask/);
});

test('group task confirmation makes one primary owner explicit', () => {
  assert.match(modal, /必须明确选择唯一第一责任人/);
  assert.match(modal, /primaryOwnerId/);
  assert.match(modal, /group_agent_participant_ids:\s*selectedAgentParticipantIds/);
  assert.match(modal, /第一责任人（必选）/);
  assert.match(modal, /aria-label=\{t\('common\.close', '关闭'\)\}/);
  assert.match(workApi, /source_kind\?:\s*'workbench'\s*\|\s*'group_message'/);
});

test('a duplicate source conversion opens the authoritative existing task', () => {
  assert.match(modal, /group_message_already_converted/);
  assert.match(modal, /error\.details\.task_id/);
  assert.match(modal, /onCreated\(existingTaskId\)/);
});

test('group and work share one task through a read projection and deep links', () => {
  assert.match(groupApi, /\/groups\/\$\{groupId\}\/tasks/);
  assert.match(panel, /key:\s*'tasks'/);
  assert.match(panel, /<GroupTasksTab/);
  assert.match(taskTab, /navigate\(task\.work_link\)/);
  assert.match(page, /queryKey:\s*\['group-tasks',\s*currentUser\?\.tenant_id,\s*activeGroupId,\s*activeSessionId\]/);
  assert.match(page, /navigate\(`\/work\/\$\{taskId\}`\)/);
  assert.match(page, /linkedTaskByMessageId/);
  assert.match(stream, /已关联任务/);
  assert.match(stream, /onOpenTask\(linkedTask\.task_id\)/);
  assert.match(
    workDetail,
    /summary\.executor_kind === 'group'\s*\? detail\.links\.executor\s*:\s*detail\.links\.formal_delivery \|\| detail\.links\.executor/,
  );
});

test('group collaborators receive a collaboration-safe detail without delivery downloads', () => {
  assert.match(workApi, /detail_scope:\s*'full'\s*\|\s*'collaboration'/);
  assert.match(workDetail, /const isFullDetail = detail\.detail_scope === 'full'/);
  assert.match(workDetail, /isFullDetail && detail\.artifacts\.length > 0/);
  assert.match(workDetail, /协作安全视图/);
});

test('group linked-task cards expose participant run ownership and failures', () => {
  assert.match(taskTab, /task\.runs\.filter/);
  assert.match(taskTab, /run\.agent_name/);
  assert.match(taskTab, /run_failed/);
  assert.match(taskTab, /TERMINAL_RUN_EVENTS/);
  assert.match(taskTab, /outcomeNotificationLabel\(run\.delivery_status, isChinese\)/);
  assert.match(taskTab, /结果通知已送达/);
  assert.match(workDetail, /runtimeEventLabel\(run\.latest_event, isChinese\)/);
  assert.match(workDetail, /outcomeNotificationLabel\(run\.delivery_status, isChinese\)/);
  assert.match(workDetail, /结果通知已送达/);
});

test('the group workflow remains usable at mobile width', () => {
  assert.match(layout, /isGroupsPage \? ' groups-shell' : ''/);
  assert.match(page, /searchParams\.get\('view'\) === 'list'/);
  assert.match(page, /navigate\('\/groups\?view=list'\)/);
  assert.match(page, /localStorage\.setItem\('groups\.showPanel', '0'\)/);
  assert.match(groupStyles, /\.groups-page\.has-active-session \.group-column\.tree\s*\{\s*display:\s*none/);
  assert.match(groupStyles, /\.group-side-panel\s*\{[\s\S]*?width:\s*100% !important/);
  assert.match(groupStyles, /\.group-message-create-task\s*\{\s*opacity:\s*1 !important/);
});
