import fs from 'node:fs';
import process from 'node:process';

import { chromium } from 'playwright';

import { waitForExactText } from './browser_assertions.mjs';


const REQUIRED_CREDENTIAL_KEYS = [
  'SMOKE_TENANT_EMAIL',
  'SMOKE_TENANT_PASSWORD',
];


function fail(stage, detail) {
  const error = new Error(stage);
  error.stage = stage;
  error.detail = detail;
  throw error;
}


function requireCondition(condition, stage, detail) {
  if (!condition) fail(stage, detail);
}


function parseArgs(argv) {
  const allowed = new Set([
    'frontend-url',
    'evidence-frontend-url',
    'credentials-file',
    'expected-version',
    'expected-commit',
    'expected-release-id',
    'evidence-nonce',
  ]);
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) {
    const raw = argv[index];
    const value = argv[index + 1];
    requireCondition(raw?.startsWith('--') && value !== undefined, 'arguments', { argument: raw });
    const key = raw.slice(2);
    requireCondition(allowed.has(key) && !(key in parsed), 'arguments', { argument: raw });
    parsed[key] = value;
  }
  for (const key of allowed) {
    requireCondition(parsed[key], 'arguments', { missing: key });
  }
  return parsed;
}


function readCredentials(path) {
  const stat = fs.lstatSync(path, { throwIfNoEntry: false });
  requireCondition(
    stat?.isFile() && !stat.isSymbolicLink() && stat.size > 0 && stat.size <= 16_384,
    'credentials_file', {
    regular_file: Boolean(stat?.isFile() && !stat.isSymbolicLink()),
    size: stat?.size ?? null,
  });
  const payload = JSON.parse(fs.readFileSync(path, 'utf8'));
  requireCondition(
    payload && typeof payload === 'object' && !Array.isArray(payload),
    'credentials_file',
    { format: 'object_required' },
  );
  requireCondition(
    Object.keys(payload).sort().join('\n') === [...REQUIRED_CREDENTIAL_KEYS].sort().join('\n'),
    'credentials_file',
    { keys: 'exact_required_keys_only' },
  );
  for (const key of REQUIRED_CREDENTIAL_KEYS) {
    requireCondition(
      typeof payload[key] === 'string' && payload[key].length > 0 && payload[key].length <= 4096,
      'credentials_file',
      { invalid_key: key },
    );
  }
  return payload;
}


function sameOriginPath(rawUrl, frontendUrl, path) {
  try {
    const candidate = new URL(rawUrl);
    const frontend = new URL(frontendUrl);
    return candidate.origin === frontend.origin && candidate.pathname === path;
  } catch {
    return false;
  }
}


function formatCredits(value) {
  return new Intl.NumberFormat('zh-CN').format(value);
}


async function requireExactCreditsText(locator, expected, field) {
  try {
    await waitForExactText(locator, expected);
  } catch {
    fail('ui_subscription_balance_rendered', { field, expected });
  }
}


