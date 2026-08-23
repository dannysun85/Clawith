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
  let toleratedHttpIndex = -1;
  let toleratedConsoleIndex = -1;

  for (let httpIndex = 0; httpIndex < httpErrors.length; httpIndex += 1) {
    const httpIssue = httpErrors[httpIndex];
    if (
      httpIssue?.status !== 409
      || !DIRECT_RUNTIME_STATE_PATH.test(httpIssue?.path || '')
    ) {
      continue;
    }

    const consoleIndex = consoleErrors.findIndex((consoleIssue) => (
      consoleIssue?.category === 'failed_resource'
      && consoleIssue?.http_status === httpIssue.status
      && consoleIssue?.source_path === httpIssue.path
      && DIRECT_RUNTIME_STATE_PATH.test(consoleIssue.source_path)
    ));
    if (consoleIndex >= 0) {
      toleratedHttpIndex = httpIndex;
      toleratedConsoleIndex = consoleIndex;
      break;
    }
  }

  const toleratedHttpErrors = httpErrors.filter((_, index) => index === toleratedHttpIndex);
  const unexpectedHttpErrors = httpErrors.filter((_, index) => index !== toleratedHttpIndex);
  const toleratedConsoleErrors = consoleErrors.filter(
    (_, index) => index === toleratedConsoleIndex,
  );
  const unexpectedConsoleErrors = consoleErrors.filter(
    (_, index) => index !== toleratedConsoleIndex,
  );

  return {
    toleratedConsoleErrors,
    toleratedHttpErrors,
    unexpectedConsoleErrors,
    unexpectedHttpErrors,
  };
}
