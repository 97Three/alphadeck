<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import DataList from '../components/DataList.vue';
import EvalManager from '../components/EvalManager.vue';
import AccountExtras from '../components/AccountExtras.vue';
import AdminAccess from '../components/AdminAccess.vue';
import ResearchReports from '../components/ResearchReports.vue';
import OpsCenter from '../components/OpsCenter.vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ module: string }>();
const route = useRoute();
const router = useRouter();
const definitions: Record<string, { title: string; intro: string; endpoint: string; list: string; label: string }> = {
  reports: { title: '投研报告', intro: '观察池、每日研究报告与推送记录。', endpoint: '/api/research/reports', list: 'latestReports', label: '报告' },
  knowledge: { title: '业务知识', intro: '查看项目支持的研究能力、数据来源和交付范围。', endpoint: '/api/quant/capability-center', list: 'capabilities', label: '能力' },
  skills: { title: '能力中心', intro: '查看技能版本、健康状态与发布记录。', endpoint: '/api/skills', list: 'skills', label: '技能' },
  eval: { title: '评测平台', intro: '用评测集回归验证研究任务与看板质量。', endpoint: '/api/evals', list: 'cases', label: '用例' },
  ops: { title: '运行治理', intro: '查看服务、Worker、工作空间和生成链路的状态。', endpoint: '/api/ops/platform', list: 'serviceCatalog', label: '服务' },
  users: { title: '用户管理', intro: '查看成员与权限、创建账号并检查审计记录。', endpoint: '/api/admin/users', list: 'users', label: '用户' },
  account: { title: '我的账号', intro: '用量、个人记忆与账号安全。', endpoint: '/api/account/usage', list: 'usage', label: '记录' },
};
const section = computed(() => String(route.params.section || 'usage'));
const endpoint = computed(() => props.module === 'account' ? section.value === 'memory' ? '/api/account/memory' : section.value === 'security' ? '/api/account/sessions' : '/api/account/usage' : props.module === 'skills' && studio.value ? '/api/skills?view=studio' : definitions[props.module]?.endpoint || '');
const data = ref<Row>({});
const loading = ref(true);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const selected = ref<Row | null>(null);
const selectedId = ref('');
const studio = ref(false);
const editor = ref('');
const skillPath = ref('SKILL.md');
const skillVersion = ref('');
const skillSummary = ref('');
const skillRollbackVersion = ref('');
const evalMode = ref<'contract' | 'e2e'>('contract');
const evalLimit = ref(3);
const evalModel = ref('');
const evalEvaluator = ref('');
const evalConcurrency = ref(1);
const evalRepeat = ref(1);
const evalKeepProjects = ref(false);
const evalSetId = ref('');
const evalSets = computed<Row[]>(() => Array.isArray(data.value.customEvalSets) ? data.value.customEvalSets as Row[] : []);
const userName = ref('');
const userEmail = ref('');
const userPassword = ref('');
const userRole = ref('member');
const oldPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const detail = ref<unknown>(null);
const detailTitle = ref('');
const list = computed<Row[]>(() => {
  const source = data.value[definitions[props.module]?.list || ''];
  return Array.isArray(source) ? source.filter(item => item && typeof item === 'object') as Row[] : [];
});
const summary = computed(() => data.value.summary && typeof data.value.summary === 'object' ? data.value.summary as Row : data.value.totals && typeof data.value.totals === 'object' ? data.value.totals as Row : {});
const visibleSummary = computed<Row>(() => {
  const keys = props.module === 'ops' ? ['score', 'ok', 'warning', 'failed'] : props.module === 'eval' ? ['caseCount', 'reportCount', 'capabilityCount'] : props.module === 'skills' ? ['total', 'ok', 'warning'] : props.module === 'knowledge' ? ['capabilities', 'readyCapabilities', 'plannedCapabilities', 'availableProviders', 'degradedProviders'] : null;
  return keys ? Object.fromEntries(keys.filter(key => summary.value[key] != null).map(key => [key, summary.value[key]])) : summary.value;
});
const summaryLabels: Record<string, string> = { watchlists: '观察池', reports: '研究报告', activeChannels: '可用推送渠道', latestScore: '最新评分', caseCount: '评测用例', reportCount: '评测报告', capabilityCount: '研究能力', capabilities: '研究能力', readyCapabilities: '可使用', plannedCapabilities: '规划中', availableProviders: '可用数据源', degradedProviders: '降级数据源', latestPassRate: '最近通过率', latestAverageScore: '最近平均分', latestPassedCount: '通过用例', latestFailedCount: '失败用例', latestTotal: '最近用例数', skills: '技能', healthy: '健康技能', warnings: '待检查', total: '已注册技能', score: '健康评分', ok: '正常', warning: '需关注', failed: '阻断项' };
const title = (item: Row) => String(item.name || item.title || item.label || item.id || item.symbol || '未命名');
const short = (value: unknown) => value == null || value === '' ? '—' : Array.isArray(value) ? `${value.length} 项` : typeof value === 'object' ? `${Object.keys(value).length} 项` : String(value);
const status = (item: Row) => String(item.status || (item.health && typeof item.health === 'object' ? (item.health as Row).status : '') || item.typeLabel || '');

