import { chromium } from 'playwright';

import { waitForExactText } from './browser_assertions.mjs';


let delayedReads = 0;
await waitForExactText(
  {
    async innerText() {
      delayedReads += 1;
      return delayedReads < 3 ? '0 / 0' : '490 / 1,000';
    },
  },
  '490 / 1,000',
  { timeoutMs: 100, pollIntervalMs: 0, sleep: async () => {} },
);
if (delayedReads !== 3) process.exit(1);


const browser = await chromium.launch({ channel: 'chromium', headless: true });
try {
  const page = await browser.newPage();
  await page.goto('data:text/html,<title>Astra browser gate</title>');
  if (await page.title() !== 'Astra browser gate') {
    throw new Error('browser self-test title mismatch');
  }
  process.stdout.write('{"ok":true,"check":"browser_launch_selftest_ok"}\n');
} finally {
  await browser.close();
}
