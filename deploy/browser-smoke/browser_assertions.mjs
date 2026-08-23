export async function waitForExactText(
  locator,
  expected,
  {
    timeoutMs = 30_000,
    pollIntervalMs = 100,
    now = () => Date.now(),
    sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  } = {},
) {
  const startedAt = now();
  let actual = '';
  while (now() - startedAt <= timeoutMs) {
    actual = (await locator.innerText()).trim();
    if (actual === expected) return actual;
    await sleep(pollIntervalMs);
  }
  const error = new Error('visible text did not converge before timeout');
  error.actual = actual;
  error.expected = expected;
  throw error;
}


const UUID_PATTERN = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const DIRECT_RUNTIME_STATE_PATH = new RegExp(
  `^/api/agents/${UUID_PATTERN}/sessions/${UUID_PATTERN}/runtime-state$`,
  'i',
);


export function partitionBrowserIssues({ consoleErrors, httpErrors }) {
  const runtimeConflictBudget = new Map();
  const toleratedHttpErrors = [];
  const unexpectedHttpErrors = [];

  for (const issue of httpErrors) {
    if (
      toleratedHttpErrors.length === 0
      && issue?.status === 409
      && DIRECT_RUNTIME_STATE_PATH.test(issue?.path || '')
    ) {
      toleratedHttpErrors.push(issue);
      runtimeConflictBudget.set(
        issue.path,
        (runtimeConflictBudget.get(issue.path) || 0) + 1,
      );
    } else {
      unexpectedHttpErrors.push(issue);
    }
  }

  const toleratedConsoleErrors = [];
  const unexpectedConsoleErrors = [];
  for (const issue of consoleErrors) {
    const remaining = runtimeConflictBudget.get(issue?.source_path) || 0;
    if (
      issue?.category === 'failed_resource'
      && issue?.http_status === 409
      && DIRECT_RUNTIME_STATE_PATH.test(issue?.source_path || '')
      && remaining > 0
    ) {
      toleratedConsoleErrors.push(issue);
      runtimeConflictBudget.set(issue.source_path, remaining - 1);
    } else {
      unexpectedConsoleErrors.push(issue);
    }
  }

  return {
    toleratedConsoleErrors,
    toleratedHttpErrors,
    unexpectedConsoleErrors,
    unexpectedHttpErrors,
  };
}
