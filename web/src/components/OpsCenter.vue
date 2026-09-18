<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';
import DataList from './DataList.vue';

type Row = Record<string, unknown>;
const props = defineProps<{ dashboard: Row }>();
const emit = defineEmits<{ updated: [] }>();
const view = ref<'overview' | 'services' | 'workspaces' | 'trace' | 'logs'>('overview');
const health = ref<Row | null>(null);
const trace = ref<Row | null>(null);
const logs = ref<Row | null>(null);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const query = ref('');
const selectedProjectId = ref('');
const selectedTraceId = ref('');
const logId = ref('');
const logLevel = ref('all');
const services = computed<Row[]>(() => Array.isArray(props.dashboard.serviceCatalog) ? props.dashboard.serviceCatalog as Row[] : []);
const checks = computed<Row[]>(() => [...(Array.isArray(props.dashboard.systemChecks) ? props.dashboard.systemChecks as Row[] : []), ...(Array.isArray(props.dashboard.capabilityChecks) ? props.dashboard.capabilityChecks as Row[] : [])]);
const atRisk = computed(() => checks.value.filter(item => item.status === 'warning' || item.status === 'failed'));
const workerSummary = computed<Row>(() => props.dashboard.agentWorkers && typeof props.dashboard.agentWorkers === 'object' ? ((props.dashboard.agentWorkers as Row).summary as Row || {}) : {});
const productSummary = computed<Row>(() => props.dashboard.productHealth && typeof props.dashboard.productHealth === 'object' ? ((props.dashboard.productHealth as Row).summary as Row || {}) : {});
const projects = computed<Row[]>(() => Array.isArray(health.value?.projects) ? health.value.projects as Row[] : []);
const traceProjects = computed<Row[]>(() => Array.isArray(trace.value?.projects) ? trace.value.projects as Row[] : []);
const logSources = computed<Row[]>(() => Array.isArray(logs.value?.logSources) ? logs.value.logSources as Row[] : []);
const selectedProject = computed(() => projects.value.find(item => item.id === selectedProjectId.value) || projects.value[0] || null);
const selectedTrace = computed(() => traceProjects.value.find(item => item.id === selectedTraceId.value) || traceProjects.value[0] || null);
const activeLog = computed(() => logSources.value.find(item => item.id === logId.value) || logSources.value[0] || null);
const filteredServices = computed(() => services.value.filter(item => `${item.name || ''} ${item.id || ''} ${item.runtime || ''}`.toLowerCase().includes(query.value.toLowerCase())));
const filteredProjects = computed(() => projects.value.filter(item => `${item.name || ''} ${item.id || ''} ${(item.health as Row)?.summary || ''}`.toLowerCase().includes(query.value.toLowerCase())));
const filteredTraces = computed(() => traceProjects.value.filter(item => `${item.name || ''} ${item.id || ''} ${(item.trace as Row)?.summary || ''}`.toLowerCase().includes(query.value.toLowerCase())));
const logEntries = computed<Row[]>(() => {
  const source = activeLog.value;
  const entries = Array.isArray(source?.entries) ? source.entries as Row[] : Array.isArray(source?.lines) ? source.lines.map((line, index) => ({ id: `${source?.id}-${index}`, message: line, level: '', timestamp: source?.modifiedAt })) : [];
  return entries.filter(item => {
    const level = String(item.level || '').toLowerCase();
    return (logLevel.value === 'all' || level.includes(logLevel.value)) && `${item.message || ''} ${item.raw || ''}`.toLowerCase().includes(query.value.toLowerCase());
  }).slice(-250);
});
const status = (value: unknown) => String(value || '待检查');
const date = (value: unknown) => value ? new Date(String(value)).toLocaleString('zh-CN') : '—';

async function loadDetail(force = false) {
  const target = view.value;
  if (target === 'overview' || target === 'services') return;
  if (!force && ((target === 'workspaces' && health.value) || (target === 'trace' && trace.value) || (target === 'logs' && logs.value))) return;
  loading.value = true; error.value = '';
  const path = target === 'workspaces' ? '/api/workspaces/health' : target === 'trace' ? '/api/workspaces/trace?events=120' : '/api/ops/platform?includeLogs=1';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<Row>>(path));
    if (target === 'workspaces') health.value = result;
    else if (target === 'trace') trace.value = result;
    else logs.value = result;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '运行信息读取失败'; }
  finally { loading.value = false; }
}
async function refresh() { emit('updated'); await loadDetail(true); }
async function validate() {
  if (!selectedProject.value) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(`/api/projects/${encodeURIComponent(String(selectedProject.value.id))}/quant/validation`, { method: 'POST', body: '{}' });
    notice.value = '已重新验证工作空间'; await loadDetail(true);
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '重新验证失败'; }
  finally { busy.value = false; }
}
watch(view, () => { query.value = ''; void loadDetail(); });
</script>

