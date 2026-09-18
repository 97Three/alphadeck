<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ member: Row }>();
const symbol = computed(() => String(props.member.symbol || props.member.code || ''));
const name = computed(() => String(props.member.name || symbol.value));
const timeframe = ref('daily');
const loading = ref(false);
const error = ref('');
const detail = ref<Row>({});
const selectedIndex = ref<number | null>(null);
const bars = computed<Row[]>(() => Array.isArray(detail.value.bars) ? detail.value.bars as Row[] : []);
const visible = computed(() => bars.value.slice(-120));
const currentIndex = computed(() => Math.max(0, Math.min(selectedIndex.value ?? visible.value.length - 1, visible.value.length - 1)));
const selected = computed<Row>(() => visible.value[currentIndex.value] || {});
const num = (value: unknown): number | null => value == null || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
const fmt = (value: unknown, digits = 2): string => num(value) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(num(value)!);
const money = (value: unknown): string => {
  const amount = num(value);
  return amount === null ? '—' : Math.abs(amount) >= 1e8 ? `${fmt(amount / 1e8)} 亿` : Math.abs(amount) >= 1e4 ? `${fmt(amount / 1e4)} 万` : fmt(amount);
};
const percent = (value: unknown): string => num(value) === null ? '—' : `${num(value)! > 0 ? '+' : ''}${fmt(value)}%`;
const scale = computed(() => {
  const values = visible.value.flatMap(bar => [num(bar.high), num(bar.low)]).filter((item): item is number => item !== null);
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 1;
  return { min, range: Math.max(max - min, 0.01) };
});
const y = (value: unknown) => 210 - ((num(value) ?? scale.value.min) - scale.value.min) / scale.value.range * 180;
const candles = computed(() => visible.value.map((bar, index) => ({
  x: 38 + (index + .5) / visible.value.length * 824,
  open: y(bar.open), close: y(bar.close), high: y(bar.high), low: y(bar.low),
  width: Math.max(2, Math.min(9, 824 / visible.value.length * .65)),
  up: (num(bar.close) ?? 0) >= (num(bar.open) ?? 0),
})));
const volumeMax = computed(() => Math.max(1, ...visible.value.map(bar => num(bar.volume) || 0)));

