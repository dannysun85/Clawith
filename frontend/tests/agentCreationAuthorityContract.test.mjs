import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const hook = readFileSync(
  new URL('../src/hooks/useAgentCreationLimit.ts', import.meta.url),
  'utf8',
);
const layout = readFileSync(new URL('../src/pages/Layout.tsx', import.meta.url), 'utf8');

test('subscription seat queries can be disabled for identities without agent creation authority', () => {
  assert.match(hook, /type AgentCreationLimitOptions = \{[\s\S]*enabled\?: boolean;/);
  assert.match(hook, /const enabled = options\.enabled \?\? true;/);
  assert.equal((hook.match(/\benabled,\n\s*\}\);/g) || []).length, 2);
});

test('the shared layout only loads creation limits for users who can create an agent', () => {
  assert.match(layout, /hasEffectiveCapability\(user, 'agent\.create\.company'\)/);
  assert.match(layout, /hasEffectiveCapability\(user, 'agent\.create\.private'\)/);
  assert.match(
    layout,
    /useAgentCreationLimit\(employeeAgents as any\[\], \{\n\s*enabled: canCreateAgent,/,
  );
});