async function load() {
  if (!endpoint.value) return;
  loading.value = true; error.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<Row>>(endpoint.value));
    data.value = result && typeof result === 'object' ? result : {};
    selected.value = list.value.find(item => String(item.id || '') === selectedId.value) || list.value[0] || null;
    selectedId.value = String(selected.value?.id || '');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取失败'; }
  finally { loading.value = false; }
}
function choose(item: Row) { selected.value = item; selectedId.value = String(item.id || ''); skillRollbackVersion.value = ''; detail.value = null; }
async function mutate(path: string, payload: Row, method = 'POST') {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<unknown>>(path, { method, body: JSON.stringify(payload) }));
    detail.value = result; detailTitle.value = '操作结果'; notice.value = '操作完成';
    await load(); return result;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败'; return null; }
  finally { busy.value = false; }
}
async function evalAction(action: string, extra: Row = {}) {
  const selected = evalSets.value.find(item => item.id === evalSetId.value);
  await mutate('/api/evals', { action, mode: evalMode.value, limit: evalLimit.value, model: evalModel.value || undefined, evaluatorId: evalEvaluator.value || undefined, concurrency: evalConcurrency.value, repeat: evalRepeat.value, keepProjects: evalKeepProjects.value, selectedCases: selected?.caseIds || [], ...extra });
}
async function showDetail(path: string, label: string) {
  busy.value = true; error.value = '';
  try { detail.value = unwrap(await apiRequest<ApiEnvelope<unknown>>(path)); detailTitle.value = label; }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '详情读取失败'; }
  finally { busy.value = false; }
}
async function readSkill() {
  if (!selectedId.value) return;
  busy.value = true; error.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<Row>>('/api/skills', { method: 'POST', body: JSON.stringify({ action: 'read-file', skillId: selectedId.value, filePath: skillPath.value }) }));
    editor.value = String(result?.content || result?.source || '');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取技能文件失败'; }
  finally { busy.value = false; }
}
async function skillAction(action: string, extra: Row = {}) {
  const editing = selected.value?.editing as Row | undefined;
  await mutate('/api/skills', { action, skillId: selectedId.value, expectedRevision: editing?.revision || data.value.catalogRevision || '', ...extra });
}
async function saveSkill() { await skillAction('save-file', { filePath: skillPath.value, content: editor.value }); }
async function publishSkill() { if (!skillVersion.value || !skillSummary.value) return; await skillAction('publish-version', { version: skillVersion.value, summary: skillSummary.value, changes: [skillSummary.value] }); }
async function rollbackSkill() {
  if (!skillRollbackVersion.value || !window.confirm(`将 ${selectedId.value} 恢复到 v${skillRollbackVersion.value}？此操作会改动平台技能版本。`)) return;
  await skillAction('rollback-version', { version: skillRollbackVersion.value });
}
async function uploadSkill(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file || !selectedId.value) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    const form = new FormData();
    form.append('action', 'upload-package');
    form.append('skillId', selectedId.value);
    form.append('expectedRevision', String((selected.value?.editing as Row | undefined)?.revision || data.value.catalogRevision || ''));
    form.append('file', file);
    detail.value = unwrap(await apiRequest<ApiEnvelope<unknown>>('/api/skills', { method: 'POST', body: form }));
    detailTitle.value = '上传结果'; notice.value = '技能包已上传'; await load();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '技能包上传失败'; }
  finally { busy.value = false; input.value = ''; }
}
async function createUser() { await mutate('/api/admin/users', { name: userName.value, email: userEmail.value, password: userPassword.value, role: userRole.value }); userPassword.value = ''; }
async function updateUser(action: string, extra: Row = {}) { if (!selectedId.value) return; await mutate('/api/admin/users', { userId: selectedId.value, action, ...extra }, 'PATCH'); }
async function changePassword() { await mutate('/api/account/password', { currentPassword: oldPassword.value, newPassword: newPassword.value, confirmPassword: confirmPassword.value }); oldPassword.value = ''; newPassword.value = ''; confirmPassword.value = ''; }
async function updateMemory(enabled: boolean) { await mutate('/api/account/memory', { personalizationEnabled: enabled }, 'PUT'); }
function toLogin() { void router.push('/login'); }
watch([() => props.module, section, studio], load);
onMounted(load);
</script>

