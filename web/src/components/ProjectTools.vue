<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import DataList from './DataList.vue';
import ProjectSkills from './ProjectSkills.vue';
import ProjectMembers from './ProjectMembers.vue';
import ProjectMemory from './ProjectMemory.vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ projectId: string }>();
const base = computed(() => `/api/projects/${encodeURIComponent(props.projectId)}`);
const view = ref<'review' | 'environment' | 'connections' | 'skills' | 'members' | 'memory'>('review');
const approvals = ref<Row[]>([]);
const validation = ref<Row>({});
const variables = ref<Row[]>([]);
const connections = ref<Row[]>([]);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const envKey = ref('');
const envValue = ref('');
const envScope = ref('runtime');
const envSecret = ref(true);
const repoName = ref('');
const repoPrivate = ref(true);
const vercelName = ref('');
const supabaseId = ref('');
const supabaseUrl = ref('');
const supabaseName = ref('');
const supabasePassword = ref('');
const supabaseOrg = ref('');
const supabaseRegion = ref('us-east-1');
const organizations = ref<Row[]>([]);
const editedApproval = ref('');
const result = ref<unknown>(null);
const title = (item: Row) => String(item.name || item.key || item.toolName || item.provider || item.id || '项目服务');
async function readView() {
  if (view.value === 'skills' || view.value === 'members' || view.value === 'memory') return;
  loading.value = true; error.value = '';
  const requests = view.value === 'review'
    ? [apiRequest<ApiEnvelope<Row[]>>(`${base.value}/agent/approvals?status=pending`), apiRequest<Row>(`${base.value}/quant/validation`)]
    : view.value === 'environment'
    ? [apiRequest<Row[]>(`/api/env/${encodeURIComponent(props.projectId)}`)]
    : [apiRequest<Row[]>(`${base.value}/services`)];
  const values = await Promise.allSettled(requests);
  values.forEach((entry, index) => {
    if (entry.status === 'rejected') { error.value = entry.reason instanceof Error ? entry.reason.message : '读取失败'; return; }
    const data = unwrap(entry.value as ApiEnvelope<unknown>);
    if (view.value === 'review') { if (index === 0) approvals.value = Array.isArray(data) ? data as Row[] : []; else validation.value = entry.value as Row; }
    else if (view.value === 'environment') variables.value = Array.isArray(data) ? data as Row[] : [];
    else connections.value = Array.isArray(data) ? data as Row[] : [];
  });
  loading.value = false;
}
async function change(path: string, method = 'POST', body: Row = {}) {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    result.value = unwrap(await apiRequest<ApiEnvelope<unknown>>(path, { method, body: method === 'DELETE' || method === 'GET' ? undefined : JSON.stringify(body) }));
    notice.value = '操作完成'; await readView();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败'; }
  finally { busy.value = false; }
}
async function resolveApproval(item: Row, decision: 'approve' | 'reject' | 'edit') {
  if (decision === 'approve' && !window.confirm(`批准 ${title(item)} 这项操作？`)) return;
  let body: Row = { decision };
  if (decision === 'edit') {
    try { body = { decision, editedInput: JSON.parse(editedApproval.value) }; }
    catch { error.value = '修改后的参数必须是有效 JSON 对象'; return; }
  }
  await change(`${base.value}/agent/approvals/${encodeURIComponent(String(item.id))}`, 'POST', body);
}
async function addVariable() {
  if (!envKey.value || !envValue.value) return;
  await change(`/api/env/${encodeURIComponent(props.projectId)}/upsert`, 'POST', { key: envKey.value, value: envValue.value, scope: envScope.value, isSecret: envSecret.value });
  envValue.value = '';
}
async function removeVariable(item: Row) {
  if (!window.confirm(`删除变量 ${item.key}？`)) return;
  await change(`/api/env/${encodeURIComponent(props.projectId)}/${encodeURIComponent(String(item.key))}`, 'DELETE');
}
async function connectGithub() { await change(`${base.value}/github/connect`, 'POST', { repo_name: repoName.value, private: repoPrivate.value }); }
async function connectVercel() { await change(`${base.value}/vercel/connect`, 'POST', { project_name: vercelName.value }); }
async function connectSupabase() { await change(`${base.value}/supabase/connect`, 'POST', { project_id: supabaseId.value, project_url: supabaseUrl.value }); }
async function loadOrganizations() {
  busy.value = true; error.value = '';
  try {
    const payload = await apiRequest<Row>('/api/supabase/organizations');
    organizations.value = Array.isArray(payload.organizations) ? payload.organizations as Row[] : [];
    if (!supabaseOrg.value) supabaseOrg.value = String(organizations.value[0]?.id || '');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取 Supabase 组织失败'; }
  finally { busy.value = false; }
}
async function createSupabase() {
  if (!supabaseName.value || !supabaseOrg.value || !supabasePassword.value) return;
  if (!window.confirm('将在 Supabase 上创建一个真实项目。确定继续？')) return;
  await change('/api/supabase/create-project', 'POST', { project_id: props.projectId, project_name: supabaseName.value, organization_id: supabaseOrg.value, db_pass: supabasePassword.value, region: supabaseRegion.value });
  supabasePassword.value = '';
}
async function disconnect(item: Row) { if (window.confirm(`断开 ${title(item)}？`)) await change(`${base.value}/services/${encodeURIComponent(String(item.id))}`, 'DELETE'); }
watch([view, () => props.projectId], readView);
onMounted(readView);
</script>

<template>
  <section class="project-tools paper-panel"><div class="panel-heading"><strong>项目管理</strong><button class="secondary-button" type="button" @click="readView">刷新</button></div>
    <div class="tools-tabs"><button :class="{ active: view === 'review' }" @click="view = 'review'">审批与验收</button><button :class="{ active: view === 'environment' }" @click="view = 'environment'">环境变量</button><button :class="{ active: view === 'connections' }" @click="view = 'connections'">外部连接</button><button :class="{ active: view === 'skills' }" @click="view = 'skills'">项目技能</button><button :class="{ active: view === 'members' }" @click="view = 'members'">项目成员</button><button :class="{ active: view === 'memory' }" @click="view = 'memory'">研究偏好</button></div>
    <p v-if="loading">正在读取…</p><p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="tools-notice">{{ notice }}</p>
    <template v-if="view === 'review'"><h3>待处理审批</h3><p v-if="!approvals.length" class="tools-empty">当前没有等待你处理的操作。</p><article v-for="item in approvals" :key="String(item.id)" class="tools-card"><div><strong>{{ title(item) }}</strong><small>{{ item.reason || item.effect }}</small></div><DataList :value="item.publicInput || {}" :limit="8" /><div class="tools-actions"><button :disabled="busy" @click="resolveApproval(item, 'approve')">批准</button><button :disabled="busy" @click="resolveApproval(item, 'reject')">拒绝</button><button :disabled="busy || !editedApproval" @click="resolveApproval(item, 'edit')">修改后批准</button></div></article><label v-if="approvals.length" class="tools-label">修改后的操作参数（JSON）<textarea v-model="editedApproval" rows="3" /></label><h3>自动验收</h3><DataList :value="validation.data || validation" :limit="14" /><div class="tools-actions"><button class="secondary-button" :disabled="busy" @click="change(`${base}/quant/validation`)">重新验证</button></div></template>
    <template v-else-if="view === 'environment'"><p>这里的变量只属于当前项目。密钥值保存后不会在列表中明文展示。</p><div class="tools-form"><label>变量名<input v-model="envKey" placeholder="例如 MODEL_API_URL" /></label><label>值<input v-model="envValue" :type="envSecret ? 'password' : 'text'" /></label><label>用途<select v-model="envScope"><option value="runtime">运行时</option><option value="build">构建时</option><option value="both">构建和运行</option></select></label><label class="tools-check"><input v-model="envSecret" type="checkbox" /> 作为密钥保存</label></div><button class="primary-button" :disabled="busy || !envKey || !envValue" @click="addVariable">保存变量</button><h3>已有变量</h3><div v-for="item in variables" :key="String(item.key)" class="tools-row"><div><strong>{{ item.key }}</strong><small>{{ item.valuePreview || '已保存' }} · {{ item.scope }}</small></div><button class="secondary-button" :disabled="busy" @click="removeVariable(item)">删除</button></div><p v-if="!variables.length" class="tools-empty">当前没有项目环境变量。</p></template>
    <ProjectSkills v-else-if="view === 'skills'" :project-id="projectId" /><ProjectMembers v-else-if="view === 'members'" :project-id="projectId" /><ProjectMemory v-else-if="view === 'memory'" :project-id="projectId" /><template v-else><div class="tools-grid"><article class="tools-card"><h3>GitHub</h3><p>连接仓库后可将生成的项目文件推送到你的仓库。</p><label class="tools-label">仓库名称<input v-model="repoName" /></label><label class="tools-check"><input v-model="repoPrivate" type="checkbox" /> 私有仓库</label><div class="tools-actions"><button :disabled="busy || !repoName" @click="connectGithub">创建并连接</button><button :disabled="busy" @click="change(`${base}/github/push`)">推送代码</button></div></article><article class="tools-card"><h3>Vercel</h3><p>连接部署项目后可发布当前看板。</p><label class="tools-label">项目名称<input v-model="vercelName" /></label><div class="tools-actions"><button :disabled="busy || !vercelName" @click="connectVercel">连接项目</button><button :disabled="busy" @click="change(`${base}/vercel/deploy`)">部署</button></div></article><article class="tools-card"><h3>Supabase</h3><p>将已有 Supabase 项目关联到当前任务。</p><label class="tools-label">项目 ID<input v-model="supabaseId" /></label><label class="tools-label">项目 URL<input v-model="supabaseUrl" /></label><button :disabled="busy || !supabaseId || !supabaseUrl" @click="connectSupabase">连接</button></article></div><h3>已连接的服务</h3><div v-for="item in connections" :key="String(item.id)" class="tools-row"><div><strong>{{ title(item) }}</strong><small>{{ item.type || item.serviceType || item.id }}</small></div><button class="secondary-button" :disabled="busy" @click="disconnect(item)">断开</button></div><p v-if="!connections.length" class="tools-empty">当前没有外部连接。</p></template>
    <div v-if="view === 'connections'" class="tools-card"><h3>创建 Supabase 项目</h3><p>需要已配置 Supabase 凭据。创建成功后，将返回的项目 ID 与项目 URL 填入上方连接表单。</p><div class="tools-actions"><button class="secondary-button" :disabled="busy" @click="loadOrganizations">读取组织</button><button v-if="vercelName" class="secondary-button" :disabled="busy" @click="change(`/api/vercel/check-project/${encodeURIComponent(vercelName)}`, 'GET')">检查 Vercel 名称</button><button v-if="repoName" class="secondary-button" :disabled="busy" @click="change(`/api/github/check-repo/${encodeURIComponent(repoName)}`, 'GET')">检查仓库名称</button></div><div class="tools-form"><label>组织<select v-model="supabaseOrg"><option v-for="item in organizations" :key="String(item.id)" :value="String(item.id)">{{ item.name || item.id }}</option></select></label><label>项目名称<input v-model="supabaseName" /></label><label>数据库密码<input v-model="supabasePassword" type="password" /></label><label>区域<input v-model="supabaseRegion" /></label></div><button class="primary-button" :disabled="busy || !supabaseOrg || !supabaseName || !supabasePassword" @click="createSupabase">创建项目</button></div>
    <div v-if="result !== null" class="tools-result"><h3>最近操作结果</h3><DataList :value="result" :limit="12" /></div>
  </section>
</template>

<style scoped>
.tools-tabs{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid #e1e3d8;margin-bottom:18px}.tools-tabs button{border:0;background:transparent;padding:10px 13px;color:#69736b}.tools-tabs button.active{color:#a56e27;border-bottom:2px solid #a56e27}.project-tools h3{font-size:14px;margin:22px 0 10px}.project-tools p{font-size:12px;color:#69736b;line-height:1.7}.tools-empty{padding:16px;background:#f6f5f0;border-radius:6px}.tools-notice{color:#2b765c!important}.tools-card{border:1px solid #e1e3d8;border-radius:7px;padding:15px;margin:12px 0;background:#fffefa;min-width:0}.tools-card strong,.tools-card small{display:block}.tools-card small{margin-top:5px;color:#69736b}.tools-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.tools-actions button,.tools-card>button{padding:8px 12px;background:white;border:1px solid #dcded4;border-radius:6px}.tools-form{display:flex;align-items:end;gap:10px;flex-wrap:wrap;margin:16px 0}.tools-form label,.tools-label{display:grid;gap:6px;font-size:12px;color:#69736b;margin:10px 0}.tools-form input,.tools-form select,.tools-label input,.tools-label textarea{border:1px solid #dcded4;border-radius:6px;padding:9px;background:white;min-width:155px}.tools-label textarea{width:100%;font:12px ui-monospace,monospace}.tools-check{display:flex!important;align-items:center;gap:7px!important}.tools-check input{min-width:0}.tools-row{display:flex;align-items:center;justify-content:space-between;gap:15px;border-bottom:1px solid #e1e3d8;padding:12px 0}.tools-row strong,.tools-row small{display:block}.tools-row small{color:#69736b;margin-top:5px;font-size:11px}.tools-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.tools-result{margin-top:25px;border-top:1px solid #e1e3d8}@media(max-width:850px){.tools-grid{grid-template-columns:1fr}}
</style>
