<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';
import { MODELS, modelReadiness, type ModelStatus } from '../lib/models';

type GlobalSettings = { cliSettings?: { pi?: { model?: string } } };
type ServiceToken = { id: string; name: string; token_preview: string; has_token: boolean; last_used?: string | null };
type Provider = 'github' | 'vercel' | 'supabase';
type Infrastructure = { provider?: string; connected?: boolean; timescale?: { enabled?: boolean; version?: string | null }; docker?: { available?: boolean; running?: boolean; service?: { status?: string; image?: string } | null }; quantSchema?: { tables?: string[] } };
const providers: { id: Provider; label: string; purpose: string }[] = [
  { id: 'github', label: 'GitHub', purpose: '把项目代码连接到仓库' },
  { id: 'vercel', label: 'Vercel', purpose: '发布生成的 Vue 看板' },
  { id: 'supabase', label: 'Supabase', purpose: '连接项目数据库' },
];
const defaultModel = ref<string>(MODELS[0].id);
const status = ref<ModelStatus | null>(null);
const infrastructure = ref<Infrastructure | null>(null);
const tokenRecords = ref<Partial<Record<Provider, ServiceToken>>>({});
const tokenValues = ref<Partial<Record<Provider, string>>>({});
const tokenNames = ref<Partial<Record<Provider, string>>>({});
const loading = ref(true);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const configuredServices = computed(() => providers.filter(item => tokenRecords.value[item.id]?.has_token).length);

async function load() {
  loading.value = true; error.value = '';
  const results = await Promise.allSettled([
    apiRequest<GlobalSettings>('/api/settings/global'),
    apiRequest<ModelStatus>('/api/settings/cli-status'),
    ...providers.map(item => apiRequest<ServiceToken>(`/api/tokens/${item.id}`)),
    apiRequest<ApiEnvelope<Infrastructure>>('/api/infrastructure/health'),
  ]);
  const settings = results[0];
  if (settings.status === 'fulfilled') defaultModel.value = settings.value.cliSettings?.pi?.model || MODELS[0].id;
  else error.value = settings.reason instanceof Error ? settings.reason.message : '读取设置失败';
  const model = results[1];
  if (model.status === 'fulfilled') status.value = model.value;
  providers.forEach((provider, index) => {
    const result = results[index + 2];
    if (result.status === 'fulfilled') tokenRecords.value[provider.id] = result.value as ServiceToken;
    else delete tokenRecords.value[provider.id];
  });
  const infra = results[providers.length + 2];
  infrastructure.value = infra.status === 'fulfilled' ? unwrap(infra.value as ApiEnvelope<Infrastructure>) : null;
  loading.value = false;
}
async function saveModel() {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest<GlobalSettings>('/api/settings/global', { method: 'PUT', body: JSON.stringify({ cliSettings: { pi: { model: defaultModel.value } } }) });
    notice.value = '默认模型已保存。新的研究任务会使用这个默认值。';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存失败'; }
  finally { busy.value = false; }
}
async function saveToken(provider: Provider) {
  const token = tokenValues.value[provider]?.trim();
  if (!token) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    const payload = await apiRequest<ApiEnvelope<ServiceToken>>('/api/tokens', { method: 'POST', body: JSON.stringify({ provider, token, name: tokenNames.value[provider] || provider }) });
    tokenRecords.value[provider] = unwrap(payload);
    tokenValues.value[provider] = '';
    notice.value = `${provider} 凭据已保存，页面只显示遮盖后的摘要。`;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存凭据失败'; }
  finally { busy.value = false; }
}
async function removeToken(provider: Provider) {
  const record = tokenRecords.value[provider];
  if (!record || !window.confirm(`移除 ${provider} 凭据？依赖它的连接将无法继续调用。`)) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(`/api/tokens/${encodeURIComponent(record.id)}`, { method: 'DELETE' });
    delete tokenRecords.value[provider];
    notice.value = `${provider} 凭据已移除。`;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '移除凭据失败'; }
  finally { busy.value = false; }
}
onMounted(load);
</script>