<template>
  <main class="module-page workspace-main">
    <div class="breadcrumb">AlphaDeck　/　{{ definitions[module]?.title }}</div>
    <div class="module-heading"><div><div class="eyebrow">WORKSPACE</div><h1>{{ definitions[module]?.title }}</h1><p>{{ definitions[module]?.intro }}</p></div><button type="button" class="secondary-button" @click="load">刷新</button></div>
    <p v-if="error" class="inline-error" role="alert">{{ error }} <button v-if="error.includes('登录') || error.includes('401')" type="button" @click="toLogin">去登录</button></p>
    <p v-if="notice" class="module-notice">{{ notice }}</p>
    <div v-if="loading" class="loading-state">正在读取数据…</div>
    <template v-else>
      <div v-if="Object.keys(visibleSummary).length" class="module-stats"><div v-for="(item, key) in visibleSummary" :key="key"><strong>{{ short(item) }}</strong><span>{{ summaryLabels[String(key)] || key }}</span></div></div>

      <template v-if="module === 'reports'">
        <ResearchReports :dashboard="data" @updated="load" />
      </template>

      <template v-else-if="module === 'knowledge'">
        <div class="module-columns"><section class="paper-panel"><div class="panel-heading"><strong>研究能力</strong><span>{{ list.length }} 项</span></div><button v-for="item in list" :key="String(item.id)" class="module-list-item" :class="{ active: selectedId === item.id }" type="button" @click="choose(item)"><strong>{{ title(item) }}</strong><small>{{ short(item.description) }}</small></button></section><section class="paper-panel"><div class="panel-heading"><strong>{{ selected ? title(selected) : '能力详情' }}</strong></div><DataList :value="selected || {}" /></section></div><section class="paper-panel module-section"><div class="panel-heading"><strong>数据来源与连接状态</strong><span>{{ (data.dataProviders as Row[] || []).length }} 项</span></div><DataList :value="data.dataProviders || []" /></section>
      </template>

      <template v-else-if="module === 'skills'">
        <div class="module-actions"><button class="secondary-button" :class="{ active: !studio }" @click="studio = false">技能目录</button><button class="secondary-button" :class="{ active: studio }" @click="studio = true">版本工作室</button><RouterLink class="secondary-button" to="/">在研究任务中使用 →</RouterLink></div>
        <div class="module-columns"><section class="paper-panel"><div class="panel-heading"><strong>已注册技能</strong><span>{{ list.length }} 项</span></div><button v-for="item in list" :key="String(item.id)" class="module-list-item" :class="{ active: selectedId === item.id }" type="button" @click="choose(item)"><strong>{{ title(item) }}</strong><small>v{{ short(item.version) }} · {{ status(item) }}</small></button></section><section class="paper-panel"><div class="panel-heading"><strong>{{ selected ? title(selected) : '技能详情' }}</strong><a v-if="selectedId" :href="`/api/skills/${encodeURIComponent(selectedId)}/package`">下载包</a></div><p>{{ selected?.boundary }}</p><DataList :value="selected?.health || {}" /><template v-if="studio && selected"><div class="module-form"><label>技能文件路径<input v-model="skillPath" /></label><button type="button" class="secondary-button" :disabled="busy" @click="readSkill">打开文件</button></div><textarea v-model="editor" class="module-editor" aria-label="技能文件内容" spellcheck="false" /><div class="module-actions"><button class="secondary-button" :disabled="busy" @click="saveSkill">保存草稿</button><button class="secondary-button" :disabled="busy" @click="skillAction('discard-draft')">放弃草稿</button></div><div class="module-form"><label>新版本号<input v-model="skillVersion" placeholder="例如 0.8.0" /></label><label>变更摘要<input v-model="skillSummary" placeholder="本次更新了什么" /></label></div><button class="primary-button" :disabled="busy || !skillVersion || !skillSummary" @click="publishSkill">发布新版本</button></template></section></div>
        <section v-if="studio && selected" class="paper-panel module-section"><div class="panel-heading"><strong>版本管理与技能包</strong><span>平台技能</span></div><p>先查看草稿和当前版本的差异，再决定发布或回滚。回滚会修改平台发布版本，项目已固定的安装版本仍单独管理。</p><div class="module-actions"><button class="secondary-button" :disabled="busy" @click="skillAction('diff-version')">查看版本差异</button><label class="secondary-button skill-upload-label">上传技能包<input type="file" accept=".zip,application/zip" hidden @change="uploadSkill" /></label></div><div class="module-form"><label>恢复到版本<select v-model="skillRollbackVersion"><option value="">选择历史版本</option><option v-for="release in (((selected.changelog as Row | undefined)?.releases as Row[]) || [])" :key="String(release.version)" :value="String(release.version)" :disabled="!release.completeSnapshot">v{{ release.version }}{{ release.completeSnapshot ? '' : ' · 缺少完整快照' }}</option></select></label><button class="secondary-button" :disabled="busy || !skillRollbackVersion" @click="rollbackSkill">恢复发布版本</button></div></section>
      </template>

      <template v-else-if="module === 'eval'">
        <div class="module-columns"><section class="paper-panel"><div class="panel-heading"><strong>评测用例</strong><span>{{ list.length }} 项</span></div><button v-for="item in list" :key="String(item.id)" class="module-list-item" :class="{ active: selectedId === item.id }" type="button" @click="choose(item)"><strong>{{ title(item) }}</strong><small>{{ short(item.capabilityLabel || item.capabilityId) }} · {{ short(item.coverageLevel) }}</small></button></section><section class="paper-panel"><div class="panel-heading"><strong>{{ selected ? title(selected) : '评测设置' }}</strong></div><p v-if="selected">{{ selected.question }}</p><DataList :value="selected?.expectedDatasets || []" /><div class="module-form"><label>评测集<select v-model="evalSetId"><option value="">全部用例</option><option v-for="set in evalSets" :key="String(set.id)" :value="String(set.id)">{{ set.name }}</option></select></label><label>评测模式<select v-model="evalMode"><option value="contract">合同检查</option><option value="e2e">端到端</option></select></label><label>最多用例<input v-model.number="evalLimit" type="number" min="1" max="100" /></label><label>模型（可选）<input v-model="evalModel" placeholder="使用默认模型" /></label></div><div class="module-actions"><button class="secondary-button" :disabled="busy" @click="evalAction('simulate-flow')">预演</button><button class="primary-button" :disabled="busy" @click="evalAction('start-benchmark')">启动评测</button></div></section></div><section class="paper-panel module-section"><div class="panel-heading"><strong>最近运行</strong><span>{{ (data.runs as Row[] || []).length }} 次</span></div><div v-for="item in (data.runs as Row[] || [])" :key="String(item.id)" class="module-run"><strong>{{ title(item) }}</strong><span>{{ status(item) }}</span><button class="secondary-button" @click="showDetail(`/api/evals/runs/${encodeURIComponent(String(item.id))}`, '评测详情')">查看</button></div><p v-if="!(data.runs as Row[] || []).length" class="module-empty">暂无运行记录。</p></section><EvalManager :cases="list" :queue="(data.queue as Row[] || [])" @updated="load" />
        <section class="paper-panel module-section"><div class="panel-heading"><strong>运行参数</strong><span>影响下一次评测</span></div><p>先用少量用例和单次运行确认流程，再增加并发或重复次数检查稳定性。</p><div class="module-form"><label>评测器<select v-model="evalEvaluator"><option value="">按模式自动选择</option><option value="rule-strict">规则严格检查</option><option value="agent-review">Agent 语义审阅</option><option value="visual-contract">看板视觉合同</option></select></label><label>并发数<input v-model.number="evalConcurrency" type="number" min="1" max="8" /></label><label>重复次数<input v-model.number="evalRepeat" type="number" min="1" max="5" /></label><label class="eval-check"><input v-model="evalKeepProjects" type="checkbox" />保留评测生成的项目</label></div></section>
      </template>

      <template v-else-if="module === 'ops'">
        <OpsCenter :dashboard="data" @updated="load" />
      </template>

      <template v-else-if="module === 'users'">
        <div class="module-columns"><section class="paper-panel"><div class="panel-heading"><strong>用户</strong><span>{{ list.length }} 人</span></div><button v-for="item in list" :key="String(item.id)" class="module-list-item" :class="{ active: selectedId === item.id }" @click="choose(item)"><strong>{{ title(item) }}</strong><small>{{ short(item.email) }} · {{ short(item.role) }}</small></button><p v-if="!list.length" class="module-empty">没有可显示的用户，或当前账号无管理权限。</p></section><section class="paper-panel"><div class="panel-heading"><strong>创建账号</strong></div><div class="module-form column"><label>姓名<input v-model="userName" /></label><label>邮箱<input v-model="userEmail" type="email" /></label><label>初始密码<input v-model="userPassword" type="password" /></label><label>角色<select v-model="userRole"><option value="member">成员</option><option value="admin">管理员</option></select></label></div><button class="primary-button" :disabled="busy || !userEmail || !userPassword" @click="createUser">创建用户</button></section></div><section class="paper-panel module-section"><div class="panel-heading"><strong>成员权限与审计</strong><button class="secondary-button" @click="showDetail('/api/admin/audit', '安全审计')">查看审计</button></div><DataList :value="selected || {}" /><div v-if="selected" class="module-actions"><button class="secondary-button" :disabled="busy" @click="updateUser('set-role', { role: selected.role === 'admin' ? 'member' : 'admin' })">切换角色</button><button class="secondary-button" :disabled="busy" @click="updateUser('set-status', { banned: !selected.banned })">{{ selected.banned ? '恢复账号' : '停用账号' }}</button><button class="secondary-button" :disabled="busy" @click="updateUser('revoke-sessions')">撤销会话</button></div></section>
        <AdminAccess :user-id="selectedId" />
      </template>

      <template v-else-if="module === 'account'">
        <div class="workspace-tabs"><RouterLink to="/account/usage" :class="{ active: section === 'usage' }">使用情况</RouterLink><RouterLink to="/account/memory" :class="{ active: section === 'memory' }">个人记忆</RouterLink><RouterLink to="/account/security" :class="{ active: section === 'security' }">账号安全</RouterLink></div>
        <section v-if="section === 'memory'" class="paper-panel module-section"><div class="panel-heading"><strong>个人记忆</strong><span>{{ short((data.integration as Row)?.status) }}</span></div><p>开启后，后续研究可以使用独立 Memory 服务保存的个人偏好。关闭不会删除历史记忆。</p><div class="module-actions"><button class="primary-button" :disabled="busy" @click="updateMemory(true)">开启个性化</button><button class="secondary-button" :disabled="busy" @click="updateMemory(false)">关闭个性化</button></div><AccountExtras kind="memory" :data="data" @updated="load" /></section>
        <section v-else-if="section === 'security'" class="paper-panel module-section"><div class="panel-heading"><strong>修改密码</strong></div><div class="module-form column"><label>当前密码<input v-model="oldPassword" type="password" autocomplete="current-password" /></label><label>新密码<input v-model="newPassword" type="password" autocomplete="new-password" /></label><label>再次输入新密码<input v-model="confirmPassword" type="password" autocomplete="new-password" /></label></div><button class="primary-button" :disabled="busy || !oldPassword || !newPassword || newPassword !== confirmPassword" @click="changePassword">更新密码</button><AccountExtras kind="sessions" :data="data" @updated="load" /></section>
        <section v-else class="paper-panel module-section"><div class="panel-heading"><strong>用量与额度</strong></div><DataList :value="data" /></section>
      </template>

      <section v-if="detail !== null" class="paper-panel module-section"><div class="panel-heading"><strong>{{ detailTitle }}</strong><button class="secondary-button" @click="detail = null">收起</button></div><DataList :value="detail" /></section>
    </template>
  </main>
