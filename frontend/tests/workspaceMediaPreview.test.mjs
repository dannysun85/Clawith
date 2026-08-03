import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const panel = readFileSync(
  new URL('../src/components/WorkspaceOperationPanel.tsx', import.meta.url),
  'utf8',
);

test('workspace audio preview only exposes the download guidance after a real media error', () => {
  assert.match(panel, /const \[audioPreviewError, setAudioPreviewError\] = useState\(false\)/);
  assert.match(panel, /<audio[\s\S]*?controls[\s\S]*?preload="metadata"[\s\S]*?onError=\{\(\) => setAudioPreviewError\(true\)\}/);
  assert.match(panel, /audioPreviewError &&/);
  assert.match(panel, /Audio preview is unavailable; download the file to review it\./);
  assert.doesNotMatch(panel, /<audio[\s\S]*?>\s*Your browser does not support audio playback\./);
});
