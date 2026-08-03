import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/App.tsx', import.meta.url),
  'utf8',
);

test('auth bootstrap refreshes the same-origin browser session when a user is already hydrated', () => {
  assert.match(source, /establishBrowserSession\(effectiveToken\)/);
  assert.match(source, /const existingUser = useAuthStore\.getState\(\)\.user/);
  assert.match(source, /if \(existingUser\)[\s\S]*?establishBrowserSession\(effectiveToken\)/);
});
