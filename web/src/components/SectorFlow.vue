<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ universeId: string }>();
const loading = ref(false);
const detailLoading = ref(false);
const error = ref('');
const response = ref<Row>({});
const detail = ref<Row>({});
const selectedSector = ref('');
const items = computed<Row[]>(() => Array.isArray(response.value.items) ? response.value.items as Row[] : []);
const summary = computed<Row>(() => response.value.marketSummary && typeof response.value.marketSummary === 'object' ? response.value.marketSummary as Row : {});
const trends = computed<Row[]>(() => Array.isArray(detail.value.trend) ? (detail.value.trend as Row[]).slice(-20) : []);
const members = computed<Row[]>(() => Array.isArray(detail.value.topMembers) ? (detail.value.topMembers as Row[]).slice(0, 8) : []);
const maxTrend = computed(() => Math.max(1, ...trends.value.map(item => Math.abs(Number(item.proxyNetAmount) || 0))));
const number = (item: unknown): number | null => item == null || item === '' || !Number.isFinite(Number(item)) ? null : Number(item);
const fmt = (item: unknown, digits = 2) => number(item) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(number(item)!);
const money = (item: unknown) => number(item) === null ? '—' : Math.abs(number(item)!) >= 1e8 ? `${fmt(number(item)! / 1e8)} 亿` : `${fmt(number(item)! / 1e4)} 万`;
const pct = (item: unknown) => number(item) === null ? '—' : `${number(item)! > 0 ? '+' : ''}${fmt(item)}%`;
const signal = (item: unknown) => item === 'warming' ? '资金升温' : item === 'cooling' ? '资金转冷' : item === 'neutral' ? '观察' : '样本不足';

