import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { createRandomUUID } from '../src/utils/randomUUID.ts';

test('uses the native randomUUID implementation when available', () => {
  let getRandomValuesCalled = false;
  const value = createRandomUUID({
    randomUUID: () => '11111111-2222-4333-8444-555555555555',
    getRandomValues: () => {
      getRandomValuesCalled = true;
      throw new Error('fallback should not run');
    },
  });

  assert.equal(value, '11111111-2222-4333-8444-555555555555');
  assert.equal(getRandomValuesCalled, false);
});

test('builds an RFC 4122 v4 UUID when randomUUID is unavailable', () => {
  const value = createRandomUUID({
    getRandomValues: (bytes) => {
      bytes.set(Array.from({ length: 16 }, (_, index) => index));
      return bytes;
    },
  });

  assert.equal(value, '00010203-0405-4607-8809-0a0b0c0d0e0f');
});

test('fails explicitly when no secure random source exists', () => {
  assert.throws(
    () => createRandomUUID(null),
    /secure random source is unavailable/i,
  );
});

test('frontend call sites use the compatibility helper instead of crypto.randomUUID', () => {
  const srcRoot = fileURLToPath(new URL('../src', import.meta.url));
  const unsupportedCallSites = [];

  function inspectDirectory(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        inspectDirectory(path);
      } else if (/\.(?:ts|tsx)$/.test(entry.name)) {
        const source = readFileSync(path, 'utf8');
        if (/\bcrypto\.randomUUID\s*\(/.test(source)) {
          unsupportedCallSites.push(path.slice(srcRoot.length + 1));
        }
      }
    }
  }

  inspectDirectory(srcRoot);
  assert.deepEqual(unsupportedCallSites, []);
});
