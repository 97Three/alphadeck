<script setup lang="ts">
import { computed, ref } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ foundation: Row; universeId: string }>();
const components = computed<Row[]>(() => Array.isArray(props.foundation.components) ? props.foundation.components as Row[] : []);
const calendar = computed<Row[]>(() => Array.isArray(props.foundation.calendarDays) ? (props.foundation.calendarDays as Row[]).slice(-15) : []);
const factors = computed<Row[]>(() => Array.isArray(props.foundation.factors) ? props.foundation.factors as Row[] : []);
const scan = ref<Row | null>(null);
const busy = ref(false);
const error = ref('');
const currentScan = computed<Row | null>(() => scan.value || (props.foundation.latestQualityScan && typeof props.foundation.latestQualityScan === 'object' ? props.foundation.latestQualityScan as Row : null));
const issues = computed<Row[]>(() => Array.isArray(currentScan.value?.issues) ? currentScan.value.issues as Row[] : []);
const status = (value: unknown) => value === 'ready' ? '可用' : value === 'partial' ? '部分可用' : '缺失';
const severity = (value: unknown) => value === 'error' ? '错误' : value === 'warning' ? '警告' : '正常';
const issueType = (value: unknown) => ({ missing_kline: '缺少 K 线', stale_kline: '行情未更新', missing_amount: '缺少成交额', missing_turnover: '缺少换手率', missing_trade_status: '缺少交易状态', missing_is_st: '缺少 ST 标记', missing_limit_fields: '缺少涨跌停字段' } as Record<string, string>)[String(value)] || String(value || '其他问题');
const number = (value: unknown) => Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN') : '—';

async function runScan() {
  busy.value = true; error.value = '';
  try {
    scan.value = unwrap(await apiRequest<ApiEnvelope<Row>>('/api/quant/strategies', {
      method: 'POST', body: JSON.stringify({ action: 'data-quality-scan', universeId: props.universeId, persist: false, lookbackYears: 5 }),
    })) || {};
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '数据质量检查失败'; }
  finally { busy.value = false; }
}
</script>

<template>
  <div class="foundation-page">
    <p v-if="foundation.error" class="foundation-error">{{ foundation.error }}</p>
    <section class="component-strip"><article v-for="item in components" :key="String(item.id)"><div><strong>{{ item.name }}</strong><b :class="String(item.status)">{{ status(item.status) }}</b></div><em>{{ number(item.count) }}</em><p>{{ item.detail || '暂无说明' }}</p></article></section>
    <div class="foundation-grid">
      <section class="paper-panel"><div class="panel-heading"><div><strong>数据质量扫描</strong><p>检查缺失 K 线、交易日和关键行情字段。</p></div><button class="secondary-button" :disabled="busy || !universeId" @click="runScan">{{ busy ? '检查中…' : '运行只读扫描' }}</button></div><p v-if="error" class="foundation-error" role="alert">{{ error }}</p><template v-if="currentScan"><div class="scan-stats"><div><strong>{{ number(currentScan.checkedSymbols) }}</strong><span>检查标的</span></div><div><strong>{{ number(currentScan.passedSymbols) }}</strong><span>通过</span></div><div><strong>{{ number(currentScan.warningSymbols) }}</strong><span>警告</span></div><div><strong>{{ number(currentScan.failedSymbols) }}</strong><span>失败</span></div></div><div v-if="issues.length" class="table-scroll"><table><thead><tr><th>标的</th><th>级别</th><th>问题</th><th>说明</th></tr></thead><tbody><tr v-for="(item,index) in issues.slice(0,20)" :key="index"><td>{{ item.name || item.symbol || '市场' }}</td><td>{{ severity(item.severity) }}</td><td>{{ issueType(item.issueType) }}</td><td>{{ item.message }}</td></tr></tbody></table></div><p v-else>本次扫描没有发现关键问题。</p></template><p v-else>尚未运行扫描。点击上方按钮可检查当前股票池。</p></section>
      <div class="foundation-side"><section class="paper-panel"><div class="panel-heading"><strong>交易日历</strong><span>最近 {{ calendar.length }} 日</span></div><div v-if="calendar.length" class="calendar-days"><div v-for="day in calendar" :key="String(day.tradeDate)" :class="{ open: day.isOpen }"><strong>{{ String(day.tradeDate).slice(5) }}</strong><small>{{ day.source === 'stock_bars-inferred' ? '推断' : day.isOpen ? '开市' : '休市' }}</small></div></div><p v-else>暂无交易日历，行情分析将按已入库 K 线推断。</p></section><section class="paper-panel"><div class="panel-heading"><strong>已登记的因子定义</strong><span>{{ factors.length }} 项</span></div><div class="factor-definitions"><article v-for="item in factors" :key="String(item.factorKey)"><strong>{{ item.name }}</strong><span>{{ item.description }}</span><code v-if="item.formula">{{ item.formula }}</code></article><p v-if="!factors.length">暂无因子定义。</p></div></section></div>
    </div>
  </div>
</template>

<style scoped>
.foundation-page{margin-top:14px}.foundation-error{color:#a63e34}.component-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.component-strip article{background:#fffefa;border:1px solid #e1e3d8;padding:13px}.component-strip article>div{display:flex;justify-content:space-between;gap:8px}.component-strip strong,.component-strip b{font-size:12px}.component-strip b{color:#7a8078}.component-strip b.ready{color:#16806a}.component-strip b.missing{color:#a63e34}.component-strip em{display:block;font-style:normal;font-size:22px;font-weight:700;margin:7px 0}.component-strip p,.paper-panel p{font-size:12px;color:#69736b;line-height:1.6}.foundation-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:13px;margin-top:13px}.foundation-side{display:grid;gap:13px;align-content:start}.scan-stats{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid #e1e3d8;margin:14px 0}.scan-stats div{padding:10px}.scan-stats div+div{border-left:1px solid #e1e3d8}.scan-stats strong,.scan-stats span{display:block}.scan-stats span{font-size:11px;color:#69736b}.paper-panel table{width:100%;border-collapse:collapse;font-size:11px;min-width:550px}.paper-panel th,.paper-panel td{text-align:left;padding:8px;border-bottom:1px solid #e1e3d8}.calendar-days{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}.calendar-days div{background:#f4f4ef;padding:7px;text-align:center}.calendar-days div.open{background:#e9f3ec;color:#16806a}.calendar-days strong,.calendar-days small{display:block}.calendar-days small{font-size:10px}.factor-definitions{max-height:460px;overflow:auto}.factor-definitions article{padding:9px 0;border-bottom:1px solid #e1e3d8}.factor-definitions strong,.factor-definitions span,.factor-definitions code{display:block;font-size:11px}.factor-definitions span{color:#69736b;margin-top:3px}.factor-definitions code{white-space:normal;margin-top:5px}@media(max-width:900px){.foundation-grid{grid-template-columns:1fr}}@media(max-width:650px){.component-strip{grid-template-columns:repeat(2,1fr)}}
</style>
