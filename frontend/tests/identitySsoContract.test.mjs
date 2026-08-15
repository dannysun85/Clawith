import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const login = readFileSync(new URL('../src/pages/Login.tsx', import.meta.url), 'utf8');
const ssoEntry = readFileSync(new URL('../src/pages/SSOEntry.tsx', import.meta.url), 'utf8');
const orgTab = readFileSync(
  new URL('../src/pages/enterprise-settings/tabs/OrgTab.tsx', import.meta.url),
  'utf8',
);
const zh = JSON.parse(readFileSync(new URL('../src/i18n/zh.json', import.meta.url), 'utf8'));
const en = JSON.parse(readFileSync(new URL('../src/i18n/en.json', import.meta.url), 'utf8'));

test('public Google and GitHub are explained as existing-identity sign-in only', () => {
  assert.match(login, /auth\.publicOAuthSignInOnly/);
  assert.match(login, /Google\/GitHub only sign in an already linked account/);
  assert.equal(
    zh.auth.publicOAuthSignInOnly,
    'Google/GitHub 只用于登录已绑定账号，不会创建账号或加入公司。新用户请使用邮箱注册或公司 SSO。',
  );
  assert.match(en.auth.publicOAuthSignInOnly, /do not create an account or join a company/);
});

test('tenant SSO keeps JIT visibly member-only and has recoverable terminal states', () => {
  assert.match(login, /auth\.tenantSsoPolicy/);
  assert.match(login, /JIT, when enabled, creates ordinary members only/);
  assert.match(ssoEntry, /sso\.memberOnlyPolicy/);
  assert.match(ssoEntry, /ordinary member, never an admin or owner/);
  assert.match(ssoEntry, /navigate\('\/login', \{ replace: true \}\)/);
  assert.match(ssoEntry, /navigate\('\/', \{ replace: true \}\)/);
  assert.match(zh.sso.memberOnlyPolicy, /普通成员.*不会成为管理员或所有者/);
});

test('company administration reports readiness without claiming real IdP verification', () => {
  assert.match(orgTab, /existingProvider\?\.readiness/);
  assert.match(orgTab, /readiness\.operational_status/);
  assert.match(orgTab, /readiness\.sso_login/);
  assert.match(orgTab, /readiness\.jit_policy/);
  assert.match(orgTab, /provider_verification === 'local_emulator_configured'/);
  assert.match(orgTab, /Real Google Workspace verification is still pending/);
  assert.match(orgTab, /jit_provisioning_enabled/);
  assert.match(orgTab, /jit_allowed_domains/);
  assert.match(orgTab, /signed Google hd claim must both match exactly/);
  assert.doesNotMatch(orgTab, />\s*Active\s*</);
});
