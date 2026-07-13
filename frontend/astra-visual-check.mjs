// Quick visual verification for the Astra rebrand.
// Takes screenshots of key pages so we can spot contrast/readability issues.
// Usage: node astra-visual-check.mjs   (dev server must be running on :3009)
import { chromium } from 'playwright';
import { writeFileSync, mkdirSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, '../.playwright-cli/astra-check');
mkdirSync(OUT_DIR, { recursive: true });

const BASE = 'http://localhost:3009';

const pages = [
  { path: '/login', name: 'login-dark', width: 1440, height: 900, theme: 'dark' },
  { path: '/login', name: 'login-light', width: 1440, height: 900, theme: 'light' },
  { path: '/forgot-password', name: 'forgot-dark', width: 1440, height: 900, theme: 'dark' },
];

const browser = await chromium.launch({ headless: true });

async function shoot({ path, name, width, height, theme }) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  // Set theme before navigation so the page picks it up from localStorage
  await page.addInitScript((t) => {
    localStorage.setItem('theme', t);
    document.documentElement.setAttribute('data-theme', t);
  }, theme);
  const url = BASE + path;
  const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
  if (!resp || !resp.ok()) {
    console.error(`[${name}] ${resp ? resp.status() : 'no response'} — ${url}`);
    await page.close();
    return;
  }
  // Wait for fonts
  await page.waitForTimeout(800);
  // Capture console errors
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('PAGE_ERROR: ' + e.message));

  const shotPath = resolve(OUT_DIR, `${name}.png`);
  await page.screenshot({ path: shotPath, fullPage: false });
  console.log(`[${name}] saved ${shotPath} (console errors: ${errors.length})`);
  if (errors.length) {
    writeFileSync(resolve(OUT_DIR, `${name}.errors.txt`), errors.join('\n'));
    for (const e of errors.slice(0, 5)) console.log('   -', e.slice(0, 200));
  }
  await page.close();
}

for (const p of pages) await shoot(p);
await browser.close();
console.log(`\nScreenshots saved in ${OUT_DIR}`);
