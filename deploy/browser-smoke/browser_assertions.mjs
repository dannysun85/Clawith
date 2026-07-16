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
