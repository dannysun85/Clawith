import fs from 'node:fs';
import process from 'node:process';

import { chromium } from 'playwright';

import { waitForExactText } from './browser_assertions.mjs';


const REQUIRED_CREDENTIAL_KEYS = [
  'SMOKE_TENANT_EMAIL',
  'SMOKE_TENANT_PASSWORD',
  'SMOKE_TENANT_ID',
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


function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}


async function pageApi(page, path, options = {}) {
  return page.evaluate(async ({ requestPath, requestOptions }) => {
    const token = window.localStorage.getItem('token');
    const response = await window.fetch(requestPath, {
      ...requestOptions,
      headers: {
        Accept: 'application/json',
        ...(requestOptions.body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(requestOptions.headers || {}),
      },
    });
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = text;
    }
    return { status: response.status, body };
  }, { requestPath: path, requestOptions: options });
}


async function waitForDirectChatRoundTrip(page, agentId, sessionId, marker, prompt) {
  const deadline = Date.now() + 300_000;
  let lastShape = { message_count: 0, assistant_count: 0 };
  while (Date.now() < deadline) {
    const result = await pageApi(
      page,
      `/api/agents/${agentId}/sessions/${sessionId}/messages?limit=100`,
    );
    requireCondition(result.status === 200 && Array.isArray(result.body), 'ui_direct_chat_history', {
      status: result.status,
    });
    const messages = result.body;
    const userPersisted = messages.some(
      (message) => message?.role === 'user' && message?.content === prompt,
    );
    const assistantMessages = messages.filter((message) => message?.role === 'assistant');
    lastShape = {
      message_count: messages.length,
      assistant_count: assistantMessages.length,
    };
    if (
      userPersisted
      && assistantMessages.some(
        (message) => typeof message?.content === 'string' && message.content.includes(marker),
      )
    ) {
      return lastShape;
    }
    await sleep(2_000);
  }
  fail('ui_direct_chat_round_trip', { code: 'assistant_marker_timeout', ...lastShape });
}


