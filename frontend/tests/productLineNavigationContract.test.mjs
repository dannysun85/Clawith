import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const layout = readFileSync(new URL('../src/pages/Layout.tsx', import.meta.url), 'utf8');
const productAccess = readFileSync(new URL('../src/utils/productAccess.ts', import.meta.url), 'utf8');
const saasAdminAccess = readFileSync(new URL('../src/utils/saasAdmin.ts', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
const companyAdmin = readFileSync(new URL('../src/pages/CompanyAdmin.tsx', import.meta.url), 'utf8');
const companyAccess = readFileSync(new URL('../src/pages/CompanyAccess.tsx', import.meta.url), 'utf8');
const accountCompanies = readFileSync(new URL('../src/pages/AccountCompanies.tsx', import.meta.url), 'utf8');
const platformOperations = readFileSync(new URL('../src/pages/PlatformOperations.tsx', import.meta.url), 'utf8');
const platformSystemEmail = readFileSync(new URL('../src/pages/PlatformSystemEmail.tsx', import.meta.url), 'utf8');
const productConsoleShell = readFileSync(new URL('../src/components/ProductConsoleShell.tsx', import.meta.url), 'utf8');
const productConsoleCss = readFileSync(new URL('../src/pages/productConsole.css', import.meta.url), 'utf8');
const indexCss = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const employees = readFileSync(new URL('../src/pages/Employees.tsx', import.meta.url), 'utf8');
const onboarding = readFileSync(new URL('../src/pages/Onboarding.tsx', import.meta.url), 'utf8');
const work = readFileSync(new URL('../src/pages/Work.tsx', import.meta.url), 'utf8');
const plaza = readFileSync(new URL('../src/pages/Plaza.tsx', import.meta.url), 'utf8');
const editor = readFileSync(new URL('../src/components/ExperienceDraftEditor.tsx', import.meta.url), 'utf8');
const agentDetail = readFileSync(new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url), 'utf8');
const deliverableWorkbench = readFileSync(new URL('../src/components/deliverables/DeliverableWorkbench.tsx', import.meta.url), 'utf8');
const douyinTab = readFileSync(new URL('../src/pages/agent-detail/tabs/DouyinTab.tsx', import.meta.url), 'utf8');
const enterpriseSettings = readFileSync(new URL('../src/pages/EnterpriseSettings.tsx', import.meta.url), 'utf8');
const productIa = JSON.parse(readFileSync(
  new URL('../../backend/app/data/product_information_architecture.v1.json', import.meta.url),
  'utf8',
));
const backendVersion = readFileSync(new URL('../../backend/VERSION', import.meta.url), 'utf8').trim();

test('the task workbench is the default entry while legacy routes remain available', () => {
  assert.match(app, /resolveProductEntry\(user, preferredSurface\)/);
  assert.match(productAccess, /work: '\/work'/);
  assert.match(productAccess, /primarySurfaces\.length > 1.*'\/choose-surface'/s);
  assert.match(app, /path="work"/);
  assert.match(app, /path="dashboard"/);
  assert.match(app, /path="employees"/);
  assert.match(app, /path="plaza"/);
  assert.match(onboarding, /navigate\('\/work'/);
  assert.match(onboarding, /createAssistant\(true\)/);
  assert.match(onboarding, /Skip for now, use defaults/);
});

test('navigation names communicate distinct product responsibilities', () => {
  assert.match(layout, /'工作台'/);
  assert.match(layout, /'团队'/);
  assert.match(layout, /'经营'/);
  assert.match(layout, /'管理'/);
  assert.match(layout, /'公司概览'/);
  assert.match(layout, /'目标与复盘'/);
  assert.match(layout, /'团队知识'/);
  assert.match(layout, /'公司管理'/);
  assert.match(layout, /'协作群组'/);
  assert.match(layout, /'我的助理'/);
  assert.match(layout, /历史助理/);
  assert.match(layout, /'数字员工'/);
  assert.match(layout, /data-tour-target="operations-nav"/);
  assert.match(layout, /data-tour-target="management-nav"/);
  assert.match(layout, /to="\/employees"/);
  assert.match(layout, /to="\/okr"/);
  assert.match(layout, /to="\/plaza"/);
  assert.match(layout, /to="\/company-admin"/);
  assert.doesNotMatch(layout, /'协作角色'|'发现中心'|'企业设置'/);
  assert.doesNotMatch(layout, /sortedAgents\.map/);
  assert.match(layout, /aria-label=\{isChinese \? '打开账户菜单' : 'Open account menu'\}/);
  assert.match(layout, /onClick=\{\(\) => setShowLanguageSubmenu\(true\)\}/);
  assert.match(layout, /langMenuTriggerRef/);
  assert.match(layout, /window\.innerWidth - menuWidth - viewportPadding/);
  assert.match(indexCss, /\.account-lang-submenu-portal[\s\S]*?z-index: 10050/);
});

test('platform SaaS authority follows the server-issued global role and capability', () => {
  assert.match(saasAdminAccess, /global_roles\?\.includes\('platform_operator'\)/);
  assert.match(saasAdminAccess, /effective_capabilities\?\.includes\('platform\.billing\.manage'\)/);
  assert.doesNotMatch(saasAdminAccess, /SAAS_ADMIN_EMAIL|VITE_SAAS_ADMIN_EMAIL|admin@reeftotem\.ai/);
  assert.match(platformOperations, /hasEffectiveCapability\(user, 'platform\.billing\.manage'\)/);
});

test('assistant navigation keeps one stable relationship and hides compatibility history by default', () => {
  assert.match(layout, /useState\(false\).*legacyAssistantsOpen|legacyAssistantsOpen.*useState\(false\)/s);
  assert.match(layout, /label: isChinese \? '我的助理' : 'My assistant'/);
  assert.match(layout, /subtitle: personalAssistant\.name/);
  assert.match(layout, /className="sidebar-legacy-toggle"/);
  assert.match(layout, /aria-expanded=\{legacyAssistantsOpen\}/);
  assert.match(layout, /legacyAssistantsOpen && !isSidebarCollapsed/);
});

test('digital employees own one center with network, full directory, and one hiring entry', () => {
  assert.match(employees, /'network' \| 'directory'/);
  assert.match(employees, /'available' \| 'managed' \| 'governance'/);
  assert.match(employees, /<WorkforceTopology topology=\{scopedTopology\}/);
  assert.match(employees, /topology=\{scopedTopology\}/);
  assert.match(employees, /openTalentMarket/);
  assert.match(layout, /view=directory&highlight=/);
  assert.match(employees, /node\.can_manage/);
  assert.match(employees, /'agent\.manage\.company'/);
  assert.match(employees, /\/settings#settings/);
});

test('company creation and member onboarding are separate recoverable product stages', () => {
  assert.match(companyAccess, /companyAuthorityConfirmed/);
  assert.match(companyAccess, /timezone: companyTimezone/);
  assert.match(companyAccess, /country_region: companyRegion/);
  assert.match(companyAccess, /commitSameOriginTenantSwitch/);
  assert.match(onboarding, /'company' \| 'profile' \| 'assistant' \| 'opening'/);
  assert.match(onboarding, /onboardingApi\.initializeCompany/);
  assert.match(onboarding, /onboardingApi\.completeProfile/);
  assert.match(onboarding, /允许普通成员创建私有 Agent/);
  assert.match(onboarding, /私人助理属于你在这家公司的成员身份/);
  assert.match(onboarding, /Provider、模型、Skill 和 Tool 稍后/);
  assert.doesNotMatch(onboarding, /setProvider|setModel|selectSkill|selectTool/);
});

test('membership changes validate scoped tokens and leave no legacy join modal', () => {
  assert.match(accountCompanies, /fallback_tenant_id && result\.access_token/);
  assert.match(accountCompanies, /commitSameOriginTenantSwitch/);
  assert.match(accountCompanies, /tenantApi\.leavePreflight/);
  assert.match(accountCompanies, /owned_agents/);
  assert.match(accountCompanies, /确认并退出公司/);
  assert.match(accountCompanies, /个人凭证失效/);
  assert.match(accountCompanies, /tenant\.membership_role/);
  assert.doesNotMatch(accountCompanies, /tenant\.role \|\| tenant\.membership_role/);
  assert.match(companyAdmin, /fallback_tenant_id && result\.access_token/);
  assert.match(companyAdmin, /commitSameOriginTenantSwitch/);
  assert.match(companyAdmin, /membershipApi\.deactivationPreflight/);
  assert.match(companyAdmin, /私人 Agent 内容不会向管理员开放/);
  assert.match(companyAdmin, /navigate\('\/account\/companies'\)/);
  assert.doesNotMatch(layout, /handleModalJoin|handleModalCreate|tenant-setup-modal/);
});

test('agent ownership handover is explicit and keeps private assistants non-transferable', () => {
  assert.match(agentDetail, /Agent 所有权交接/);
  assert.match(agentDetail, /\/agents\/\$\{agentId\}\/handover/);
  assert.match(agentDetail, /new_creator_id: handoverTargetId/);
  assert.match(agentDetail, /productRole === 'personal_assistant'/);
  assert.match(agentDetail, /私人助理包含个人上下文，不能转交/);
  assert.match(agentDetail, /canForceHandover=\{currentUser\?\.membership_role === 'org_owner'/);
});

test('agent detail preserves the server-owned product role instead of guessing from names', () => {
  assert.match(agentDetail, /agent\.product_role \|\| 'agent_employee'/);
  assert.match(agentDetail, /data-testid="agent-product-role"/);
  assert.match(agentDetail, /'legacy_personal_assistant'/);
  assert.doesNotMatch(agentDetail, /role_description.*legacy_personal_assistant/);
});

test('company administration routes stay behind the company-admin boundary', () => {
  assert.match(
    app,
    /path="\/company-admin\/\*" element={<ProtectedRoute><CompanyAdminRoute><CompanyAdmin \/><\/CompanyAdminRoute><\/ProtectedRoute>}/,
  );
  assert.match(
    app,
    /path="\/enterprise" element={<ProtectedRoute><CompanyAdminRoute><LegacyCompanyAdminRedirect \/><\/CompanyAdminRoute><\/ProtectedRoute>}/,
  );
  assert.match(app, /hasProductSurface\(user, 'company_admin'\)/);
  assert.match(companyAdmin, /'company\.members\.view'/);
  assert.match(companyAdmin, /'company\.ownership\.transfer'/);
  assert.match(companyAdmin, /organizationInvitations/);
  assert.match(companyAdmin, /requestOwnershipTransfer/);
  assert.match(companyAdmin, /allow_member_private_agents/);
  assert.match(companyAdmin, /default_approval_policy/);
  assert.match(companyAdmin, /发送邮箱邀请/);
  assert.match(companyAdmin, /SMTP 已接受（不代表对方已读）/);
  assert.match(companyAdmin, /issueOrganizationInvitationManualLink/);
  assert.match(companyAdmin, /current-password/);
  assert.match(companyAdmin, /旧链接已失效并重新受理/);
  assert.match(api, /'Idempotency-Key': idempotencyKey/);
  assert.doesNotMatch(
    companyAdmin,
    /createOrganizationInvitation[\s\S]{0,500}result\.token/,
  );
});

test('knowledge and integrations keeps every governed secondary capability reachable', () => {
  assert.match(companyAdmin, /type IntegrationView = 'tools' \| 'skills' \| 'org' \| 'douyin'/);
  assert.match(companyAdmin, /企业知识与集成二级功能/);
  assert.match(companyAdmin, /\/company-admin\/integrations\/\$\{tab\.key\}/);
  assert.match(companyAdmin, /initialTab=\{integrationView\}/);
  assert.match(app, /tools: 'integrations\/tools'/);
  assert.match(app, /skills: 'integrations\/skills'/);
  assert.match(app, /org: 'integrations\/org'/);
  assert.match(app, /douyin: 'integrations\/douyin'/);
  assert.match(deliverableWorkbench, /href="\/company-admin\/integrations\/org"/);
  assert.match(douyinTab, /window\.location\.href = '\/company-admin\/integrations\/douyin'/);
  assert.match(enterpriseSettings, /redirect_after: '\/company-admin\/integrations\/douyin'/);
  assert.doesNotMatch(deliverableWorkbench, /\/enterprise#org/);
  assert.doesNotMatch(douyinTab, /\/enterprise#douyin/);
  assert.doesNotMatch(enterpriseSettings, /redirect_after: '\/enterprise#douyin'/);
});

test('the runtime product catalog stays aligned with public navigation and has no invented report center', () => {
  assert.equal(productIa.version, 1);
  assert.equal(productIa.catalog_id, 'astra-product-ia-1.12.2-r1');
  assert.match(productIa.catalog_id, new RegExp(`-${backendVersion.replaceAll('.', '\\.')}-`));
  const entries = new Map(productIa.entries.map((entry) => [entry.id, entry]));
  const expected = {
    work: ['/work', '工作台'],
    groups: ['/groups', '协作群组'],
    employees: ['/employees', '数字员工'],
    dashboard: ['/dashboard', '公司概览'],
    okr: ['/okr', '目标与复盘'],
    team_knowledge: ['/plaza', '团队知识'],
    company_admin: ['/company-admin', '公司管理'],
  };
  for (const [id, [route, label]] of Object.entries(expected)) {
    assert.equal(entries.get(id)?.route, route);
    assert.equal(entries.get(id)?.breadcrumbs?.['zh-CN']?.[0], label);
    assert.match(`${app}\n${layout}`, new RegExp(route.replaceAll('/', '\\/')));
    assert.match(layout, new RegExp(label));
  }
  for (const id of [
    'company_integration_tools',
    'company_integration_skills',
    'company_integration_org',
    'company_integration_accounts',
  ]) {
    const entry = entries.get(id);
    assert.ok(entry);
    assert.match(companyAdmin, new RegExp(entry.breadcrumbs['zh-CN'].at(-1)));
  }
  assert.match(companyAdmin, /\/company-admin\/integrations\/\$\{tab\.key\}/);
  assert.doesNotMatch(JSON.stringify(productIa), /报告中心|Report center/i);
});

test('platform operations use an independent shell and separated registration grants', () => {
  assert.match(app, /path="\/admin\/platform\/\*"/);
  assert.match(app, /hasProductSurface\(user, 'platform_admin'\)/);
  assert.match(platformOperations, /kind="platform"/);
  assert.match(platformOperations, /platform\.registration\.manage/);
  assert.match(platformOperations, /createRegistrationGrants/);
  assert.match(platformOperations, /createSupportSession/);
  assert.match(platformOperations, /supportTenantSummary/);
  assert.match(platformOperations, /data-testid="support-tenant-summary"/);
  assert.match(platformOperations, /data-testid="tenant-purge-queue"/);
  assert.match(platformOperations, /执行无删除 dry-run/);
  assert.match(platformOperations, /物理清理由受控执行器处理，不暴露网页按钮/);
  assert.match(platformOperations, /createTenantDeletionHold/);
  assert.match(platformOperations, /releaseTenantDeletionHold/);
  assert.match(api, /'\/auth\/register\/init'/);
  assert.match(api, /\/admin\/tenant-deletions\/\$\{tenantId\}\/dry-run/);
  assert.doesNotMatch(platformOperations, /executeTenantPurge|物理删除按钮/);
  assert.match(platformOperations, /不返回成员身份明细、Agent 内容、消息、附件或 Workspace 文件/);
  assert.match(api, /support-sessions\/\$\{sessionId\}\/tenants\/\$\{tenantId\}\/summary/);
  assert.match(platformOperations, /support never grants access|支持会话不授予/);
  assert.doesNotMatch(
    platformOperations,
    /to: '\/employees'|to="\/employees"|to: '\/groups'|to="\/groups"|to: '\/assistant'|to="\/assistant"/,
  );
});

test('product governance consoles keep security and logout reachable on mobile', () => {
  assert.match(productConsoleShell, /product-console__mobile-account/);
  assert.match(productConsoleShell, /aria-label="移动端账号操作"/);
  assert.match(productConsoleCss, /\.product-console__mobile-account \{ display: flex; \}/);
});

test('platform system email remains reachable, secret-safe, and testable', () => {
  assert.match(platformOperations, /\/admin\/platform\/system-email/);
  assert.match(platformOperations, /section === 'system-email'/);
  assert.match(platformOperations, /platform\.registration\.manage/);
  assert.match(platformSystemEmail, /\/enterprise\/system-settings\/system_email_platform/);
  assert.match(platformSystemEmail, /method: 'PUT'/);
  assert.match(platformSystemEmail, /\/enterprise\/system-email\/test/);
  assert.match(platformSystemEmail, /CONFIGURED_SECRET_PLACEHOLDER/);
  assert.match(platformSystemEmail, /type="password"/);
  assert.match(platformSystemEmail, /SMTP 服务器已接受/);
  assert.match(platformSystemEmail, /不代表对方已收件或已读/);
  assert.match(platformSystemEmail, /evidence_level: 'smtp_accepted'/);
  assert.match(platformSystemEmail, /savedConfigurationReady/);
  assert.match(platformSystemEmail, /请先保存完整 SMTP 配置/);
  assert.doesNotMatch(platformSystemEmail, /console\.(?:log|debug|info)\(/);
});

test('the workbench proposes an explainable executor while keeping manual override advanced', () => {
  assert.match(work, /routingMode/);
  assert.match(work, /executorProposal/);
  assert.match(work, /advancedExecutor/);
  assert.match(work, /personal_assistant/);
  assert.match(work, /agent_employee/);
  assert.match(work, /系统已选择|System selected/);
  assert.doesNotMatch(work, /name="provider"|name="model"|setProvider|setModel/);
  assert.match(work, /delivery_mode === 'formal_deliverable'/);
  assert.match(work, /本任务只整理 brief/);
  assert.doesNotMatch(work, /zh: '商业图片'|zh: '人物视频'|zh: '精美 PPT'/);
});

test('discover separates experience from hiring and preserves work provenance', () => {
  assert.match(plaza, /'experience' \| 'talent'/);
  assert.match(plaza, /openTalentMarket/);
  assert.match(plaza, /source_task_id: params\.get\('task'\)/);
  assert.match(plaza, /source_deliverable_request_id: params\.get\('delivery'\)/);
  assert.match(editor, /source_task_id: form\.source_task_id/);
  assert.match(editor, /source_deliverable_request_id: form\.source_deliverable_request_id/);
  assert.match(work, /item\.user_stage === 'delivery'/);
});
