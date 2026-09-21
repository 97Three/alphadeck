<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';
import MarketMemberDetail from '../components/MarketMemberDetail.vue';
import SectorFlow from '../components/SectorFlow.vue';
import FoundationPanel from '../components/FoundationPanel.vue';
import FactorCatalog from '../components/FactorCatalog.vue';
import FinancialWiki from '../components/FinancialWiki.vue';
import DataList from '../components/DataList.vue';

type Row = Record<string, unknown>;
type StrategyData = { summary?: Row; templates?: Row[]; research?: Row; foundation?: Row; factorCatalog?: Row; scanJobs?: Row[]; scanRuns?: Row[] };
const router = useRouter();
const data = ref<StrategyData>({});
const loading = ref(true);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const view = ref<'templates' | 'universe' | 'quote' | 'jobs' | 'screening' | 'sector' | 'factors' | 'foundation' | 'knowledge' | 'compare'>('universe');
const selectedId = ref('');
const keyword = ref('');
const family = ref('all');
const universeId = ref('');
const symbol = ref('600519');
const start = ref('');
const end = ref('');
const queryResult = ref<unknown>(null);
const queryTitle = ref('');
const emptyBarsResult = computed(() => queryTitle.value === 'symbol-bars' && queryResult.value !== null && typeof queryResult.value === 'object' && Array.isArray((queryResult.value as Row).bars) && ((queryResult.value as Row).bars as unknown[]).length === 0);
const jobs = ref<Row[]>([]);
const members = ref<Row[]>([]);
const memberPage = ref(1);
const memberPageSize = ref(20);
const memberTotal = ref(0);
const memberTotalPages = ref(1);
const memberSearch = ref('');
const memberLoading = ref(false);
const memberError = ref('');
const selectedMemberSymbol = ref('');
const quoteMode = ref('symbol-bars');
const newMember = ref('');
const screenMode = ref('short_term');
const qualityPersist = ref(false);
const batchSize = ref(5);
const batchStart = ref(1);
const rangeMode = ref<'year' | 'five' | 'custom'>('five');
const ingestionStart = ref('');
const ingestionEnd = ref('');
const ingestionProvider = ref<'eastmoney' | 'baostock'>('eastmoney');
const jobsLoading = ref(false);
const jobsError = ref('');
const ingestionMessage = ref('');
const ingestionFailed = ref(false);
const ingestionSection = ref<HTMLElement | null>(null);
const scanSymbol = ref('');
const selected = computed(() => (data.value.templates || []).find(item => item.id === selectedId.value));
const families = computed(() => [...new Set((data.value.templates || []).map(item => String(item.family || '')).filter(Boolean))]);
const filteredTemplates = computed(() => (data.value.templates || []).filter(item => {
  const matchesFamily = family.value === 'all' || item.family === family.value;
  const text = `${item.name || ''} ${item.description || ''} ${item.family || ''} ${item.promptSeed || ''}`.toLowerCase();
  return matchesFamily && text.includes(keyword.value.trim().toLowerCase());
}));
const scanRun = computed<Row | null>(() => selected.value?.latestScanRun && typeof selected.value.latestScanRun === 'object' ? selected.value.latestScanRun as Row : null);
const scanResults = computed<Row[]>(() => Array.isArray(scanRun.value?.results) ? scanRun.value.results as Row[] : []);
const universes = computed(() => Array.isArray(data.value.research?.universes) ? data.value.research.universes as Row[] : []);
const selectedUniverse = computed<Row>(() => universes.value.find(item => item.id === universeId.value) || universes.value[0] || {});
const coverageTotal = computed(() => Number(selectedUniverse.value.memberCount || memberTotal.value || 0));
const coverageReady = computed(() => Number(selectedUniverse.value.readyCount || 0));
const coveragePercent = computed(() => coverageTotal.value ? Math.min(100, Math.round(coverageReady.value / coverageTotal.value * 100)) : 0);
const batchEnd = computed(() => Math.min(coverageTotal.value, batchStart.value + Number(batchSize.value || 0) - 1));
const recentJobs = computed(() => jobs.value.slice(0, 5));
const summary = computed(() => data.value.summary || {});
const title = (item: Row) => String(item.name || item.symbol || item.id || '未命名');
const value = (item: unknown): string => item == null || item === '' ? '—' : typeof item === 'object' ? Array.isArray(item) ? item.join('、') : Object.entries(item as Row).map(([key, val]) => `${key}: ${val}`).join(' · ') : String(item);
const numeric = (item: unknown): number | null => item == null || item === '' || !Number.isFinite(Number(item)) ? null : Number(item);
const amount = (item: unknown): string => numeric(item) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(numeric(item)!);
const signed = (item: unknown): string => numeric(item) === null ? '—' : `${numeric(item)! > 0 ? '+' : ''}${amount(item)}%`;
const money = (item: unknown): string => numeric(item) === null ? '—' : Math.abs(numeric(item)!) >= 1e8 ? `${amount(numeric(item)! / 1e8)} 亿` : Math.abs(numeric(item)!) >= 1e4 ? `${amount(numeric(item)! / 1e4)} 万` : amount(item);
const memberState = (item: Row): string => numeric(item.rowCount) ? `${amount(item.rowCount)} 根 · 至 ${String(item.lastTs || '').slice(0, 10)}` : '尚未补数';
const trendLabel = (item: unknown): string => item === 'bullish' ? '多头' : item === 'bearish' ? '空头' : item === 'sideways' ? '震荡' : '样本不足';
const jobStatus = (item: unknown): string => ({ completed: '已完成', partial: '部分完成', failed: '失败', running: '进行中', pending: '等待中', paused: '已暂停', stopped: '已停止' } as Record<string, string>)[String(item)] || '未知状态';
const providerName = (item: unknown): string => String(item || '').includes('baostock') ? 'Baostock' : String(item || '').includes('eastmoney') ? '东方财富' : '行情源';
const jobTime = (item: unknown): string => typeof item === 'string' && item ? new Date(item).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
const jobFailure = (item: Row): string => String(item.error || (item.metadata && typeof item.metadata === 'object' ? (item.metadata as Row).error || '' : '') || '请在任务详情查看失败标的');
function goToIngestion() { view.value = 'universe'; void nextTick(() => ingestionSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })); }
const resultTitles: Record<string, string> = { 'symbol-bars': '历史 K 线', 'realtime-quote': '实时行情', 'symbol-dividends': '分红记录', 'intraday-bars': '分时行情', 'technical-indicators': '技术指标', 'financial-reports': '财务报表', 'fundamental-indicators': '基本面指标', announcements: '公告', 'resolve-symbol': '标的查询', 'trading-calendar': '交易日历', 'a-share-screener': '候选股票', 'data-quality-scan': '数据质量检查' };