async function load() {
  if (!symbol.value) return;
  loading.value = true; error.value = ''; selectedIndex.value = null;
  try {
    detail.value = unwrap(await apiRequest<ApiEnvelope<Row>>('/api/quant/strategies', {
      method: 'POST', body: JSON.stringify({ action: 'symbol-bars', symbol: symbol.value, timeframe: timeframe.value, adjustment: 'qfq', limit: 240 }),
    })) || {};
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取本地 K 线失败'; detail.value = {}; }
  finally { loading.value = false; }
}
function selectPointer(event: PointerEvent) {
  if (!visible.value.length) return;
  const bounds = (event.currentTarget as SVGSVGElement).getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width * 900;
  selectedIndex.value = Math.max(0, Math.min(visible.value.length - 1, Math.round((x - 38) / 824 * visible.value.length - .5)));
}
function step(delta: number) { selectedIndex.value = Math.max(0, Math.min(visible.value.length - 1, currentIndex.value + delta)); }
watch([symbol, timeframe], load, { immediate: true });
</script>

<template>
  <div class="member-detail">
    <div class="detail-top"><div><strong>{{ name }} · {{ symbol }}</strong><small>本地 K 线 · 前复权 · {{ bars.length }} 根</small></div><label>周期 <select v-model="timeframe"><option value="daily">日线</option><option value="weekly">周线</option><option value="monthly">月线</option></select></label><button type="button" @click="load">刷新</button></div>
    <p v-if="loading">正在读取本地行情…</p><p v-else-if="error" class="detail-error">{{ error }}</p><p v-else-if="!bars.length">这只标的尚无本地 K 线。可先在上方补数，完成后刷新这里查看。</p>
    <template v-else>
      <div class="bar-facts"><strong>{{ String(selected.ts || '').slice(0, 10) }}</strong><span>开 {{ fmt(selected.open) }}</span><span>高 {{ fmt(selected.high) }}</span><span>低 {{ fmt(selected.low) }}</span><span>收 {{ fmt(selected.close) }}</span><span>涨跌 {{ percent(selected.changePercent) }}</span><span>成交量 {{ fmt(selected.volume, 0) }}</span><span>成交额 {{ money(selected.amount) }}</span><span>换手 {{ percent(selected.turnover) }}</span></div>
      <svg viewBox="0 0 900 280" role="img" :aria-label="`${name} K 线图`" tabindex="0" preserveAspectRatio="none" @pointermove="selectPointer" @click="selectPointer" @pointerleave="selectedIndex = null" @keydown.left.prevent="step(-1)" @keydown.right.prevent="step(1)">
        <line x1="38" y1="210" x2="862" y2="210" class="axis"/><line x1="38" y1="270" x2="862" y2="270" class="axis"/>
        <line v-if="candles.length" :x1="candles[currentIndex].x" :x2="candles[currentIndex].x" y1="20" y2="270" class="crosshair"/>
        <g v-for="(candle, index) in candles" :key="index" :class="candle.up ? 'rise' : 'fall'"><line :x1="candle.x" :x2="candle.x" :y1="candle.high" :y2="candle.low"/><rect :x="candle.x-candle.width/2" :y="Math.min(candle.open,candle.close)" :width="candle.width" :height="Math.max(2,Math.abs(candle.open-candle.close))"/></g>
        <rect v-for="(bar,index) in visible" :key="`v-${index}`" :x="candles[index].x-candles[index].width/2" :y="268-(num(bar.volume) || 0)/volumeMax*45" :width="candles[index].width" :height="(num(bar.volume) || 0)/volumeMax*45" :class="candles[index].up ? 'volume-rise' : 'volume-fall'"/>
      </svg>
      <div class="range-label"><span>{{ String(visible[0]?.ts || '').slice(0,10) }}</span><span>移动鼠标或点击 K 线查看当天明细；键盘左右键也可切换</span><span>{{ String(visible.at(-1)?.ts || '').slice(0,10) }}</span></div>
    </template>
  </div>
</template>

<style scoped>
.member-detail{padding:18px 20px 22px;background:#faf9f4;border-top:1px solid #dcded4}.detail-top{display:flex;align-items:center;gap:14px;justify-content:space-between;flex-wrap:wrap}.detail-top strong,.detail-top small{display:block}.detail-top small{color:#747d73;margin-top:4px}.detail-top label{display:flex;align-items:center;gap:8px;font-size:12px}.detail-top select,.detail-top button{border:1px solid #dcded4;background:white;border-radius:5px;padding:6px 9px}.bar-facts{display:flex;flex-wrap:wrap;gap:9px 15px;margin:15px 0;padding:10px 0;border-block:1px solid #dcded4;font-size:12px;font-variant-numeric:tabular-nums}.bar-facts strong{color:#243b30}.member-detail svg{display:block;width:100%;height:300px;cursor:crosshair;touch-action:pan-y}.member-detail svg:focus-visible{outline:2px solid #a56e27}.axis{stroke:#d6d9d0}.crosshair{stroke:#8a744e;stroke-dasharray:4 4;pointer-events:none}.rise line,.rise rect{stroke:#b44a43;fill:#b44a43}.fall line,.fall rect{stroke:#17836b;fill:#17836b}.volume-rise{fill:#e9a8a9}.volume-fall{fill:#97d1bb}.range-label{display:flex;justify-content:space-between;gap:10px;color:#747d73;font-size:11px}.detail-error{color:#a63e34}@media(max-width:700px){.member-detail svg{height:230px}.range-label span:nth-child(2){display:none}}
</style>
