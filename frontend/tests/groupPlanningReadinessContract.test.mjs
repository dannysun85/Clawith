import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const groupApi = readFileSync(
  new URL('../src/services/groupApi.ts', import.meta.url),
  'utf8',
);
const groupsPage = readFileSync(
  new URL('../src/pages/groups/GroupsPage.tsx', import.meta.url),
  'utf8',
);
const composer = readFileSync(
  new URL('../src/pages/groups/MessageComposer.tsx', import.meta.url),
  'utf8',
);

test('group workspace reads tenant-safe planning readiness before multi-agent send', () => {
  assert.match(groupApi, /\/groups\/\$\{groupId\}\/planning-readiness/);
  assert.match(groupsPage, /queryKey: \['group-planning-readiness', groupId\]/);
  assert.match(groupsPage, /planningAvailable=\{planningReadinessQuery\.data\?\.available\}/);
  assert.match(groupsPage, /planningReadinessLoading=\{planningReadinessQuery\.isLoading\}/);
});

test('multi-agent send fails closed while single-agent and plain messages remain available', () => {
  assert.match(composer, /agentCount > 1/);
  assert.match(
    composer,
    /planningReadinessLoading \|\| planningAvailable !== true/,
  );
  assert.match(composer, /sending \|\| disabled \|\| multiAgentPlanningBlocked/);
  assert.match(composer, /仅 @ 一名 Agent 继续/);
  assert.doesNotMatch(composer, /disabled=\{planningAvailable !== true\}/);
});
