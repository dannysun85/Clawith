import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../src/pages/SaasAdmin.tsx', import.meta.url), 'utf8');

test('platform model routes expose a complete account-to-model verification flow', () => {
  assert.match(source, /平台模型池/);
  assert.match(source, /Provider 账号池/);
  assert.match(source, /\/enterprise\/llm-models\?platform=true/);
  assert.match(source, /\/enterprise\/llm-test/);
  assert.match(source, /连接与工具验证/);
  assert.match(source, /PlatformModelPoolCard models=\{models\}/);
});