<template>
  <main class="workspace-main settings-page">
    <div class="breadcrumb">AlphaDeck　/　设置与连接</div>
    <div class="settings-heading"><div class="eyebrow">WORKSPACE SETTINGS</div><h1>让研究工具连接起来</h1><p>选择默认模型、检查可用性，并管理项目发布所需的服务凭据。</p></div>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="settings-notice">{{ notice }}</p>
    <p v-if="loading" class="loading-state">正在读取设置…</p>
    <template v-else>
      <div class="settings-overview"><div><span>默认模型</span><strong>{{ MODELS.find(item => item.id === defaultModel)?.label || '待选择' }}</strong><small>{{ modelReadiness(status, defaultModel) ? '需要检查连接' : '连接检查通过' }}</small></div><div><span>服务凭据</span><strong>{{ configuredServices }} / {{ providers.length }}</strong><small>GitHub、Vercel、Supabase</small></div><div><span>本地数据库</span><strong>{{ infrastructure?.connected ? '已连接' : '待检查' }}</strong><small>{{ infrastructure?.provider || 'PostgreSQL + TimescaleDB' }}</small></div></div>
      <section class="paper-panel settings-panel"><div class="panel-heading"><strong>默认研究模型</strong><button class="secondary-button" :disabled="busy" @click="load">重新检查</button></div><p>首页创建新研究时默认选择这个模型。实际可用性还取决于本机服务和 API Key。</p><div class="settings-model"><select v-model="defaultModel" aria-label="默认研究模型"><option v-for="model in MODELS" :key="model.id" :value="model.id">{{ model.label }}</option></select><button class="primary-button" :disabled="busy" @click="saveModel">保存默认模型</button></div><div class="settings-status" :class="{ bad: modelReadiness(status, defaultModel) }">{{ modelReadiness(status, defaultModel) || '模型连接检查通过' }}</div><p class="settings-hint">模型 API Key 从项目根目录的 <code>.env</code> 或进程环境读取；修改后重启服务，再点“重新检查”。</p></section>
      <section class="paper-panel settings-panel"><div class="panel-heading"><strong>本地基础设施</strong><RouterLink class="secondary-button" to="/ops-platform">打开运行治理 →</RouterLink></div><p>研究任务、评测和工作空间依赖本地数据库；行情时序数据由 TimescaleDB 保存。</p><div class="infra-grid"><div><span>数据库连接</span><strong>{{ infrastructure?.connected ? '正常' : '无法确认' }}</strong></div><div><span>TimescaleDB</span><strong>{{ infrastructure?.timescale?.enabled ? '已启用' : '待检查' }}</strong></div><div><span>数据库表</span><strong>{{ infrastructure?.quantSchema?.tables?.length ?? '—' }}</strong></div><div><span>Docker 服务</span><strong>{{ infrastructure?.docker?.service?.status || (infrastructure?.docker?.running ? '运行中' : '待检查') }}</strong></div></div><p v-if="!infrastructure?.connected" class="settings-hint">数据库未连接时，可到“运行治理”查看具体错误，再检查本地 Docker 服务。</p></section>
      <section class="paper-panel settings-panel"><div class="panel-heading"><strong>服务凭据</strong><span>仅管理员可修改</span></div><p>这里管理代码托管、发布和数据库连接使用的凭据。模型 Key 不在此处填写。</p><div v-for="provider in providers" :key="provider.id" class="settings-provider"><div><strong>{{ provider.label }}</strong><small>{{ provider.purpose }}</small><small v-if="tokenRecords[provider.id]">已配置：{{ tokenRecords[provider.id]?.token_preview }}</small><small v-else>尚未配置</small></div><div class="settings-token-fields"><input v-model="tokenNames[provider.id]" :aria-label="`${provider.label} 凭据名称`" placeholder="凭据名称（可选）" /><input v-model="tokenValues[provider.id]" :aria-label="`${provider.label} 凭据`" type="password" autocomplete="new-password" placeholder="粘贴新凭据" /><button class="secondary-button" :disabled="busy || !tokenValues[provider.id]?.trim()" @click="saveToken(provider.id)">保存</button><button v-if="tokenRecords[provider.id]" class="secondary-button" :disabled="busy" @click="removeToken(provider.id)">移除</button></div></div></section>
    </template>
  </main>
</template>

<style scoped>
.settings-page{max-width:1120px;margin:auto}.settings-heading h1{font:650 clamp(27px,3vw,37px)/1.3 Georgia,'Noto Serif SC',serif;margin:10px 0}.settings-heading p,.settings-panel p{color:var(--muted);font-size:13px;line-height:1.8}.settings-panel{padding:24px;margin:20px 0}.settings-panel .panel-heading{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:10px}.settings-model{display:flex;gap:10px;flex-wrap:wrap}.settings-model select,.settings-token-fields input{border:1px solid var(--line);background:#fff;padding:10px;border-radius:6px;min-width:230px}.settings-status{margin-top:15px;font-size:13px;color:#287455}.settings-status.bad{color:var(--danger)}.settings-hint{margin:12px 0 0}.settings-notice{color:#287455;font-size:13px}.settings-provider{border-top:1px solid var(--line);padding:18px 0;display:grid;grid-template-columns:190px minmax(0,1fr);gap:18px}.settings-provider strong,.settings-provider small{display:block}.settings-provider small{font-size:11px;color:var(--muted);margin-top:6px}.settings-token-fields{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.settings-token-fields input{min-width:170px;flex:1}.settings-token-fields button{white-space:nowrap}@media(max-width:720px){.settings-provider{grid-template-columns:1fr}.settings-token-fields input{width:100%}}
.settings-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:22px 0}.settings-overview>div{background:#273d34;color:white;border-radius:8px;padding:17px}.settings-overview span,.settings-overview strong,.settings-overview small{display:block}.settings-overview span{font-size:11px;color:#dbc89f}.settings-overview strong{font-size:16px;margin:8px 0;overflow-wrap:anywhere}.settings-overview small{font-size:11px;color:#d8e0d8}.infra-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}.infra-grid>div{background:#f3f1e9;border:1px solid #e6e3d7;border-radius:6px;padding:13px}.infra-grid span,.infra-grid strong{display:block}.infra-grid span{font-size:11px;color:#6f776f}.infra-grid strong{font-size:14px;margin-top:8px;overflow-wrap:anywhere}@media(max-width:760px){.settings-overview{grid-template-columns:1fr}.infra-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