async function load() {
  if (!data.value.research) loading.value = true;
  try {
    data.value = unwrap(await apiRequest<ApiEnvelope<StrategyData>>('/api/quant/strategies')) || {};
    if (!selectedId.value) selectedId.value = String(data.value.templates?.[0]?.id || '');
    if (!universeId.value) universeId.value = String(data.value.research?.primaryUniverseId || universes.value[0]?.id || '');
    error.value = '';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '策略数据读取失败'; }
  finally { loading.value = false; }
}
async function action(name: string, options: Row = {}) {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<unknown>>('/api/quant/strategies', { method: 'POST', body: JSON.stringify({ action: name, ...options }) }));
    queryResult.value = name in resultTitles ? result : null;
    queryTitle.value = name;
    notice.value = '操作已完成';
    return result;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败'; return null; }
  finally { busy.value = false; }
}
async function queryMarket() { const limit = quoteMode.value === 'symbol-bars' ? 1260 : quoteMode.value === 'technical-indicators' ? 120 : quoteMode.value === 'trading-calendar' ? 260 : 8; await action(quoteMode.value, { symbol: symbol.value.trim(), query: symbol.value.trim(), limit, period: 'daily', adjustment: 'qfq', start: start.value || undefined, end: end.value || undefined }); }
async function loadMembers(page = memberPage.value) {
  if (!universeId.value) return;
  memberLoading.value = true; memberError.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<Row>>('/api/quant/strategies', { method: 'POST', body: JSON.stringify({ action: 'universe-members', universeId: universeId.value, page, pageSize: memberPageSize.value, keyword: memberSearch.value.trim() }) })) || {};
    members.value = Array.isArray(result.members) ? result.members as Row[] : [];
    memberPage.value = Number(result.page) || page;
    memberTotal.value = Number(result.total) || 0;
    memberTotalPages.value = Number(result.totalPages) || 1;
  } catch (cause) { memberError.value = cause instanceof Error ? cause.message : '读取股票池失败'; members.value = []; }
  finally { memberLoading.value = false; }
}
async function loadJobs() {
  if (!universeId.value) return;
  jobsLoading.value = true;
  jobsError.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<Row>>('/api/quant/strategies', { method: 'POST', body: JSON.stringify({ action: 'ingestion-jobs', universeId: universeId.value, limit: 30 }) })) || {};
    jobs.value = Array.isArray(result.jobs) ? result.jobs as Row[] : [];
  } catch (cause) { jobsError.value = cause instanceof Error ? cause.message : '读取补数记录失败'; }
  finally { jobsLoading.value = false; }
}
async function startBatch() {
  if (!Number.isInteger(batchStart.value) || batchStart.value < 1 || batchStart.value > coverageTotal.value || !Number.isInteger(batchSize.value) || batchSize.value < 1 || batchSize.value > 10) { error.value = '请检查起始位置和每批数量。'; return; }
  if (rangeMode.value === 'custom' && (!ingestionStart.value || !ingestionEnd.value || ingestionStart.value > ingestionEnd.value || ingestionStart.value > new Date().toISOString().slice(0, 10))) { error.value = '请填写有效的开始和结束日期。'; return; }
  const customYears = rangeMode.value === 'custom' ? Math.ceil((Date.now() - Date.parse(ingestionStart.value)) / 31_557_600_000) + 1 : 0;
  if (customYears > 30) { error.value = '自定义日期最多可补近 30 年，请调整开始日期。'; return; }
  const rangeLabel = rangeMode.value === 'year' ? '近 1 年' : rangeMode.value === 'five' ? '近 5 年' : `${ingestionStart.value} 至 ${ingestionEnd.value}`;
  if (!window.confirm(`将用${ingestionProvider.value === 'eastmoney' ? '东方财富' : 'Baostock'}补充${selectedUniverse.value.name || '当前股票池'}第 ${batchStart.value}～${batchEnd.value} 只标的的${rangeLabel}日线，并写入本地数据库。确定继续？`)) return;
  const lookbackYears = rangeMode.value === 'year' ? 1 : rangeMode.value === 'five' ? 5 : Math.max(1, customYears);
  ingestionFailed.value = false;
  ingestionMessage.value = `正在补第 ${batchStart.value}～${batchEnd.value} 只标的，请保持页面打开。`;
  const result = await action('run-ingestion-batch', { universeId: universeId.value, provider: ingestionProvider.value, offset: batchStart.value - 1, batchSize: batchSize.value, limit: Math.min(20000, lookbackYears * 260), lookbackYears, start: rangeMode.value === 'custom' ? ingestionStart.value : undefined, end: rangeMode.value === 'custom' ? ingestionEnd.value : undefined, period: 'daily', adjustment: 'qfq' }) as Row | null;
  if (!result) { ingestionFailed.value = true; ingestionMessage.value = error.value || '补数失败，请稍后重试。'; return; }
  const failed = Number(result.failed_symbols || 0);
  const completed = Number(result.completed_symbols || 0);
  const failures = Array.isArray(result.symbols)
    ? (result.symbols as Row[]).filter(item => item.status === 'failed').map(item => `${item.symbol || '未知标的'}：${item.error || '上游取数失败'}`).slice(0, 3)
    : [];
  const outcome = `本批完成 ${completed} 个标的${failed ? `，失败 ${failed} 个` : ''}；写入 ${result.rows_upserted || 0} 根 K 线。${failures.length ? `失败原因：${failures.join('；')}` : ''}`;
  if (!failed && Number.isFinite(Number(result.next_offset))) batchStart.value = Number(result.next_offset) + 1;
  await Promise.all([load(), loadJobs(), loadMembers()]);
  notice.value = outcome;
  ingestionMessage.value = outcome;
  ingestionFailed.value = failed > 0;
  if (failed) error.value = failures.join('；') || `${failed} 只标的补数失败，请查看最近批次。`;
}
async function startAutofill() { if (!window.confirm('自动补数会连续处理当前股票池并写入本地数据库。确定启动？')) return; await action('start-ingestion-autofill', { universeId: universeId.value, batchSize: batchSize.value, maxBatches: 20, lookbackYears: 5, period: 'daily', adjustment: 'qfq' }); await loadJobs(); await loadMembers(); }
async function addMember() { if (!newMember.value.trim()) return; const result = await action('add-universe-member', { universeId: universeId.value, query: newMember.value.trim(), syncHistory: false }); if (result) { newMember.value = ''; await Promise.all([load(), loadMembers(1)]); } }
async function runScan(now: boolean) { if (!selectedId.value) return; const result = await action(now ? 'run-scan-now' : 'run-scan', { templateId: selectedId.value, symbol: scanSymbol.value.trim() || undefined }); if (result) { await load(); view.value = 'compare'; } }
async function runQualityScan() { if (qualityPersist.value && !window.confirm('质量扫描结果会写入本地数据库。确定继续？')) return; await action('data-quality-scan', { universeId: universeId.value, persist: qualityPersist.value, lookbackYears: 5 }); }
async function controlJob(control: 'pause' | 'resume' | 'stop', id: string) { if (!id) return; await action('control-ingestion-job', { jobId: id, control, reason: '策略平台手动操作' }); await loadJobs(); }
function research() { void router.push({ path: '/', query: { question: String(selected.value?.promptSeed || selected.value?.description || '') } }); }
watch([view, universeId, memberPageSize], () => { if (view.value === 'universe') { selectedMemberSymbol.value = ''; void loadMembers(1); } });
watch(universeId, () => { batchStart.value = 1; jobs.value = []; void loadJobs(); });
let searchTimer: ReturnType<typeof setTimeout> | undefined;
watch(memberSearch, () => { if (searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(() => { if (view.value === 'universe') void loadMembers(1); }, 300); });
onMounted(load);
</script>

<template>
  <main class="workspace-main module-page strategy-page">
    <div class="breadcrumb">AlphaDeck　/　策略实验室</div><div class="eyebrow">STRATEGY LAB</div>
    <div class="module-heading"><div><h1>策略实验室</h1><p>先确认数据覆盖，再研究策略。每个结果都能追溯到当前行情库。</p></div><div class="strategy-heading-actions"><button class="primary-button" type="button" @click="goToIngestion">补齐股票池行情 →</button><button class="secondary-button" type="button" @click="load">刷新</button></div></div>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="module-notice">{{ notice }}</p>
    <div v-if="loading" class="loading-state">正在读取策略与数据状态…</div>
    <template v-else>
      <div class="strategy-stats"><div><strong>{{ value(summary.templates) }}</strong><span>策略模板</span></div><div><strong>{{ value(summary.trackedSymbols) }}</strong><span>跟踪标的</span></div><div><strong>{{ value(summary.syncedSymbols) }}</strong><span>已同步标的</span></div><div><strong>{{ value(summary.syncedBars) }}</strong><span>K 线记录</span></div></div>
      <div class="workspace-tabs strategy-tabs"><button v-for="item in [{id:'universe',label:'股票池与补数'},{id:'templates',label:'策略目录'},{id:'quote',label:'行情查询'},{id:'screening',label:'筛选与扫描'},{id:'jobs',label:'补数记录'}]" :key="item.id" type="button" :class="{ active: view === item.id }" @click="view = item.id as typeof view">{{ item.label }}</button><details class="strategy-more"><summary>更多工具</summary><div><button v-for="item in [{id:'sector',label:'板块资金'},{id:'compare',label:'扫描与参数对比'},{id:'factors',label:'因子目录'},{id:'foundation',label:'基础组件'},{id:'knowledge',label:'金融知识'}]" :key="item.id" type="button" :class="{ active: view === item.id }" @click="view = item.id as typeof view">{{ item.label }}</button></div></details></div>
      <div v-if="view === 'templates'" class="strategy-grid"><section class="paper-panel"><div class="panel-heading"><strong>策略模板</strong><span>{{ filteredTemplates.length }} / {{ data.templates?.length || 0 }} 项</span></div><div class="strategy-filters"><input v-model="keyword" aria-label="搜索策略" placeholder="搜索名称、条件或规则" /><select v-model="family" aria-label="策略类别"><option value="all">全部类别</option><option v-for="item in families" :key="item" :value="item">{{ item }}</option></select></div><button v-for="item in filteredTemplates" :key="String(item.id)" class="strategy-item" :class="{ active: selectedId === item.id }" type="button" @click="selectedId = String(item.id)"><strong>{{ title(item) }}</strong><small>{{ value(item.family) }} · {{ value(item.status) }}</small></button><p v-if="!filteredTemplates.length" class="strategy-empty">没有匹配的策略，试试其他关键词。</p></section><section v-if="selected" class="paper-panel strategy-detail"><div class="panel-heading"><strong>{{ title(selected) }}</strong><span>{{ value(selected.readiness && (selected.readiness as Row).label) }}</span></div><p>{{ value(selected.description) }}</p><div class="readiness-note">{{ value(selected.readiness && (selected.readiness as Row).summary) }}</div><h3>默认标的</h3><p>{{ value(selected.defaultSymbols) }}</p><h3>参数与规则</h3><dl><div v-for="(item, index) in (selected.parameterSchema as Row[] || [])" :key="index"><dt>{{ item.label }}</dt><dd>{{ value(item.value) }} {{ value(item.unit) }}</dd></div></dl><h3>数据限制</h3><ul><li v-for="(item, index) in (selected.limitations as string[] || [])" :key="index">{{ item }}</li></ul><div class="module-actions"><button class="primary-button" type="button" @click="research">带着这个策略发起研究 →</button><button class="secondary-button" type="button" @click="view = 'compare'">查看扫描和参数对比</button></div></section></div>
      <section v-else-if="view === 'universe'" ref="ingestionSection" class="strategy-work ingest-workbench">
        <div class="ingest-hero">
          <div>
            <span class="ingest-kicker">股票池 · 历史行情</span>
            <h2>先把行情补齐，再开始研究</h2>
            <p>选择股票池和日期范围，按顺序补入本地 K 线。每批完成后会自动指向下一批。</p>
          </div>
          <div class="ingest-hero-actions">
            <div class="ingest-coverage-number"><strong>{{ coverageReady }}</strong><span>/ {{ coverageTotal }} 只已有行情</span></div>
            <button class="primary-button ingest-start-button" type="button" :disabled="busy || !universeId || batchStart > coverageTotal" @click="startBatch">{{ busy ? '正在补数…' : '补下一批 · ' + batchSize + ' 只' }}</button>
          </div>
        </div>
        <div class="ingest-progress" role="progressbar" :aria-valuenow="coverageReady" :aria-valuemax="coverageTotal" aria-label="股票池行情覆盖"><div :style="{ width: coveragePercent + '%' }"></div></div>
        <p v-if="ingestionMessage" class="ingest-feedback" :class="{ error: ingestionFailed }" role="status">{{ ingestionMessage }}</p>
        <div class="ingest-grid">
          <div class="paper-panel ingest-setup">
            <div class="ingest-step"><span>01</span><div><strong>选择股票池</strong><small>只会补入你选中的这一组标的</small></div></div>
            <div class="ingest-universe-options">
              <button v-for="item in universes" :key="String(item.id)" type="button" :class="{ active: universeId === item.id }" @click="universeId = String(item.id)"><strong>{{ title(item) }}</strong><small>{{ amount(item.memberCount) }} 只标的</small></button>
            </div>
            <div class="ingest-step"><span>02</span><div><strong>选择时间范围</strong><small>默认近 5 年日线；已存在的日期会更新，不会重复插入</small></div></div>
            <div class="ingest-range-options" role="group" aria-label="补数时间范围">
              <button type="button" :class="{ active: rangeMode === 'year' }" @click="rangeMode = 'year'">近 1 年</button>
              <button type="button" :class="{ active: rangeMode === 'five' }" @click="rangeMode = 'five'">近 5 年</button>
              <button type="button" :class="{ active: rangeMode === 'custom' }" @click="rangeMode = 'custom'">自定义日期</button>
            </div>
            <div v-if="rangeMode === 'custom'" class="ingest-date-fields"><label>开始日期<input v-model="ingestionStart" type="date" /></label><label>结束日期<input v-model="ingestionEnd" type="date" /></label></div>
            <div class="ingest-step"><span>03</span><div><strong>分批执行</strong><small>单批最多 10 只；网络慢时建议保持 5 只</small></div></div>
            <div class="ingest-batch-controls"><label>从第几只开始<input v-model.number="batchStart" type="number" min="1" :max="Math.max(1, coverageTotal)" /></label><label>每批补几只<input v-model.number="batchSize" type="number" min="1" max="10" /></label></div>
            <p v-if="coverageTotal === 0" class="ingest-batch-preview">这个股票池还没有标的，可以先在下方添加。</p>
            <p v-else-if="batchStart > coverageTotal" class="ingest-batch-preview">已处理到股票池末尾。需要重新检查时，可把起始位置改为 1。</p>
            <p v-else class="ingest-batch-preview">本次预计处理第 <strong>{{ batchStart }}～{{ batchEnd }}</strong> 只。完成后可继续点击“补下一批”。</p>
            <details class="ingest-advanced"><summary>备用数据源与高级操作</summary><label>数据来源<select v-model="ingestionProvider"><option value="eastmoney">东方财富（当前推荐）</option><option value="baostock">Baostock</option></select></label><p>当前网络下 Baostock 登录可能超时；建议先用东方财富。Baostock 的连续补数仅在其服务恢复后使用。</p><button class="secondary-button" type="button" :disabled="busy || ingestionProvider !== 'baostock'" @click="startAutofill">Baostock 连续补数</button></details>
          </div>
          <aside class="paper-panel ingest-recent">
            <div class="panel-heading"><strong>最近补数</strong><button type="button" class="text-button" :disabled="jobsLoading" @click="loadJobs">{{ jobsLoading ? '刷新中…' : '刷新进度' }}</button></div>
            <p v-if="jobsError" class="inline-error" role="alert">{{ jobsError }}</p>
            <p v-else-if="!recentJobs.length" class="ingest-empty">还没有补数记录。选好范围后，点击右上方“补下一批”。</p>
            <div v-for="item in recentJobs" :key="String(item.id)" class="ingest-job">
              <div><strong>{{ providerName(item.provider) }} · {{ jobTime(item.startedAt || item.createdAt) }}</strong><span class="ingest-job-status" :class="String(item.status)">{{ jobStatus(item.status) }}</span></div>
              <p>完成 {{ amount(item.completedSymbols) }} / {{ amount(item.totalSymbols) }} 只 · 写入 {{ amount(item.rowsUpserted) }} 根 K 线</p>
              <small v-if="Number(item.failedSymbols || 0)" class="ingest-job-error">{{ amount(item.failedSymbols) }} 只失败：{{ jobFailure(item) }}</small>
            </div>
            <button type="button" class="text-button ingest-all-jobs" @click="view = 'jobs'">查看全部补数记录 →</button>
          </aside>
        </div>
        <div class="paper-panel ingest-members">
          <div class="panel-heading"><div><strong>股票池明细</strong><span>点击任意标的，查看本地 K 线和每日行情</span></div><span>{{ amount(memberTotal) }} 只</span></div>
          <div class="ingest-member-add"><input v-model="newMember" aria-label="新增标的代码或名称" placeholder="输入代码或名称，例如 600519" /><button class="secondary-button" type="button" :disabled="busy || !newMember.trim()" @click="addMember">加入股票池</button></div>
          <div class="member-controls"><input v-model="memberSearch" aria-label="筛选股票池标的" placeholder="筛选名称、代码、板块或交易所" /><label>每页 <select v-model.number="memberPageSize"><option :value="10">10</option><option :value="20">20</option><option :value="50">50</option></select> 条</label><span>第 {{ memberPage }} / {{ memberTotalPages }} 页</span><button :disabled="memberLoading || memberPage <= 1" @click="loadMembers(memberPage - 1)">上一页</button><button :disabled="memberLoading || memberPage >= memberTotalPages" @click="loadMembers(memberPage + 1)">下一页</button></div>
        <p v-if="memberError" class="inline-error" role="alert">{{ memberError }}</p><p v-if="memberLoading">正在读取股票池行情…</p>
        <div v-else class="table-scroll member-table"><table><thead><tr><th>标的</th><th>行情</th><th>强弱</th><th>趋势</th><th>流动性</th><th>估值</th><th>本地数据</th></tr></thead><tbody><template v-for="item in members" :key="String(item.symbol)"><tr class="member-row" :class="{ selected: selectedMemberSymbol === item.symbol }" :aria-expanded="selectedMemberSymbol === item.symbol" tabindex="0" @click="selectedMemberSymbol = selectedMemberSymbol === item.symbol ? '' : String(item.symbol)" @keydown.enter="selectedMemberSymbol = selectedMemberSymbol === item.symbol ? '' : String(item.symbol)"><td><strong>{{ title(item) }}</strong><small>{{ value(item.symbol || item.code) }} · {{ value(item.exchange) }}</small><span v-if="Array.isArray(item.sectorTags) && item.sectorTags.length" class="sector-tags">{{ (item.sectorTags as string[]).slice(0, 2).join(' · ') }}</span></td><td><strong>{{ amount(item.latestClose) }}</strong><small :class="{ positive: (numeric(item.latestChangePct) || 0) > 0, negative: (numeric(item.latestChangePct) || 0) < 0 }">{{ signed(item.latestChangePct) }}</small></td><td><span>20 日 {{ signed(item.strength20dPct) }}</span><small>60 日 {{ signed(item.strength60dPct) }}</small></td><td><strong>{{ trendLabel(item.trendStatus) }}</strong><small>MA20 {{ amount(item.ma20) }} · MA60 {{ amount(item.ma60) }}</small></td><td><strong>{{ money(item.avgAmount20d) }}</strong><small>20 日平均成交额</small></td><td><strong>PE {{ amount(item.peTtm) }}</strong><small>PB {{ amount(item.pbMrq) }}</small></td><td><strong>{{ memberState(item) }}</strong><small>{{ value(item.dataProvider) }}</small></td></tr><tr v-if="selectedMemberSymbol === item.symbol" class="member-detail-row"><td colspan="7"><MarketMemberDetail :member="item" /></td></tr></template><tr v-if="!members.length"><td colspan="7">没有匹配的标的。可以调整筛选词或股票池。</td></tr></tbody></table></div></div></section>
      <section v-else-if="view === 'quote'" class="paper-panel strategy-work"><div class="panel-heading"><strong>行情与基础数据</strong><span>只读</span></div><div class="module-form"><label>股票代码或名称<input v-model="symbol" placeholder="例如 600519" /></label><label>查询内容<select v-model="quoteMode"><option value="symbol-bars">历史 K 线</option><option value="realtime-quote">实时行情</option><option value="symbol-dividends">分红</option><option value="intraday-bars">分时行情</option><option value="technical-indicators">技术指标</option><option value="financial-reports">财务报表</option><option value="fundamental-indicators">基本面指标</option><option value="announcements">公告</option><option value="resolve-symbol">查找标的</option><option value="trading-calendar">交易日历</option></select></label><label v-if="quoteMode === 'trading-calendar'">开始日期<input v-model="start" type="date" /></label><label v-if="quoteMode === 'trading-calendar'">结束日期<input v-model="end" type="date" /></label></div><div class="module-actions"><button class="primary-button" :disabled="busy || (quoteMode !== 'trading-calendar' && !symbol.trim())" @click="queryMarket">查询</button></div></section>
      <section v-else-if="view === 'jobs'" class="paper-panel strategy-work ingest-history">
        <div class="panel-heading"><div><strong>补数记录</strong><p>按时间查看每次补数的结果，失败时可在这里定位原因。</p></div><button class="secondary-button" type="button" :disabled="jobsLoading" @click="loadJobs">{{ jobsLoading ? '刷新中…' : '刷新记录' }}</button></div>
        <p v-if="jobsError" class="inline-error" role="alert">{{ jobsError }}</p>
        <p v-else-if="!jobs.length" class="ingest-empty">还没有补数记录。回到股票池，选择范围后开始补数。</p>
        <div v-for="item in jobs" :key="String(item.id)" class="ingest-history-row">
          <div class="ingest-history-main"><strong>{{ providerName(item.provider) }} · {{ jobTime(item.startedAt || item.createdAt) }}</strong><span class="ingest-job-status" :class="String(item.status)">{{ jobStatus(item.status) }}</span></div>
          <p>完成 {{ amount(item.completedSymbols) }} / {{ amount(item.totalSymbols) }} 只 · 写入 {{ amount(item.rowsUpserted) }} 根 K 线</p>
          <p v-if="Number(item.failedSymbols || 0)" class="ingest-job-error">{{ amount(item.failedSymbols) }} 只失败：{{ jobFailure(item) }}</p>
          <div v-if="item.status === 'running' || item.status === 'paused'" class="ingest-job-actions"><button v-if="item.status === 'running'" type="button" :disabled="busy" @click="controlJob('pause', String(item.id))">暂停</button><button v-if="item.status === 'paused'" type="button" :disabled="busy" @click="controlJob('resume', String(item.id))">继续</button><button type="button" :disabled="busy" @click="controlJob('stop', String(item.id))">停止</button></div>
        </div>
        <button type="button" class="text-button" @click="goToIngestion">← 返回股票池补数</button>
      </section>
      <section v-else-if="view === 'screening'" class="paper-panel strategy-work"><div class="panel-heading"><strong>筛选与扫描</strong><span>基于当前股票池</span></div><div class="module-form"><label>股票池<select v-model="universeId"><option v-for="item in universes" :key="String(item.id)" :value="String(item.id)">{{ title(item) }}</option></select></label><label>筛选方式<select v-model="screenMode"><option value="short_term">短线候选</option><option value="limit_up_relay">涨停接力</option><option value="trend_liquidity">趋势与流动性</option></select></label></div><div class="module-actions"><button class="secondary-button" :disabled="busy" @click="action('a-share-screener', { universeId, mode: screenMode, limit: 20 })">查看候选股票</button><button class="secondary-button" @click="view = 'sector'">查看板块资金</button><button class="secondary-button" :disabled="busy" @click="runQualityScan">检查数据质量</button><label class="scan-check"><input v-model="qualityPersist" type="checkbox" /> 保存质量扫描结果</label></div><h3>运行策略扫描</h3><p>先在“策略目录”选中模板，再运行。异步运行会进入任务队列。</p><div class="module-form"><label>单个标的（可选）<input v-model="scanSymbol" placeholder="例如 600519" /></label></div><div class="module-actions"><button class="secondary-button" :disabled="busy || !selectedId" @click="runScan(false)">加入扫描队列</button><button class="primary-button" :disabled="busy || !selectedId" @click="runScan(true)">立即扫描</button></div></section>
      <section v-else-if="view === 'compare'" class="strategy-comparison"><div class="paper-panel"><div class="panel-heading"><strong>策略扫描与参数对比</strong><button class="secondary-button" @click="load">刷新结果</button></div><div class="module-form"><label>策略模板<select v-model="selectedId"><option v-for="item in data.templates" :key="String(item.id)" :value="String(item.id)">{{ title(item) }}</option></select></label></div><div v-if="scanRun" class="scan-summary"><div><strong>{{ scanRun.symbol || '—' }}</strong><span>扫描标的</span></div><div><strong>{{ scanRun.succeeded ?? '—' }} / {{ scanRun.total ?? '—' }}</strong><span>成功组合</span></div><div><strong>{{ scanRun.bestResultId || '—' }}</strong><span>最优参数 ID</span></div><div><strong>{{ scanRun.status || '—' }}</strong><span>扫描状态</span></div></div><div v-if="scanResults.length" class="table-scroll"><table><thead><tr><th>组合</th><th>状态</th><th>参数</th><th>收益率</th><th>最大回撤</th><th>胜率</th><th>Sharpe</th></tr></thead><tbody><tr v-for="item in scanResults" :key="String(item.id)" :class="{ best: item.id === scanRun?.bestResultId }"><td>{{ item.id }}</td><td>{{ item.status }}</td><td>{{ value(item.parameters) }}</td><td>{{ value((item.metrics as Row)?.totalReturnPct) }}</td><td>{{ value((item.metrics as Row)?.maxDrawdownPct) }}</td><td>{{ value((item.metrics as Row)?.winRatePct) }}</td><td>{{ value((item.metrics as Row)?.sharpe) }}</td></tr></tbody></table></div><div v-else class="scan-empty"><strong>还没有可比较的扫描结果</strong><p>到“筛选与扫描”选择策略，先加入队列或立即运行；完成后在这里对比参数表现。</p><button class="secondary-button" @click="view = 'screening'">去运行扫描 →</button></div></div><div class="paper-panel"><div class="panel-heading"><strong>最近扫描任务</strong><span>{{ data.scanJobs?.length || 0 }} 项</span></div><div v-for="item in data.scanJobs" :key="String(item.id)" class="strategy-job"><strong>{{ item.symbol || item.templateId }}</strong><span>{{ item.status }}</span><small>{{ item.error || item.updatedAt }}</small></div><p v-if="!data.scanJobs?.length" class="strategy-empty">暂无扫描任务。</p></div></section>
      <SectorFlow v-else-if="view === 'sector'" :universe-id="universeId" />
      <FactorCatalog v-else-if="view === 'factors'" :catalog="data.factorCatalog || {}" />
      <FoundationPanel v-else-if="view === 'foundation'" :foundation="data.foundation || {}" :universe-id="universeId" />
      <FinancialWiki v-else-if="view === 'knowledge'" />
      <section v-if="queryResult !== null" class="paper-panel strategy-response"><div class="panel-heading"><strong>{{ resultTitles[queryTitle] || '查询结果' }}</strong><button type="button" class="secondary-button" @click="queryResult = null">收起</button></div><p v-if="emptyBarsResult" class="strategy-empty">这只标的在本地还没有 K 线。可到“股票池与补数”查看覆盖情况，补数完成后再查询。</p><DataList v-else :value="queryResult" :limit="100" /></section>
    </template>
  </main>
</template>

<style scoped>
.module-heading{display:flex;justify-content:space-between;align-items:center;gap:20px}.module-heading p{color:#6f776f}.strategy-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.strategy-stats>div{background:#fffefa;border:1px solid #e1e3d8;border-radius:8px;padding:18px}.strategy-stats strong,.strategy-stats span{display:block}.strategy-stats strong{font-size:25px;color:#263c33}.strategy-stats span{font-size:12px;color:#6f776f;margin-top:6px}.strategy-grid{display:grid;grid-template-columns:320px 1fr;gap:16px;margin-top:16px}.strategy-item{width:100%;display:block;text-align:left;padding:13px;border:0;border-bottom:1px solid #e1e3d8;background:transparent}.strategy-item.active{background:#f5f1e8;color:#a06922}.strategy-item strong,.strategy-item small{display:block}.strategy-item small{margin-top:4px;color:#6f776f}.strategy-detail h3,.strategy-work h3{font-size:14px;margin:24px 0 8px}.strategy-detail p,.strategy-work p{color:#6f776f;font-size:13px;line-height:1.7}.readiness-note{padding:12px;background:#f5f1e8;border-left:3px solid #ad7730;font-size:13px}.strategy-detail li{margin-bottom:8px;font-size:12px;line-height:1.6}.strategy-detail .primary-button{margin-top:16px}.module-form{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.module-form label{display:grid;gap:6px;font-size:12px;color:#69736b}.module-form input,.module-form select{min-width:160px;padding:9px;border:1px solid #dcded4;border-radius:6px;background:white;color:#263c33}.module-actions{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0}.module-actions button{padding:9px 13px;border-radius:6px;border:1px solid #dcded4;background:white}.module-notice{color:#3a755e}.strategy-job,.factor-row{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #e1e3d8;font-size:12px}.strategy-job small{margin-left:auto}.factor-row strong{width:150px;flex:none}.factor-row span{overflow-wrap:anywhere}.strategy-response{margin-top:16px}.table-scroll{overflow:auto}.table-scroll table{width:100%;border-collapse:collapse;font-size:12px}.table-scroll th,.table-scroll td{text-align:left;padding:8px;border-bottom:1px solid #e1e3d8}.strategy-page dl div{display:flex;justify-content:space-between;gap:15px;padding:7px 0;border-bottom:1px solid #e1e3d8;font-size:12px}.strategy-page dd{margin:0;text-align:right;overflow-wrap:anywhere}@media(max-width:850px){.strategy-stats{grid-template-columns:repeat(2,1fr)}.strategy-grid{grid-template-columns:1fr}}
.strategy-filters{display:grid;gap:7px;padding:10px;border-bottom:1px solid #e1e3d8}.strategy-filters input,.strategy-filters select{border:1px solid #dcded4;background:#fff;border-radius:5px;padding:8px;font-size:12px;min-width:0}.strategy-empty{padding:15px;color:#6f776f;font-size:12px}.strategy-comparison{display:grid;gap:14px;margin-top:16px}.scan-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin:15px 0}.scan-summary div{border:1px solid #e1e3d8;background:#f6f4ed;border-radius:6px;padding:12px}.scan-summary strong,.scan-summary span{display:block;overflow-wrap:anywhere}.scan-summary strong{font-size:16px}.scan-summary span{font-size:11px;color:#6f776f;margin-top:5px}.scan-empty{padding:30px;background:#f7f5ef;border:1px dashed #d9d3c7;border-radius:7px;text-align:center}.scan-empty strong{display:block;font-size:15px}.scan-empty p{font-size:12px;color:#6f776f;margin:8px auto 15px;max-width:480px;line-height:1.6}.strategy-comparison tr.best{background:#f6eddb}.strategy-comparison .strategy-job{align-items:center}@media(max-width:650px){.scan-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}
.member-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:20px 0 12px}.member-controls input{flex:1;min-width:220px;padding:9px;border:1px solid #dcded4;border-radius:6px}.member-controls label{display:flex;align-items:center;gap:5px;font-size:12px}.member-controls select,.member-controls button{padding:7px 9px;border:1px solid #dcded4;border-radius:5px;background:white}.member-controls span{font-size:12px;color:#69736b}.member-table table{min-width:1120px}.member-table th{white-space:nowrap}.member-table td{vertical-align:top;padding:12px 9px}.member-table td strong,.member-table td small{display:block}.member-table td small{margin-top:4px;color:#737d73}.member-table .member-row{cursor:pointer}.member-table .member-row:hover,.member-table .member-row.selected{background:#f6f3e9}.member-table .sector-tags{display:block;color:#6f776f;font-size:11px;margin-top:5px}.member-table .positive{color:#a63e34}.member-table .negative{color:#16806a}.member-detail-row td{padding:0}.member-detail-row:hover{background:transparent}
.strategy-page .module-heading h1{font-size:clamp(28px,3vw,38px);margin:4px 0}.strategy-page .module-heading p{margin:5px 0 0}.strategy-page .strategy-stats{gap:0;margin:15px 0 12px;border:1px solid #dedfd5;border-radius:8px;background:#fffefa;overflow:hidden}.strategy-page .strategy-stats>div{border:0;border-radius:0;background:transparent;padding:10px 16px}.strategy-page .strategy-stats>div+div{border-left:1px solid #e6e7de}.strategy-page .strategy-stats strong{font-size:20px;line-height:1.2}.strategy-page .strategy-stats span{margin-top:3px}.strategy-page .workspace-tabs{margin-top:5px}.strategy-page .paper-panel{margin-top:12px}.strategy-page .member-table td{padding:8px 9px}.strategy-page .member-controls{margin:8px 0}.strategy-page .strategy-work .module-form{margin:8px 0}.strategy-page .strategy-work .module-actions{margin:5px 0}.strategy-page .strategy-work .module-form input,.strategy-page .strategy-work .module-form select{padding:6px 8px}.strategy-page .strategy-work>p{margin:6px 0}.strategy-page .strategy-work .panel-heading{margin-bottom:8px}@media(max-width:700px){.strategy-page .strategy-stats>div:nth-child(3){border-left:0}.strategy-page .strategy-stats>div:nth-child(n+3){border-top:1px solid #e6e7de}}
.strategy-heading-actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.strategy-heading-actions button{padding:10px 14px}.strategy-tabs{align-items:center;gap:18px}.strategy-more{position:relative;margin-left:auto;align-self:stretch}.strategy-more summary{list-style:none;cursor:pointer;color:#69736b;font-size:13px;padding:0 0 11px}.strategy-more summary::-webkit-details-marker{display:none}.strategy-more summary::after{content:' ▾';font-size:11px}.strategy-more>div{position:absolute;right:0;top:27px;z-index:5;min-width:190px;display:grid;gap:0;background:#fffefa;border:1px solid #dedfd5;border-radius:8px;box-shadow:0 12px 30px #263c3320;padding:6px}.strategy-more>div button{text-align:left;padding:9px 11px;border:0;border-radius:5px}.strategy-more>div button:hover,.strategy-more>div button.active{background:#f5f1e8;color:#263c33}
.ingest-workbench{scroll-margin-top:18px}.ingest-hero{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:23px 26px;background:linear-gradient(120deg,#edf2e9,#f7f4e9 68%,#f5ead8);border:1px solid #d6e0d1;border-radius:10px}.ingest-kicker{font-size:11px;font-weight:750;letter-spacing:.1em;color:#54755f}.ingest-hero h2{font-size:23px;margin:7px 0}.ingest-hero p{max-width:530px;margin:0;color:#617166;font-size:13px;line-height:1.6}.ingest-hero-actions{display:grid;justify-items:end;gap:9px;flex:none}.ingest-coverage-number{display:flex;align-items:baseline;gap:5px;color:#617166;font-size:12px}.ingest-coverage-number strong{font-size:28px;line-height:1;color:#263c33}.ingest-start-button{padding:12px 18px;font-size:13px;font-weight:700}.ingest-progress{height:7px;background:#e4eadf;border-radius:0 0 8px 8px;margin:0 8px 14px;overflow:hidden}.ingest-progress>div{height:100%;background:#5d886a;border-radius:8px;transition:width .3s}.ingest-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(270px,.8fr);gap:14px;align-items:start}.ingest-setup,.ingest-recent,.ingest-members{background:#fffefa}.ingest-step{display:flex;gap:12px;align-items:start;border-top:1px solid #e8e8df;padding-top:17px;margin:16px 0 11px}.ingest-step:first-child{margin-top:0;border-top:0;padding-top:0}.ingest-step>span{display:grid;place-items:center;width:28px;height:28px;border-radius:50%;background:#edf2e9;color:#3d6c50;font-size:11px;font-weight:800;flex:none}.ingest-step strong,.ingest-step small{display:block}.ingest-step strong{font-size:13px}.ingest-step small{font-size:11px;color:#738076;margin-top:4px}.ingest-universe-options,.ingest-range-options{display:flex;gap:8px;flex-wrap:wrap}.ingest-universe-options button,.ingest-range-options button{border:1px solid #d8ddd3;background:#fff;border-radius:7px;padding:9px 12px;text-align:left;color:#425248;font-size:12px}.ingest-universe-options button.active,.ingest-range-options button.active{border-color:#6c9b78;background:#eef5eb;color:#28543a}.ingest-universe-options button strong,.ingest-universe-options button small{display:block}.ingest-universe-options button small{font-size:10px;color:#738076;margin-top:3px}.ingest-date-fields,.ingest-batch-controls{display:flex;gap:11px;flex-wrap:wrap;margin-top:10px}.ingest-date-fields label,.ingest-batch-controls label,.ingest-advanced label{display:grid;gap:5px;font-size:11px;color:#6d786d}.ingest-date-fields input,.ingest-batch-controls input,.ingest-advanced select{border:1px solid #d8ddd3;background:white;border-radius:6px;padding:8px 10px;min-width:140px;color:#263c33}.ingest-batch-controls input{width:145px}.ingest-batch-preview{margin:9px 0 0;font-size:11px!important}.ingest-batch-preview strong{color:#263c33}.ingest-advanced{border-top:1px solid #e8e8df;margin-top:17px;padding-top:13px}.ingest-advanced summary{cursor:pointer;color:#5f7363;font-size:11px;font-weight:650}.ingest-advanced label{margin-top:12px}.ingest-advanced p{font-size:11px!important;margin:8px 0!important}.ingest-advanced button{font-size:11px;padding:7px 10px}.ingest-recent .panel-heading{margin-bottom:7px}.text-button{border:0;background:transparent;color:#47785a;font-size:11px;font-weight:650;cursor:pointer;padding:4px}.ingest-empty{font-size:12px;color:#79847a;line-height:1.6}.ingest-job{padding:12px 0;border-top:1px solid #e8e8df}.ingest-job>div,.ingest-history-main{display:flex;justify-content:space-between;align-items:center;gap:8px}.ingest-job strong,.ingest-history-main strong{font-size:12px;color:#2c4033}.ingest-job p,.ingest-history-row p{font-size:11px;color:#758076;margin:6px 0 0}.ingest-job-status{font-size:10px;border-radius:20px;padding:3px 7px;color:#547461;background:#edf5ea;white-space:nowrap}.ingest-job-status.failed,.ingest-job-status.partial{color:#a65e3f;background:#faeee6}.ingest-job-status.running{color:#477097;background:#eaf3fb}.ingest-job-error{display:block;color:#b45442!important;font-size:11px;margin-top:6px;line-height:1.5}.ingest-all-jobs{margin-top:12px}.ingest-members{margin-top:14px}.ingest-members .panel-heading>div{display:grid;gap:4px}.ingest-members .panel-heading span{font-size:11px}.ingest-member-add{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.ingest-member-add input{min-width:250px;flex:1;border:1px solid #dcded4;border-radius:6px;padding:8px 10px}.ingest-member-add button{padding:8px 12px}.ingest-history .panel-heading p{margin:4px 0 0}.ingest-history-row{padding:15px 0;border-top:1px solid #e8e8df}.ingest-job-actions{display:flex;gap:7px;margin-top:8px}.ingest-job-actions button{padding:5px 9px;border:1px solid #d8ddd3;border-radius:5px;background:white;font-size:11px}
@media(max-width:850px){.ingest-grid{grid-template-columns:1fr}.ingest-hero{align-items:start}.ingest-recent{order:2}}@media(max-width:620px){.strategy-page .module-heading,.ingest-hero{display:block}.strategy-heading-actions{margin-top:13px}.ingest-hero-actions{justify-items:start;margin-top:16px}.strategy-tabs{gap:13px}.ingest-universe-options button{flex:1}.ingest-date-fields label,.ingest-batch-controls label{flex:1}.ingest-date-fields input,.ingest-batch-controls input{width:100%;min-width:0}}
</style>
