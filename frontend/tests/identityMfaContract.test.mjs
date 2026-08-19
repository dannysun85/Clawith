import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const login = readFileSync(new URL('../src/pages/Login.tsx', import.meta.url), 'utf8');
const security = readFileSync(new URL('../src/pages/AccountSecurity.tsx', import.meta.url), 'utf8');
const qrCode = readFileSync(new URL('../src/components/MfaQrCode.tsx', import.meta.url), 'utf8');
const companyAdmin = readFileSync(new URL('../src/pages/CompanyAdmin.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');

test('account security has one authenticated route and a complete MFA API surface', () => {
  assert.match(app, /path="\/account\/security" element={<ProtectedRoute><AccountSecurity \/><\/ProtectedRoute>}/);
  assert.match(api, /mfaStatus: \(\) => request<MfaStatus>\('\/auth\/mfa\/status'\)/);
  assert.match(api, /'\/auth\/mfa\/setup'/);
  assert.match(api, /'\/auth\/mfa\/bootstrap\/setup'/);
  assert.match(api, /'\/auth\/mfa\/setup\/confirm'/);
  assert.match(api, /'\/auth\/mfa\/challenge\/verify'/);
  assert.match(api, /'\/auth\/mfa\/recovery-codes\/rotate'/);
  assert.match(api, /'\/auth\/mfa\/disable'/);
  assert.match(api, /`\/auth\/mfa\/admin\/reset\/\$\{userId\}`/);
  assert.match(api, /url\.startsWith\('\/auth\/mfa\/'\)/);
  assert.match(api, /apiError\.code === 'mfa_setup_required'.*apiError\.code === 'mfa_challenge_required'/s);
  assert.match(api, /`\/account\/security\?reason=\$\{encodeURIComponent\(apiError\.code\)\}`/);
});

test('login keeps MFA challenge, setup, and recovery acknowledgement inside one guarded flow', () => {
  assert.match(login, /stage: 'verify' \| 'setup' \| 'recovery'/);
  assert.match(login, /candidate\.requires_mfa === true \|\| candidate\.requires_mfa_setup === true/);
  assert.match(login, /authApi\.startMfaBootstrap\(challenge\.challenge_token\)/);
  assert.match(login, /authApi\.confirmMfaSetup\(mfaFlow\.challengeToken, mfaCode\.trim\(\)\)/);
  assert.match(login, /authApi\.verifyMfaChallenge\(mfaFlow\.challengeToken, mfaCode\.trim\(\)\)/);
  assert.match(login, /if \(!mfaFlow\?\.tokenResponse \|\| !mfaCodesSaved\) return/);
  assert.match(login, /disabled={!mfaCodesSaved \|\| loading}/);
  assert.match(login, /navigator\.clipboard\.writeText\(mfaFlow\.recoveryCodes\.join\('\\n'\)\)/);
  assert.match(login, /这是首次绑定，不是系统替你设置过/);
  assert.match(login, /inputMode={mfaFlow\.stage === 'setup' \? 'numeric' : 'text'}/);
  assert.match(login, /maxLength={mfaFlow\.stage === 'setup' \? 6 : 64}/);
  assert.doesNotMatch(login, /localStorage\.setItem\([^\n]*(?:secret|provisioning|recovery)/i);
});

test('MFA enrollment renders an in-memory QR code with an explicit manual fallback', () => {
  assert.match(qrCode, /toDataURL\(provisioningUri/);
  assert.match(qrCode, /alt={isChinese \? '多因素验证器绑定二维码'/);
  assert.match(qrCode, /<details className="mfa-enrollment__manual">/);
  assert.match(qrCode, /二维码和密钥只保留在当前页面内存中，不会写入浏览器存储/);
  assert.doesNotMatch(qrCode, /localStorage|sessionStorage|indexedDB/);
  assert.match(login, /<MfaQrCode/);
  assert.match(security, /<MfaQrCode/);
});

test('identity security explains its global boundary and protects sensitive mutations', () => {
  assert.match(security, /MFA 绑定到自然人 Identity，而不是某一家公司的 membership/);
  assert.match(security, /authApi\.startMfaSetup\(setupPassword\)/);
  assert.match(security, /authApi\.rotateMfaRecoveryCodes\(mutationPassword, mutationCode\.trim\(\)\)/);
  assert.match(security, /if \(!user \|\| !status\) return/);
  assert.match(security, /account-security__danger[\s\S]{0,200}关闭 MFA/);
  assert.match(security, /公司管理员仍可关闭 MFA/);
  assert.match(security, /disabled={!codesSaved}/);
  assert.match(security, /不能再次查看明文|Plaintext cannot be viewed again/);
});

test('company administrators can reset only eligible ordinary members with reauthentication and reason', () => {
  assert.match(companyAdmin, /member\.id !== user\?\.id && member\.role === 'member' && member\.is_active && member\.mfa_enabled/);
  assert.match(companyAdmin, /membershipApi\.resetMfa\(mfaResetTarget\.id/);
  assert.match(companyAdmin, /current_password: mfaResetPassword/);
  assert.match(companyAdmin, /reason: mfaResetReason\.trim\(\)/);
  assert.match(companyAdmin, /全部旧会话与恢复码已失效/);
  assert.match(companyAdmin, /若该 Identity 还属于其他公司，服务端会拒绝并要求平台运营处理/);
});
