import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const companyAdmin = readFileSync(new URL('../src/pages/CompanyAdmin.tsx', import.meta.url), 'utf8');
const enterpriseSettings = readFileSync(new URL('../src/pages/EnterpriseSettings.tsx', import.meta.url), 'utf8');

test('embedded company settings keep one source of truth for name and region', () => {
  assert.match(companyAdmin, /<EnterpriseSettings initialTab="info" embedded \/>/);
  assert.match(enterpriseSettings, /!embedded && <CompanyNameEditor/);
  assert.match(enterpriseSettings, /!embedded && <CompanyTimezoneEditor/);
});

test('the embedded settings surface cannot bypass recoverable company deletion', () => {
  assert.match(
    enterpriseSettings,
    /company console owns the recoverable[\s\S]*?\{!embedded && <div[\s\S]*?Danger Zone/,
  );
  assert.match(companyAdmin, /tenantApi\.scheduleDeletion/);
  assert.match(companyAdmin, /30 天可恢复停用期/);
});