<template>
  <div class="ops-center"><div class="ops-tabs" role="tablist" aria-label="运行治理视图"><button v-for="item in [{id:'overview',label:'运行总览'},{id:'services',label:'服务治理'},{id:'workspaces',label:'工作空间'},{id:'trace',label:'生成链路'},{id:'logs',label:'运行日志'}]" :key="item.id" type="button" role="tab" :aria-selected="view === item.id" :class="{active:view === item.id}" @click="view = item.id as typeof view">{{ item.label }}</button><button class="ops-refresh" @click="refresh">刷新状态</button></div><p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="ops-notice">{{ notice }}</p><p v-if="loading" class="loading-state">正在读取运行现场…</p>
    <div v-if="view === 'overview'" class="ops-overview"><section class="ops-feature"><div class="eyebrow">SYSTEM POSTURE</div><h2>{{ atRisk.length ? `有 ${atRisk.length} 项需要关注` : '运行状态概览' }}</h2><p>从服务状态、工作空间和生成链路往下排查。页面数据来自当前本地环境。</p><div class="ops-feature-actions"><button @click="view = 'services'">检查服务 →</button><button @click="view = 'workspaces'">检查工作空间 →</button></div></section><section class="paper-panel"><div class="panel-heading"><strong>优先处理</strong><span>{{ atRisk.length }} 项</span></div><div v-for="item in atRisk.slice(0, 5)" :key="String(item.id)" class="ops-check"><strong>{{ item.label || item.name || item.id }}</strong><span>{{ item.status }}</span><p>{{ item.detail || item.summary || item.message }}</p></div><p v-if="!atRisk.length" class="ops-muted">当前检查没有报告阻断或警告。可继续查看工作空间的验收状态。</p></section><section class="paper-panel"><div class="panel-heading"><strong>Worker 与任务执行</strong></div><div class="ops-facts"><div><strong>{{ workerSummary.activeWorkers ?? '—' }}</strong><span>运行中 Worker</span></div><div><strong>{{ workerSummary.pendingJobs ?? '—' }}</strong><span>待派发任务</span></div><div><strong>{{ workerSummary.availableSlots ?? '—' }}</strong><span>可用槽位</span></div></div><p class="ops-muted">运行模式：{{ (dashboard.agentWorkers as Row)?.dispatchMode || '待检查' }}</p></section><section class="paper-panel"><div class="panel-heading"><strong>产品健康</strong></div><div class="ops-facts"><div><strong>{{ productSummary.requests ?? 0 }}</strong><span>研究请求</span></div><div><strong>{{ productSummary.completedRequests ?? 0 }}</strong><span>已完成</span></div><div><strong>{{ productSummary.failedRequests ?? 0 }}</strong><span>失败</span></div></div><p class="ops-muted">到“工作空间”查看每个任务的交付、验证和数据质量。</p></section></div>
    <div v-else-if="view === 'services'" class="ops-services"><div class="ops-search"><input v-model="query" aria-label="搜索服务" placeholder="搜索服务名称或运行时" /></div><div class="ops-service-grid"><article v-for="item in filteredServices" :key="String(item.id)" class="paper-panel ops-service"><div class="ops-service-head"><strong>{{ item.name || item.id }}</strong><span>{{ status(item.status || item.configurationStatus) }}</span></div><p>{{ item.description || item.purpose || item.detail || '当前服务的配置与运行状态。' }}</p><small>{{ item.runtime || item.type || '服务' }}</small><DataList :value="item" :limit="7" /></article></div><p v-if="!filteredServices.length" class="ops-muted">没有匹配的服务。</p><section class="paper-panel"><div class="panel-heading"><strong>基础设施</strong></div><DataList :value="dashboard.infrastructure || {}" :limit="20" /></section></div>
    <div v-else-if="view === 'workspaces'" class="ops-detail-layout"><div class="ops-list"><input v-model="query" aria-label="搜索工作空间" placeholder="搜索任务或状态" /><button v-for="item in filteredProjects" :key="String(item.id)" :class="['ops-list-item',{active:selectedProject?.id===item.id}]" @click="selectedProjectId = String(item.id)"><strong>{{ item.name }}</strong><span>{{ (item.health as Row)?.status }} · {{ (item.health as Row)?.score ?? '—' }} 分</span><small>{{ (item.health as Row)?.summary }}</small></button><p v-if="!filteredProjects.length" class="ops-muted">当前没有工作空间记录。</p></div><section v-if="selectedProject" class="paper-panel ops-detail"><div class="panel-heading"><strong>{{ selectedProject.name }}</strong><span>{{ (selectedProject.health as Row)?.status }}</span></div><p>{{ (selectedProject.health as Row)?.summary }}</p><div class="ops-facts"><div><strong>{{ (selectedProject.health as Row)?.score ?? '—' }}</strong><span>健康评分</span></div><div><strong>{{ (selectedProject.health as Row)?.blockers ?? 0 }}</strong><span>阻断</span></div><div><strong>{{ (selectedProject.health as Row)?.warnings ?? 0 }}</strong><span>警告</span></div></div><div class="ops-actions"><button class="primary-button" :disabled="busy" @click="validate">重新验证</button><RouterLink class="secondary-button" :to="`/${selectedProject.id}/chat`">打开研究任务 →</RouterLink></div><h3>验收与数据质量</h3><DataList :value="selectedProject.validation || {}" /><DataList :value="selectedProject.dataQuality || {}" /><h3>关键产物</h3><DataList :value="selectedProject.artifacts || []" :limit="20" /></section></div>
    <div v-else-if="view === 'trace'" class="ops-detail-layout"><div class="ops-list"><input v-model="query" aria-label="搜索生成链路" placeholder="搜索任务或链路状态" /><button v-for="item in filteredTraces" :key="String(item.id)" :class="['ops-list-item',{active:selectedTrace?.id===item.id}]" @click="selectedTraceId = String(item.id)"><strong>{{ item.name }}</strong><span>{{ (item.trace as Row)?.status }} · {{ (item.trace as Row)?.eventCount ?? 0 }} 事件</span><small>{{ (item.trace as Row)?.summary }}</small></button><p v-if="!filteredTraces.length" class="ops-muted">暂无生成链路记录。</p></div><section v-if="selectedTrace" class="paper-panel ops-detail"><div class="panel-heading"><strong>{{ selectedTrace.name }}</strong><span>{{ (selectedTrace.trace as Row)?.status }}</span></div><p>{{ (selectedTrace.trace as Row)?.summary }}</p><div class="ops-facts"><div><strong>{{ (selectedTrace.trace as Row)?.requestCount ?? 0 }}</strong><span>请求</span></div><div><strong>{{ (selectedTrace.trace as Row)?.toolCallCount ?? 0 }}</strong><span>工具调用</span></div><div><strong>{{ (selectedTrace.trace as Row)?.errorCount ?? 0 }}</strong><span>错误</span></div></div><h3>执行阶段</h3><div class="ops-stages"><div v-for="(item,index) in (selectedTrace.stages as Row[] || [])" :key="index"><strong>{{ item.label || item.id }}</strong><span>{{ item.status }}</span></div></div><h3>最近事件</h3><div class="ops-timeline"><div v-for="(item,index) in (selectedTrace.timeline as Row[] || []).slice(-30)" :key="index"><small>{{ date(item.timestamp || item.createdAt) }} · {{ item.stage }}</small><p>{{ item.message || item.summary || item.type }}</p></div></div><RouterLink class="secondary-button" :to="`/${selectedTrace.id}/chat`">打开研究任务 →</RouterLink></section></div>
    <div v-else class="ops-detail-layout"><div class="ops-list"><input v-model="query" aria-label="搜索日志" placeholder="搜索日志内容" /><select v-model="logLevel" aria-label="日志级别"><option value="all">全部级别</option><option value="error">错误</option><option value="warn">警告</option><option value="info">信息</option></select><button v-for="item in logSources" :key="String(item.id)" :class="['ops-list-item',{active:activeLog?.id===item.id}]" @click="logId = String(item.id)"><strong>{{ item.label || item.id }}</strong><span>{{ item.exists ? `${item.lineCount || 0} 行` : '不可用' }}</span><small>{{ item.type || '日志源' }}</small></button><p v-if="!logSources.length" class="ops-muted">没有可读取的日志源。</p></div><section class="paper-panel ops-detail"><div class="panel-heading"><strong>{{ activeLog?.label || '运行日志' }}</strong><span>{{ logEntries.length }} 行</span></div><p v-if="activeLog?.error" class="inline-error">{{ activeLog.error }}</p><a v-if="activeLog?.externalUrl" :href="String(activeLog.externalUrl)" target="_blank" rel="noopener noreferrer">在日志服务中打开 ↗</a><div class="ops-log-lines"><div v-for="item in logEntries" :key="String(item.id)"><time>{{ date(item.timestamp) }}</time><b :class="String(item.level || '').toLowerCase()">{{ item.level || 'LOG' }}</b><code>{{ item.message || item.raw }}</code></div><p v-if="!logEntries.length">此筛选条件下没有日志。</p></div></section></div>
  </div>