async function run() {
  const args = parseArgs(process.argv.slice(2));
  const credentials = readCredentials(args['credentials-file']);
  requireCondition(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
      .test(credentials.SMOKE_TENANT_ID),
    'credentials_file',
    { invalid_key: 'SMOKE_TENANT_ID' },
  );
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
    const consoleErrors = [];
    const pageErrors = [];
    page.on('response', (response) => {
      if (response.url().startsWith(frontendUrl) && response.status() >= 500) {
        serverErrors.push({ path: new URL(response.url()).pathname, status: response.status() });
      }
    });
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(true);
    });
    page.on('pageerror', (error) => {
      pageErrors.push(error?.name || 'Error');
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
    const firstLoginPayload = await loginResponse.json();
    if (firstLoginPayload?.requires_tenant_selection === true) {
      const tenants = Array.isArray(firstLoginPayload.tenants) ? firstLoginPayload.tenants : [];
      const targetTenant = tenants.find(
        (tenant) => String(tenant?.tenant_id) === credentials.SMOKE_TENANT_ID,
      );
      requireCondition(Boolean(targetTenant), 'ui_tenant_selection', {
        code: 'target_tenant_not_available',
      });
      const tenantButtonName = `${targetTenant.tenant_name}${
        targetTenant.tenant_slug ? ` (${targetTenant.tenant_slug})` : ''
      }`;
      const scopedLoginResponsePromise = page.waitForResponse(
        (response) => sameOriginPath(response.url(), frontendUrl, '/api/auth/login')
          && response.request().method() === 'POST'
          && response.request().postDataJSON()?.tenant_id === credentials.SMOKE_TENANT_ID,
        { timeout: 30_000 },
      );
      const tenantButton = page.getByRole('button', { name: tenantButtonName, exact: true });
      await tenantButton.waitFor({ state: 'visible', timeout: 30_000 });
      await tenantButton.click();
      const scopedLoginResponse = await scopedLoginResponsePromise;
      requireCondition(scopedLoginResponse.status() === 200, 'ui_tenant_login', {
        status: scopedLoginResponse.status(),
      });
      const scopedLoginPayload = await scopedLoginResponse.json();
      requireCondition(
        String(scopedLoginPayload?.user?.tenant_id) === credentials.SMOKE_TENANT_ID,
        'ui_tenant_scope',
        { code: 'unexpected_tenant_context' },
      );
    } else {
      requireCondition(
        String(firstLoginPayload?.user?.tenant_id) === credentials.SMOKE_TENANT_ID,
        'ui_tenant_scope',
        { code: 'unexpected_tenant_context' },
      );
    }
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
    const evidencePrefix = `RTQA-${args['evidence-nonce'].slice(0, 8).toUpperCase()}`;
    const workMarker = `${evidencePrefix}-WORK`;
    const taskTitle = `${workMarker} 商业发布验收`;
    const groupName = `${evidencePrefix}-GROUP 发布验收协作组`;
    const groupMarker = `${evidencePrefix}-GROUP-MESSAGE`;

    await page.goto(`${frontendUrl}/work`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const completedWorkTab = page.getByRole('tab', {
      name: /最近完成|Recently completed/i,
    });
    await completedWorkTab.waitFor({ state: 'visible', timeout: 30_000 });
    await completedWorkTab.click();
    await page.getByText(taskTitle, { exact: true }).first().waitFor({
      state: 'visible',
      timeout: 30_000,
    });

    const groupsResult = await pageApi(page, '/api/groups');
    requireCondition(groupsResult.status === 200 && Array.isArray(groupsResult.body), 'ui_group_api', {
      status: groupsResult.status,
    });
    const releaseGroup = groupsResult.body.find((group) => group?.name === groupName);
    requireCondition(Boolean(releaseGroup?.id), 'ui_group_persistence', {
      code: 'release_group_missing',
    });
    const sessionsResult = await pageApi(page, `/api/groups/${releaseGroup.id}/sessions`);
    requireCondition(sessionsResult.status === 200 && Array.isArray(sessionsResult.body), 'ui_group_api', {
      status: sessionsResult.status,
    });
    const primarySession = sessionsResult.body.find((session) => session?.is_primary === true);
    requireCondition(Boolean(primarySession?.id), 'ui_group_persistence', {
      code: 'primary_group_session_missing',
    });
    await page.goto(`${frontendUrl}/groups/${releaseGroup.id}/${primarySession.id}`, {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });
    await page.getByText(groupName, { exact: true }).first().waitFor({ state: 'visible', timeout: 30_000 });
    await page.getByText(groupMarker, { exact: false }).first().waitFor({ state: 'visible', timeout: 30_000 });

    await page.goto(`${frontendUrl}/employees`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await page.getByTestId('workforce-scope-contract').waitFor({ state: 'visible', timeout: 30_000 });
    await page.waitForFunction(
      (expectedTitle) => document.body.innerText.includes(expectedTitle),
      taskTitle,
      { timeout: 30_000 },
    );

    const workIndexResult = await pageApi(page, '/api/work');
    requireCondition(
      workIndexResult.status === 200 && typeof workIndexResult.body?.personal_assistant_agent_id === 'string',
      'ui_direct_chat_route',
      { status: workIndexResult.status },
    );
    const assistantAgentId = workIndexResult.body.personal_assistant_agent_id;
    await page.goto(`${frontendUrl}/agents/${assistantAgentId}/chat`, {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });
    const directChatShell = page.getByTestId('direct-chat-shell');
    try {
      await directChatShell.waitFor({ state: 'visible', timeout: 30_000 });
    } catch {
      const agentResult = await pageApi(page, `/api/agents/${assistantAgentId}`);
      const shellDiagnostics = await directChatShell.evaluateAll((nodes) => nodes.map((node) => {
        const element = /** @type {HTMLElement} */ (node);
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return {
          display: style.display,
          visibility: style.visibility,
          opacity: style.opacity,
          width_positive: rect.width > 0,
          height_positive: rect.height > 0,
        };
      }));
      fail('ui_direct_chat_shell', {
        current_path: new URL(page.url()).pathname,
        locator_count: shellDiagnostics.length,
        shell_diagnostics: shellDiagnostics,
        agent_status: agentResult.status,
        agent_access_level: typeof agentResult.body?.access_level === 'string'
          ? agentResult.body.access_level
          : null,
        console_error_count: consoleErrors.length,
        page_error_count: pageErrors.length,
        page_error_names: [...new Set(pageErrors)],
      });
    }
    const sessionResponsePromise = page.waitForResponse(
      (response) => sameOriginPath(response.url(), frontendUrl, `/api/agents/${assistantAgentId}/sessions`)
        && response.request().method() === 'POST',
      { timeout: 30_000 },
    );
    await page.getByTestId('direct-chat-new-session').click();
    const sessionResponse = await sessionResponsePromise;
    requireCondition(sessionResponse.status() === 201, 'ui_direct_chat_session', {
      status: sessionResponse.status(),
    });
    const sessionPayload = await sessionResponse.json();
    requireCondition(typeof sessionPayload?.id === 'string', 'ui_direct_chat_session', {
      code: 'session_id_missing',
    });
    const directMarker = `${evidencePrefix}-CHAT`;
    const directPrompt = `${directMarker}\n第一行必须原样输出 ${directMarker}，第二行用一句话说明本次候选版本可以服务真实客户。`;
    const directInput = page.getByTestId('direct-chat-input');
    await directInput.waitFor({ state: 'visible', timeout: 30_000 });
    await page.waitForFunction(
      () => !document.querySelector('[data-testid="direct-chat-input"]')?.hasAttribute('disabled'),
      undefined,
      { timeout: 60_000 },
    );
    await directInput.fill(directPrompt);
    const directSend = page.getByTestId('direct-chat-send');
    await directSend.waitFor({ state: 'visible', timeout: 30_000 });
    await directSend.click();
    const directRoundTrip = await waitForDirectChatRoundTrip(
      page,
      assistantAgentId,
      sessionPayload.id,
      directMarker,
      directPrompt,
    );

    await page.goto(
      `${frontendUrl}/agents/${assistantAgentId}/chat?session_id=${sessionPayload.id}`,
      { waitUntil: 'domcontentloaded', timeout: 30_000 },
    );
    await page.getByTestId('direct-chat-shell').waitFor({ state: 'visible', timeout: 30_000 });
    await page.waitForFunction(
      (marker) => document.body.innerText.includes(marker),
      directMarker,
      { timeout: 60_000 },
    );
    const recoveredMessages = await pageApi(
      page,
      `/api/agents/${assistantAgentId}/sessions/${sessionPayload.id}/messages?limit=100`,
    );
    requireCondition(
      recoveredMessages.status === 200
        && Array.isArray(recoveredMessages.body)
        && recoveredMessages.body.some(
          (message) => message?.role === 'assistant'
            && typeof message?.content === 'string'
            && message.content.includes(directMarker),
        ),
      'ui_direct_chat_recovery',
      { status: recoveredMessages.status },
    );

    const postChatSummaryResult = await pageApi(page, '/api/subscription/summary');
    requireCondition(
      postChatSummaryResult.status === 200
        && Number.isFinite(postChatSummaryResult.body?.balance)
        && Number.isFinite(postChatSummaryResult.body?.available_balance)
        && Number.isFinite(postChatSummaryResult.body?.reserved)
        && Number.isFinite(postChatSummaryResult.body?.consumed_credits)
        && postChatSummaryResult.body.reserved === 0
        && postChatSummaryResult.body.available_balance === postChatSummaryResult.body.balance
        && postChatSummaryResult.body.consumed_credits > summary.consumed_credits,
      'ui_post_chat_credits',
      { code: 'direct_chat_credits_not_settled' },
    );
    const postChatSummary = postChatSummaryResult.body;

    await page.goto(`${frontendUrl}/account/subscription`, {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });
    await page.getByTestId('subscription-available-credits-value').waitFor({
      state: 'visible',
      timeout: 30_000,
    });
    await Promise.all([
      requireExactCreditsText(
        page.getByTestId('subscription-credits-usage-value'),
        `${formatCredits(postChatSummary.consumed_credits)} / ${formatCredits(postChatSummary.total_granted)}`,
        'post_chat_credits_usage',
      ),
      requireExactCreditsText(
        page.getByTestId('subscription-available-credits-value'),
        `${formatCredits(postChatSummary.available_balance)} / ${formatCredits(postChatSummary.balance)}`,
        'post_chat_available_credits',
      ),
    ]);
    requireCondition(new URL(page.url()).pathname === '/account/subscription', 'ui_final_url', {
      path: new URL(page.url()).pathname,
    });
    requireCondition(serverErrors.length === 0, 'ui_server_responses', serverErrors.slice(0, 10));
    requireCondition(consoleErrors.length === 0 && pageErrors.length === 0, 'ui_console_errors', {
      console_error_count: consoleErrors.length,
      page_error_count: pageErrors.length,
    });
    requireCondition(blockedOrigins.length === 0, 'ui_cross_origin_request', {
      origins: [...new Set(blockedOrigins)].slice(0, 10),
    });
    requireCondition(unexpectedPageCount === 0, 'ui_unexpected_popup', {
      count: unexpectedPageCount,
    });

    return {
      evidence_schema_version: 3,
      evidence_kind: 'release_business_browser',
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
      business_flow: {
        work: { task_visible: true },
        group: { group_visible: true, message_restored: true },
        topology: { completed_work_visible: true },
        direct_chat: {
          round_trip: true,
          durable_after_reload: true,
          message_count: directRoundTrip.message_count,
          assistant_count: directRoundTrip.assistant_count,
        },
        credits: {
          settled_after_chat: true,
          reserved_after: postChatSummary.reserved,
          consumed_delta_positive: postChatSummary.consumed_credits > summary.consumed_credits,
        },
      },
      checks: [
        'ui_release_identity_ok',
        'ui_tenant_login_ok',
        'ui_tenant_scope_ok',
        'ui_subscription_summary_api_ok',
        'ui_subscription_balance_rendered_ok',
        'ui_subscription_page_ok',
        'ui_work_task_visible_ok',
        'ui_group_persistence_ok',
        'ui_workforce_topology_ok',
        'ui_direct_chat_round_trip_ok',
        'ui_direct_chat_recovery_ok',
        'ui_post_chat_credits_settled_ok',
        'ui_no_console_error_ok',
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
    evidence_schema_version: 3,
    evidence_kind: 'release_business_browser',
    ok: false,
    stage: error?.stage ?? 'browser_smoke',
    detail: error?.detail ?? { name: error?.name ?? 'Error', message: error?.message ?? 'unknown' },
  };
  process.stderr.write(`${JSON.stringify(payload)}\n`);
  process.exitCode = 1;
}
