import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const readLocale = (name) => JSON.parse(readFileSync(
  new URL(`../src/i18n/${name}.json`, import.meta.url),
  'utf8',
));

const leafPaths = (value, prefix = '') => Object.entries(value).flatMap(([key, child]) => {
  const path = prefix ? `${prefix}.${key}` : key;
  return child && typeof child === 'object'
    ? leafPaths(child, path)
    : [path];
});

const getPath = (value, path) => path.split('.').reduce(
  (current, segment) => current?.[segment],
  value,
);

test('v1.11 Groups and Experience Library have complete Chinese namespaces', () => {
  const en = readLocale('en');
  const zh = readLocale('zh');

  for (const namespace of ['groups', 'experience']) {
    assert.ok(zh[namespace], `${namespace} is missing from zh.json`);
    for (const path of leafPaths(en[namespace])) {
      const translated = getPath(zh[namespace], path);
      assert.equal(typeof translated, 'string', `missing zh.${namespace}.${path}`);
      assert.notEqual(translated.trim(), '', `empty zh.${namespace}.${path}`);
    }
  }

  assert.equal(zh.groups.title, '群聊');
  assert.equal(zh.experience.feedTitle, '团队经验库');
  assert.equal(zh.experience.nav.team, '团队经验');
});