async function run() {
  const args = parseArgs(process.argv.slice(2));
  const credentials = readCredentials(args['credentials-file']);
  const frontendUrl = args['frontend-url'].replace(/\/+$/, '');
  const evidenceFrontendUrl = args['evidence-frontend-url'].replace(/\/+$/, '');
  const expectedCommit = args['expected-commit'];
  requireCondition(/^[0-9a-f]{40}$/.test(expectedCommit), 'arguments', { expected_commit: 'invalid' });
  requireCondition(/^[0-9a-f]{32}$/.test(args['evidence-nonce']), 'arguments', {
    evidence_nonce: 'invalid',
  });

  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      ignoreHTTPSErrors: false,
      locale: 'zh-CN',
      serviceWorkers: 'block',
    });
    const blockedOrigins = [];
    await context.route('**/*', async (route) => {
      const requestUrl = route.request().url();
      let parsedUrl;
      try {
        parsedUrl = new URL(requestUrl);
      } catch {
        blockedOrigins.push('invalid-url');
        await route.abort('blockedbyclient');
        return;
      }
      const protocol = parsedUrl.protocol;
      if (protocol === 'data:' || protocol === 'blob:') {
        await route.continue();
        return;
      }
      if (parsedUrl.origin !== new URL(frontendUrl).origin) {
        blockedOrigins.push(parsedUrl.origin);
        await route.abort('blockedbyclient');
        return;
      }
      await route.continue();
    });
    const page = await context.newPage();
    let unexpectedPageCount = 0;
    context.on('page', (openedPage) => {
      if (openedPage !== page) unexpectedPageCount += 1;
    });
    const serverErrors = [];
    page.on('response', (response) => {
      if (response.url().startsWith(frontendUrl) && response.status() >= 500) {
        serverErrors.push({ path: new URL(response.url()).pathname, status: response.status() });
      }
    });

    const versionResponse = await context.request.get(`${frontendUrl}/api/version`, { timeout: 30_000 });
    requireCondition(versionResponse.status() === 200, 'release_identity', {
      status: versionResponse.status(),
    });
    const releaseIdentity = await versionResponse.json();
    requireCondition(
      releaseIdentity?.version === args['expected-version']
        && releaseIdentity?.commit === expectedCommit
        && releaseIdentity?.release_id === args['expected-release-id'],
      'release_identity',
      {
        version: releaseIdentity?.version ?? null,
        commit: releaseIdentity?.commit ?? null,
        release_id: releaseIdentity?.release_id ?? null,
      },
    );

    await page.goto(`${frontendUrl}/login`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    requireCondition(new URL(page.url()).origin === new URL(frontendUrl).origin, 'ui_login_origin', {
      origin: new URL(page.url()).origin,
    });
    await page.locator('input[type="email"]').fill(credentials.SMOKE_TENANT_EMAIL);
    await page.locator('input[type="password"]').fill(credentials.SMOKE_TENANT_PASSWORD);
    const loginResponsePromise = page.waitForResponse(
      (response) => sameOriginPath(response.url(), frontendUrl, '/api/auth/login')
        && response.request().method() === 'POST',
      { timeout: 30_000 },
    );
    await page.locator('button[type="submit"]').click();
    const loginResponse = await loginResponsePromise;
    requireCondition(loginResponse.status() === 200, 'ui_tenant_login', {
      status: loginResponse.status(),
    });
    await page.waitForURL(
      (url) => url.origin === new URL(frontendUrl).origin && url.pathname !== '/login',
      { timeout: 30_000 },
    );

    const summaryResponsePromise = page.waitForResponse(
      (response) => sameOriginPath(response.url(), frontendUrl, '/api/subscription/summary'),
      { timeout: 30_000 },
    );
    await page.goto(`${frontendUrl}/account/subscription`, {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });
    const summaryResponse = await summaryResponsePromise;
    requireCondition(summaryResponse.status() === 200, 'ui_subscription_summary', {
      status: summaryResponse.status(),
    });
    const summary = await summaryResponse.json();
    requireCondition(
      summary
        && Number.isFinite(summary.balance)
        && Number.isFinite(summary.available_balance)
        && Number.isFinite(summary.reserved)
        && Number.isFinite(summary.consumed_credits)
        && Number.isFinite(summary.total_granted),
      'ui_subscription_summary',
      { required_fields: 'balance,available_balance,reserved,consumed_credits,total_granted' },
    );

    await page.locator('.subscription-detail-page').waitFor({ state: 'visible', timeout: 30_000 });
    await page.locator('.subscription-detail-page .subscription-usage-card').waitFor({
      state: 'visible',
      timeout: 30_000,
    });
    const creditsUsageValue = page.getByTestId('subscription-credits-usage-value');
    const availableCreditsValue = page.getByTestId('subscription-available-credits-value');
    const reservedCreditsValue = page.getByTestId('subscription-available-credits-reserved');
    await creditsUsageValue.waitFor({ state: 'visible', timeout: 30_000 });
    await availableCreditsValue.waitFor({ state: 'visible', timeout: 30_000 });
    await reservedCreditsValue.waitFor({ state: 'visible', timeout: 30_000 });
    const expectedCreditsUsage = `${formatCredits(summary.consumed_credits)} / ${formatCredits(summary.total_granted)}`;
    const expectedAvailableCredits = `${formatCredits(summary.available_balance)} / ${formatCredits(summary.balance)}`;
    const expectedReservedCredits = summary.reserved
      ? `预占 ${formatCredits(summary.reserved)}`
      : '积分';
    await Promise.all([
      requireExactCreditsText(creditsUsageValue, expectedCreditsUsage, 'credits_usage'),
      requireExactCreditsText(availableCreditsValue, expectedAvailableCredits, 'available_credits'),
      requireExactCreditsText(reservedCreditsValue, expectedReservedCredits, 'reserved_credits'),
    ]);
    requireCondition(new URL(page.url()).pathname === '/account/subscription', 'ui_final_url', {
      path: new URL(page.url()).pathname,
    });
    const bodyText = await page.locator('body').innerText();
    requireCondition(/Credits 用量|Credits Usage/i.test(bodyText), 'ui_credits_metric', {
      visible: false,
    });
    requireCondition(/可用 Credits|Available Credits/i.test(bodyText), 'ui_available_credits_metric', {
      visible: false,
    });
    requireCondition(
      !/Internal Server Error|HTTP 5\d\d|加载失败|请求失败|Failed to load/i.test(bodyText),
      'ui_error_banner',
      { visible: true },
    );
    requireCondition(serverErrors.length === 0, 'ui_server_responses', serverErrors.slice(0, 10));
    requireCondition(blockedOrigins.length === 0, 'ui_cross_origin_request', {
      origins: [...new Set(blockedOrigins)].slice(0, 10),
    });
    requireCondition(unexpectedPageCount === 0, 'ui_unexpected_popup', {
      count: unexpectedPageCount,
    });

    return {
      evidence_schema_version: 2,
      evidence_kind: 'subscription_browser',
      ok: true,
      frontend_url: evidenceFrontendUrl,
      browser_target: 'isolated_candidate_frontend_network',
      release_identity: releaseIdentity,
      evidence_nonce: args['evidence-nonce'],
      final_path: '/account/subscription',
      subscription_summary: {
        plan_code: summary.plan_code ?? null,
        balance: summary.balance,
        available_balance: summary.available_balance,
        reserved: summary.reserved ?? null,
      },
      checks: [
        'ui_release_identity_ok',
        'ui_tenant_login_ok',
        'ui_subscription_summary_api_ok',
        'ui_subscription_balance_rendered_ok',
        'ui_subscription_page_ok',
        'ui_no_server_error_ok',
      ],
    };
  } finally {
    await browser.close();
  }
}


try {
  process.stdout.write(`${JSON.stringify(await run(), null, 2)}\n`);
} catch (error) {
  const payload = {
    evidence_schema_version: 2,
    evidence_kind: 'subscription_browser',
    ok: false,
    stage: error?.stage ?? 'browser_smoke',
    detail: error?.detail ?? { name: error?.name ?? 'Error', message: error?.message ?? 'unknown' },
  };
  process.stderr.write(`${JSON.stringify(payload)}\n`);
  process.exitCode = 1;
}