async function request(sector = '') {
  return unwrap(await apiRequest<ApiEnvelope<Row>>('/api/quant/strategies', {
    method: 'POST', body: JSON.stringify({ action: 'sector-capital-flow', universeId: props.universeId, limit: 50, sector: sector || undefined, detailDays: 20 }),
  })) || {};
}
async function load() {
  if (!props.universeId) return;
  loading.value = true; error.value = ''; selectedSector.value = ''; detail.value = {};
  try { response.value = await request(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取板块资金失败'; response.value = {}; }
  finally { loading.value = false; }
}
async function selectSector(sector: string) {
  if (selectedSector.value === sector) { selectedSector.value = ''; detail.value = {}; return; }
  selectedSector.value = sector; detailLoading.value = true; error.value = '';
  try { detail.value = (await request(sector)).detail as Row || {}; }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取板块明细失败'; detail.value = {}; }
  finally { detailLoading.value = false; }
}
watch(() => props.universeId, load, { immediate: true });
</script>

<template>
  <section class="sector-flow paper-panel">
    <div class="panel-heading"><div><strong>板块资金热度</strong><p>按股票池内的行情估算方向额，用于观察相对强弱。</p></div><button class="secondary-button" :disabled="loading" @click="load">刷新板块</button></div>
    <p v-if="error" class="sector-error" role="alert">{{ error }}</p>
    <p v-if="loading">正在汇总板块行情…</p>
    <template v-else>
      <div class="sector-summary"><div><strong>{{ fmt(summary.sectorCount, 0) }}</strong><span>板块</span></div><div><strong>{{ fmt(summary.coveredSymbolCount, 0) }}</strong><span>有行情的标的</span></div><div><strong>{{ fmt(summary.warmingCount, 0) }}</strong><span>资金升温</span></div><div><strong>{{ money(summary.proxyNetAmount) }}</strong><span>方向额代理</span></div></div>
      <p v-if="response.proxyNote" class="sector-note">{{ response.proxyNote }}</p>
      <div v-if="items.length" class="table-scroll"><table><thead><tr><th>板块</th><th>覆盖</th><th>上涨占比</th><th>方向额代理</th><th>20 日强弱</th><th>量能比</th><th>信号</th></tr></thead><tbody><tr v-for="item in items" :key="String(item.sector)" :class="{ active: selectedSector === item.sector }" tabindex="0" @click="selectSector(String(item.sector))" @keydown.enter="selectSector(String(item.sector))"><td><strong>{{ item.sector }}</strong></td><td>{{ fmt(item.coveredCount, 0) }} / {{ fmt(item.memberCount, 0) }}</td><td>{{ pct(item.risingRatio) }}</td><td :class="{ rise: (number(item.proxyNetAmount) || 0) > 0, fall: (number(item.proxyNetAmount) || 0) < 0 }">{{ money(item.proxyNetAmount) }}</td><td>{{ pct(item.strength20dPct) }}</td><td>{{ number(item.amountRatio20d) === null ? '—' : `${fmt(item.amountRatio20d)} 倍` }}</td><td>{{ signal(item.signal) }}</td></tr></tbody></table></div>
      <p v-else class="sector-empty">当前股票池还没有足够的本地行情来计算板块资金。补数完成后刷新即可查看。</p>
      <div v-if="selectedSector" class="sector-detail"><h3>{{ selectedSector }} · 近期热度</h3><p v-if="detailLoading">正在读取趋势与成分股…</p><template v-else><div v-if="trends.length" class="trend-bars"><div v-for="point in trends" :key="String(point.tradeDate)" class="trend-day" :title="`${point.tradeDate} · 方向额 ${money(point.proxyNetAmount)}`"><i :class="(number(point.proxyNetAmount) || 0) >= 0 ? 'rise-bar' : 'fall-bar'" :style="{ height: `${Math.max(5, Math.abs(Number(point.proxyNetAmount) || 0) / maxTrend * 100)}%` }"/><small>{{ String(point.tradeDate).slice(5) }}</small></div></div><h4>主要标的</h4><div v-if="members.length" class="member-tags"><span v-for="member in members" :key="String(member.symbol)">{{ member.name || member.symbol }} <b>{{ pct(member.latestChangePercent) }}</b></span></div><p v-else>暂无可展示的成分股行情。</p><p v-for="(line,index) in (detail.analysis as string[] || [])" :key="index">{{ line }}</p></template></div>
    </template>
  </section>
</template>

<style scoped>
.sector-flow{margin-top:14px}.sector-flow .panel-heading{align-items:flex-start}.sector-flow .panel-heading p{font-size:12px;color:#747d73;margin:4px 0 0}.sector-error{color:#a63e34}.sector-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid #e1e3d8;margin:12px 0}.sector-summary>div{padding:10px 14px}.sector-summary>div+div{border-left:1px solid #e1e3d8}.sector-summary strong,.sector-summary span{display:block}.sector-summary strong{font-size:17px}.sector-summary span{font-size:11px;color:#747d73}.sector-note,.sector-empty{font-size:12px;color:#69736b;line-height:1.6}.sector-flow table{width:100%;border-collapse:collapse;font-size:12px}.sector-flow th,.sector-flow td{padding:10px;text-align:left;border-bottom:1px solid #e1e3d8}.sector-flow tbody tr{cursor:pointer}.sector-flow tbody tr:hover,.sector-flow tbody tr.active{background:#f6f3e9}.rise{color:#a63e34}.fall{color:#16806a}.sector-detail{border-top:1px solid #e1e3d8;margin-top:18px;padding-top:12px}.sector-detail h3{margin:0 0 10px}.sector-detail h4{margin:12px 0 7px}.trend-bars{height:140px;display:flex;align-items:end;gap:5px;border-bottom:1px solid #e1e3d8}.trend-day{flex:1;min-width:12px;height:100%;display:flex;flex-direction:column;justify-content:end;align-items:stretch;gap:4px}.trend-day i{display:block;min-height:4px}.trend-day small{text-align:center;color:#747d73;font-size:9px}.rise-bar{background:#ce7772}.fall-bar{background:#78ad99}.member-tags{display:flex;gap:8px;flex-wrap:wrap}.member-tags span{border:1px solid #e1e3d8;padding:6px;font-size:11px}.member-tags b{margin-left:5px}@media(max-width:700px){.sector-summary{grid-template-columns:repeat(2,1fr)}.sector-summary>div:nth-child(3){border-left:0;border-top:1px solid #e1e3d8}.sector-summary>div:nth-child(4){border-top:1px solid #e1e3d8}.sector-flow table{min-width:760px}}
</style>