</template>

<style scoped>
.ops-center{margin-top:15px}.ops-tabs{display:flex;gap:3px;border-bottom:1px solid #dcded4;margin-bottom:18px;overflow:auto}.ops-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;padding:12px 15px;color:#69736b;white-space:nowrap;font-size:12px;font-weight:700}.ops-tabs button.active{border-color:#a56e27;color:#263c33}.ops-tabs .ops-refresh{margin-left:auto;color:#a56e27}.ops-overview{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.ops-feature{background:linear-gradient(115deg,#283e35,#40564a);border-radius:9px;color:#fff;padding:29px}.ops-feature .eyebrow{color:#d9b879}.ops-feature h2{font:600 25px Georgia,'Noto Serif SC',serif;margin:16px 0 8px}.ops-feature p{font-size:12px;color:#e5e9e0;line-height:1.7}.ops-feature-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}.ops-feature-actions button{border:1px solid #9bac9f;background:transparent;color:white;border-radius:6px;padding:8px 11px;font-size:12px}.ops-check{border-bottom:1px solid #e1e3d8;padding:10px 0;font-size:12px}.ops-check span{float:right;color:#a56e27}.ops-check p,.ops-muted{font-size:12px;color:#69736b;line-height:1.6;margin:5px 0}.ops-services{display:grid;gap:14px}.ops-search input,.ops-list input,.ops-list select{width:100%;border:1px solid #dcded4;background:#fffefa;border-radius:6px;padding:10px;font-size:12px}.ops-service-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}.ops-service{min-height:210px}.ops-service-head{display:flex;justify-content:space-between;gap:10px;font-size:13px}.ops-service-head span{color:#a56e27;font-size:11px}.ops-service p,.ops-detail>p{font-size:12px;color:#69736b;line-height:1.6;margin:10px 0}.ops-service small{font-size:10px;color:#a56e27}.ops-detail-layout{display:grid;grid-template-columns:minmax(230px,310px) minmax(0,1fr);gap:14px;align-items:start}.ops-list{display:grid;gap:7px;max-height:900px;overflow:auto}.ops-list-item{display:grid;gap:6px;text-align:left;border:1px solid #e0dfd5;border-radius:7px;background:#fcfbf7;padding:12px}.ops-list-item.active{background:#f6f0e5;border-color:#af7c37}.ops-list-item strong{font-size:13px}.ops-list-item span,.ops-list-item small{font-size:11px;color:#69736b;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.ops-facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:15px 0}.ops-facts div{background:#f3f1e9;border-radius:6px;padding:12px}.ops-facts strong,.ops-facts span{display:block}.ops-facts strong{font-size:20px}.ops-facts span{font-size:10px;color:#69736b;margin-top:4px}.ops-actions{display:flex;gap:8px;flex-wrap:wrap}.ops-detail h3{font-size:14px;margin:21px 0 10px;border-bottom:1px solid #e1e3d8;padding-bottom:7px}.ops-stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:7px}.ops-stages div{background:#f4f1e9;padding:9px;border-radius:5px;font-size:11px}.ops-stages strong{display:block}.ops-stages span{color:#a56e27}.ops-timeline{max-height:380px;overflow:auto;border-left:2px solid #d7c5a6;padding-left:15px}.ops-timeline>div{padding:10px 0;border-bottom:1px solid #e1e3d8}.ops-timeline small{color:#a56e27}.ops-timeline p{font-size:12px;line-height:1.6;margin:4px 0}.ops-log-lines{background:#25302a;color:#edf0e8;border-radius:6px;padding:10px;max-height:660px;overflow:auto;margin-top:13px}.ops-log-lines>div{display:grid;grid-template-columns:150px 58px minmax(0,1fr);gap:8px;padding:5px 0;border-bottom:1px solid #3d4a42;font-size:11px}.ops-log-lines time{color:#a9b5ab}.ops-log-lines b{color:#b9ccbd}.ops-log-lines b.error,.ops-log-lines b.fatal{color:#f6a398}.ops-log-lines b.warn,.ops-log-lines b.warning{color:#e7ca8c}.ops-log-lines code{white-space:pre-wrap;overflow-wrap:anywhere}.ops-notice{background:#eaf4ec;color:#337453;border-radius:6px;padding:10px;font-size:12px}@media(max-width:820px){.ops-overview,.ops-detail-layout{grid-template-columns:1fr}.ops-list{max-height:290px}.ops-log-lines>div{grid-template-columns:80px 46px minmax(0,1fr)}}
</style>