</template>

<style scoped>
.module-heading{display:flex;justify-content:space-between;align-items:center;gap:20px}.module-heading p{color:#68746b}.module-notice{color:#2d775d}.module-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:20px 0}.module-stats>div{border:1px solid #e1e3d8;background:#fffefa;border-radius:7px;padding:14px}.module-stats strong,.module-stats span{display:block}.module-stats strong{font-size:23px;color:#263c33}.module-stats span{margin-top:5px;font-size:11px;color:#68746b}.module-columns{display:grid;grid-template-columns:minmax(250px,1fr) minmax(0,2fr);gap:16px;margin-top:16px}.module-list-item{display:block;text-align:left;width:100%;border:0;border-bottom:1px solid #e1e3d8;background:transparent;padding:12px 8px}.module-list-item.active{background:#f5f1e8;color:#986923}.module-list-item strong,.module-list-item small{display:block}.module-list-item small{margin-top:4px;color:#68746b;white-space:normal;overflow-wrap:anywhere}.module-section{margin-top:16px}.module-empty{font-size:13px;color:#68746b}.module-actions{display:flex;flex-wrap:wrap;gap:9px;margin:18px 0}.module-actions button,.module-actions a{padding:9px 13px;border-radius:6px}.module-actions .active{border-color:#a56e27;color:#a56e27}.module-form{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:16px 0}.module-form.column{display:grid;max-width:380px}.module-form label{display:grid;gap:6px;font-size:12px;color:#68746b}.module-form input,.module-form select{padding:9px;border:1px solid #dcded4;border-radius:6px;background:#fff;min-width:170px}.module-editor{width:100%;height:320px;border:1px solid #dcded4;border-radius:6px;padding:12px;resize:vertical;font:12px/1.5 ui-monospace,monospace}.module-run{display:flex;align-items:center;gap:14px;padding:10px;border-bottom:1px solid #e1e3d8}.module-run button{margin-left:auto}.module-page h3{font-size:15px;margin:22px 0 10px}.workspace-tabs a{padding:11px 17px;text-decoration:none;color:inherit}.workspace-tabs a.active{border-bottom:2px solid #a56e27;color:#a56e27}@media(max-width:760px){.module-columns{grid-template-columns:1fr}.module-heading{align-items:flex-start}.module-stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
