import assert from 'node:assert/strict';
import test from 'node:test';

import { waitForExactText } from './browser_assertions.mjs';


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
