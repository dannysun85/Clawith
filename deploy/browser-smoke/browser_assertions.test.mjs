import assert from 'node:assert/strict';
import test from 'node:test';

import { partitionBrowserIssues, waitForExactText } from './browser_assertions.mjs';


test('waitForExactText tolerates a delayed React DOM commit', async () => {
  let reads = 0;
  const locator = {
    async innerText() {
      reads += 1;
      return reads < 3 ? '0 / 0' : '490 / 1,000';
    },
  };

  const actual = await waitForExactText(locator, '490 / 1,000', {
    timeoutMs: 10,
    pollIntervalMs: 0,
    sleep: async () => {},
  });

  assert.equal(actual, '490 / 1,000');
  assert.equal(reads, 3);
});


test('waitForExactText fails within a bounded deadline', async () => {
  let clock = 0;
  const locator = { async innerText() { return 'stale'; } };

  await assert.rejects(
    waitForExactText(locator, 'current', {
      timeoutMs: 2,
      pollIntervalMs: 1,
      now: () => clock,
      sleep: async () => { clock += 1; },
    }),
    /did not converge/,
  );
});


test('partitionBrowserIssues tolerates only a correlated direct-runtime 409', () => {
  const path = '/api/agents/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/sessions/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/runtime-state';
  const result = partitionBrowserIssues({
    httpErrors: [{ path, status: 409 }],
    consoleErrors: [{ category: 'failed_resource', http_status: 409, source_path: path }],
  });

  assert.equal(result.toleratedHttpErrors.length, 1);
  assert.equal(result.toleratedConsoleErrors.length, 1);
  assert.deepEqual(result.unexpectedHttpErrors, []);
  assert.deepEqual(result.unexpectedConsoleErrors, []);
});


test('partitionBrowserIssues never hides unrelated or uncorrelated browser failures', () => {
  const path = '/api/agents/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/sessions/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/runtime-state';
  const result = partitionBrowserIssues({
    httpErrors: [
      { path, status: 409 },
      { path, status: 409 },
      { path: '/assets/missing.js', status: 404 },
    ],
    consoleErrors: [
      { category: 'failed_resource', http_status: 404, source_path: '/assets/missing.js' },
      { category: 'react', http_status: null, source_path: '/assets/index.js' },
      { category: 'failed_resource', http_status: 409, source_path: path },
      { category: 'failed_resource', http_status: 409, source_path: path },
    ],
  });

  assert.deepEqual(result.unexpectedHttpErrors, [
    { path, status: 409 },
    { path: '/assets/missing.js', status: 404 },
  ]);
  assert.equal(result.toleratedConsoleErrors.length, 1);
  assert.equal(result.unexpectedConsoleErrors.length, 3);
});
