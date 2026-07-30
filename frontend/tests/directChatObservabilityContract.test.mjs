import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  formatRuntimeErrorDiagnostics,
  normalizeRuntimeError,
  runtimeErrorDisablesReconnect,
} from '../src/services/runtimeError.ts';

const source = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);
const runtimeErrorSource = readFileSync(
  new URL('../src/services/runtimeError.ts', import.meta.url),
  'utf8',
);

test('direct chat reattaches an active run from its last durable event cursor', () => {
  assert.match(source, /type: 'attach_run'/);
  assert.match(source, /runtimeEventCursorRef/);
  assert.match(source, /event_cursor/);
  assert.match(source, /run_id: active\.runId/);
});

test('replayed tool packets keep one row by stable tool call id', () => {
  assert.match(source, /msg\.toolCallId === toolMsg\.toolCallId/);
  assert.match(source, /existing\.toolStatus === 'done' && toolMsg\.toolStatus === 'running'/);
  assert.match(source, /toolCallId: message\.toolCallId/);
  assert.match(source, /toolCallId: m\.toolCallId/);
});

test('an authoritative active run keeps a thinking indicator visible after reload', () => {
  assert.match(source, /\['queued', 'running'\]\.includes\(activeRun\.status\)/);
  assert.match(source, /showDirectRunThinking/);
  assert.match(source, /\{showDirectRunThinking && \(/);
  assert.match(source, /lastChatMessage\.toolStatus === 'running'/);
});

test('optional task history is discovered before its content is requested', () => {
  assert.match(source, /const rootFiles = await fileApi\.list\(id!\)/);
  assert.match(source, /entry\.path === 'task_history\.md'/);
  assert.match(source, /return fileApi\.read\(id!, 'task_history\.md'\)/);
  assert.doesNotMatch(
    source,
    /fileApi\.read\(id!, 'task_history\.md'\)\.catch\(\(\) => null\)/,
  );
});
