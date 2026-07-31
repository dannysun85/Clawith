import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const viteConfig = readFileSync(
  new URL('../vite.config.ts', import.meta.url),
  'utf8',
);

test('local API and websocket proxies use the deterministic IPv4 loopback', () => {
  assert.match(viteConfig, /target: `http:\/\/127\.0\.0\.1:\$\{backendPort\}`/);
  assert.match(viteConfig, /target: `ws:\/\/127\.0\.0\.1:\$\{backendPort\}`/);
  assert.doesNotMatch(viteConfig, /target: `(http|ws):\/\/localhost:/);
});
