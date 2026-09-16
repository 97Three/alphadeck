<script setup lang="ts">
import { computed, ref } from 'vue';
import dashboardData from 'virtual:dashboard-data';

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const list = (value: unknown): RecordValue[] => Array.isArray(value) ? value.map(record) : [];
const number = (value: unknown): number | null => { const result = Number(value); return value == null || value === '' || !Number.isFinite(result) ? null : result; };
const text = (value: unknown, fallback = '—'): string => typeof value === 'string' && value.trim() ? value : fallback;
const format = (value: unknown, digits = 2): string => number(value) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(number(value)!);
const source = record(dashboardData);
const assets = computed(() => list(source.assets));
const activeIndex = ref(0);
const active = computed(() => assets.value[activeIndex.value] || source);
const selected = computed(() => record(active.value));
const kline = computed(() => record(selected.value.kline || selected.value.history));
const bars = computed(() => {
  for (const value of [kline.value.bars, kline.value.data, selected.value.bars, selected.value.klines]) {
    if (Array.isArray(value) && value.length) return list(value);
  }
  return [];
});
const closes = computed(() => bars.value.map(bar => number(bar.close)).filter((value): value is number => value !== null));
const chartBars = computed(() => bars.value.slice(-80));
const chartScale = computed(() => {
  const values = chartBars.value.flatMap(bar => [bar.low, bar.high, bar.close].map(number).filter((item): item is number => item !== null));
  return { low: values.length ? Math.min(...values) : 0, high: values.length ? Math.max(...values) : 1 };
});
const chartY = (value: number): number => 175 - (value - chartScale.value.low) / (chartScale.value.high - chartScale.value.low || 1) * 150;
const candles = computed(() => chartBars.value.flatMap((bar, index) => {
  const open = number(bar.open), high = number(bar.high), low = number(bar.low), close = number(bar.close);
  if (open === null || high === null || low === null || close === null) return [];
  const x = 20 + (index + .5) / chartBars.value.length * 600;
  return [{ x, top: chartY(Math.max(open, close)), bottom: chartY(Math.min(open, close)), high: chartY(high), low: chartY(low), width: Math.max(3, Math.min(12, 600 / chartBars.value.length * .65)), up: close >= open }];
}));
const chartPoints = computed(() => {
  const values = closes.value.slice(-80);
  if (values.length < 2) return '';
  return values.map((value, index) => `${(20 + (index + .5) / values.length * 600).toFixed(1)},${chartY(value).toFixed(1)}`).join(' ');
});
const ma60Points = computed(() => {
  if (closes.value.length < 60) return '';
  const first = closes.value.length - chartBars.value.length;
  return chartBars.value.map((_, index) => {
    const position = first + index;
    if (position < 59) return null;
    const value = closes.value.slice(position - 59, position + 1).reduce((a, b) => a + b, 0) / 60;
    return `${(20 + (index + .5) / chartBars.value.length * 600).toFixed(1)},${chartY(value).toFixed(1)}`;
  }).filter(Boolean).join(' ');
});
const volumeBars = computed(() => {
  const samples = bars.value.slice(-80);
  const max = Math.max(1, ...samples.map(bar => number(bar.volume) || 0));
  return samples.map((bar, index) => ({ x: index / samples.length * 640, height: (number(bar.volume) || 0) / max * 70, width: Math.max(2, 640 / samples.length - 2) }));
});
const summary = computed(() => record(selected.value.summary));
const quote = computed(() => record(selected.value.quote || selected.value.realtime || source.quote));
const indicators = computed(() => record(selected.value.technicalIndicators || selected.value.indicators));
const backtest = computed(() => record(selected.value.backtest));
const financials = computed(() => record(selected.value.financials || source.financials));
const reports = computed(() => list(financials.value.reports));
const announcements = computed(() => list(Array.isArray(selected.value.announcements) ? selected.value.announcements : record(selected.value.announcements || source.announcements).announcements));
const dataQuality = computed(() => record(selected.value.dataQuality || source.dataQuality || source.quality));
const comparison = computed(() => list(record(source.comparison).rows));
const selectionRanking = computed(() => list(record(source.selectionRanking).rows));
const financialQuality = computed(() => list(record(source.financialQuality).rows));
const fundamentalPoints = computed(() => list(record(source.fundamentalIndicators).points));
const revenueBars = computed(() => {
  const rows = (fundamentalPoints.value.length ? fundamentalPoints.value : reports.value).slice(-12);
  const max = Math.max(1, ...rows.map(item => number(item.revenue || item.operating_revenue) || 0));
  return rows.map((item, index) => ({ date: text(item.report_date || item.date), revenue: number(item.revenue || item.operating_revenue) || 0, x: index / rows.length * 640, width: Math.max(4, 640 / rows.length - 8), height: (number(item.revenue || item.operating_revenue) || 0) / max * 145 }));
});
const fundamentalComparison = computed(() => record(source.fundamentalMetricComparison));
const metrics = computed(() => record(selected.value.computedMetrics || source.computedMetrics));
const holdings = computed(() => list(source.holdings));
const portfolio = computed(() => record(source.portfolio));
const concentration = computed(() => record(portfolio.value.concentration));
const correlation = computed(() => record(source.correlation));
const liquidity = computed(() => list(record(source.liquidity).rows));
const equity = computed(() => list(backtest.value.equity_curve));
const trades = computed(() => list(backtest.value.trades));
const screener = computed(() => record(source.screener));
const conclusion = computed(() => record(source.conclusion));
const warnings = computed(() => Array.isArray(source.warnings) ? source.warnings : []);
const hasContent = computed(() => bars.value.length || assets.value.length || Object.keys(quote.value).length || reports.value.length || fundamentalPoints.value.length || equity.value.length || holdings.value.length || comparison.value.length || selectionRanking.value.length);
const equityPoints = computed(() => {
  const values = equity.value.map(item => number(item.equity || item.value || item.nav)).filter((item): item is number => item !== null);
  if (values.length < 2) return '';
  const min = Math.min(...values), max = Math.max(...values);
  return values.map((value, index) => `${20 + index / (values.length - 1) * 600},${175 - (value - min) / (max - min || 1) * 150}`).join(' ');
});
const name = computed(() => text(selected.value.name || selected.value.symbol || source.title, '研究看板'));
const symbol = computed(() => text(selected.value.symbol || selected.value.code, ''));
const template = computed(() => text(record(source.visualization).templateId || record(source.visualization).template_id, '量化研究'));
const latest = computed(() => bars.value.at(-1) || {});
const start = computed(() => bars.value[0] || {});
const change = computed(() => { const first = number(start.value.close), last = number(latest.value.close); return first && last ? (last / first - 1) * 100 : null; });
const realTime = ref<RecordValue>({});
const realTimeError = ref('');
async function refreshQuote() {
  if (!symbol.value) return;
  realTimeError.value = '';
  try {
    const response = await fetch(`/api/market/quotes/realtime/${encodeURIComponent(symbol.value)}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`行情接口返回 HTTP ${response.status}`);
    realTime.value = record(await response.json());
  } catch (error) { realTimeError.value = error instanceof Error ? error.message : '刷新失败'; }
}
</script>

<template>
  <div class="dashboard-shell" data-source-file="data_file/final/dashboard-data.json" :data-template="template">
    <header class="dashboard-header"><div class="brand">Quant<span>Pilot</span><small>研究看板</small></div><span class="source-badge">真实数据 · {{ template }}</span></header>
    <main class="dashboard-main">
      <div class="eyebrow">RESEARCH DASHBOARD</div><h1>{{ name }}</h1>
      <p class="subtitle">{{ text(source.question || source.title || source.description, '研究数据与图表由当前任务生成。') }}</p>
      <nav v-if="assets.length > 1" class="asset-tabs" aria-label="选择研究标的"><button v-for="(asset, index) in assets" :key="index" type="button" :aria-current="activeIndex === index ? 'true' : undefined" @click="activeIndex = index">{{ text(asset.name || asset.symbol, `标的 ${index + 1}`) }}</button></nav>
      <div v-if="!hasContent" class="empty-state">等待真实研究数据。完成数据准备后，这里会展示图表与研究证据。</div>
      <template v-else>
        <section v-if="bars.length || Object.keys(quote).length" class="metric-row" aria-label="关键指标"><div><span>最新收盘或报价</span><strong>{{ format(latest.close || quote.price) }}</strong><small>{{ text(latest.date || latest.ts) }}</small></div><div><span>区间变化</span><strong :class="{ positive: (change || 0) > 0, negative: (change || 0) < 0 }">{{ change === null ? '—' : `${change > 0 ? '+' : ''}${change.toFixed(2)}%` }}</strong><small>{{ bars.length }} 条 K 线</small></div><div><span>成交量</span><strong>{{ format(latest.volume, 0) }}</strong><small>{{ text(kline.timeframe || source.timeframe, '日线') }}</small></div></section>
        <section v-if="bars.length" class="chart-panel"><div class="section-head"><div><span class="eyebrow">PRICE & TREND</span><h2>K 线与量价结构</h2></div><span>{{ symbol }}</span></div><svg v-if="candles.length || chartPoints" viewBox="0 0 640 195" role="img" :aria-label="`${name} K 线与收盘价趋势，${bars.length} 个数据点`" preserveAspectRatio="none"><title>K 线、收盘价和 MA60</title><line x1="0" y1="170" x2="640" y2="170" class="axis" /><g v-for="(bar, index) in candles" :key="index" :class="bar.up ? 'candle-up' : 'candle-down'"><line :x1="bar.x" :x2="bar.x" :y1="bar.high" :y2="bar.low" /><rect :x="bar.x - bar.width / 2" :y="bar.top" :width="bar.width" :height="Math.max(2, bar.bottom - bar.top)" /></g><polyline v-if="!candles.length" :points="chartPoints" class="trend" /><polyline v-if="ma60Points" :points="ma60Points" class="ma60" /></svg><div v-else class="chart-empty">数据点不足，暂不能绘制趋势图。</div><div class="chart-axis"><span>{{ text(start.date || start.ts) }}</span><span>日 K <i class="legend-close"></i> MA60 <i class="legend-ma60"></i></span><span>{{ text(latest.date || latest.ts) }}</span></div><div v-if="volumeBars.length" class="volume-chart"><h3>成交量副图</h3><svg viewBox="0 0 640 80" role="img" aria-label="成交量副图" preserveAspectRatio="none"><title>成交量</title><rect v-for="(bar, index) in volumeBars" :key="index" :x="bar.x" :y="75 - bar.height" :width="bar.width" :height="bar.height" class="volume-up" /></svg></div></section>
        <section v-if="comparison.length || selectionRanking.length" class="table-panel"><div class="section-head"><div><span class="eyebrow">COMPARISON</span><h2>多标的指标矩阵 · 相对强弱与排名依据</h2></div></div><div class="table-scroll"><table><thead><tr><th>标的</th><th>排名</th><th>得分</th><th>区间收益</th><th>最大回撤</th><th>波动率</th></tr></thead><tbody><tr v-for="row in (comparison.length ? comparison : selectionRanking)" :key="String(row.symbol)"><td>{{ text(row.symbol) }}</td><td>{{ format(selectionRanking.find(item => item.symbol === row.symbol)?.rank, 0) }}</td><td>{{ format(selectionRanking.find(item => item.symbol === row.symbol)?.score) }}</td><td>{{ format(row.period_return) }}</td><td>{{ format(row.max_drawdown) }}</td><td>{{ format(row.volatility20d) }}</td></tr></tbody></table></div></section>
        <section v-if="Object.keys(screener).length || selectionRanking.length || financialQuality.length" class="table-panel"><div class="section-head"><div><span class="eyebrow">RESEARCH FILTER</span><h2>候选筛选与财务质量</h2></div></div><p v-if="screener.total_candidates === 0" class="empty-note">暂无候选：没有满足安全条件的候选。请调整筛选范围，或检查数据缺口。</p><div class="metric-row compact"><div><span>扫描标的</span><strong>{{ format(screener.scanned_symbols, 0) }}</strong></div><div><span>候选数量</span><strong>{{ format(screener.total_candidates, 0) }}</strong></div><div><span>筛选日期</span><strong>{{ text(screener.trade_date) }}</strong></div></div><div v-if="financialQuality.length" class="table-scroll"><table><thead><tr><th>标的</th><th>财务质量证据</th></tr></thead><tbody><tr v-for="(row, index) in financialQuality" :key="index"><td>{{ text(row.symbol) }}</td><td>{{ text(row.summary || row.conclusion || row.grade || row.status) }}</td></tr></tbody></table></div></section>
        <section v-if="holdings.length" class="table-panel"><div class="section-head"><div><span class="eyebrow">PORTFOLIO</span><h2>持仓矩阵与集中度</h2></div></div><div class="table-scroll"><table><thead><tr><th>标的</th><th>权重</th><th>现价</th><th>市值</th></tr></thead><tbody><tr v-for="(holding, index) in holdings" :key="index"><td>{{ text(holding.name || holding.symbol) }}</td><td>{{ format(holding.weight, 2) }}</td><td>{{ format(holding.current_price) }}</td><td>{{ format(holding.market_value) }}</td></tr></tbody></table></div><div class="detail-grid"><article><h2>组合风险与调仓线索</h2><dl><div v-for="(value, key) in concentration" :key="key"><dt>{{ key }}</dt><dd>{{ format(value) }}</dd></div></dl><p>相关性：{{ list(correlation.top_pairs).length }} 组；流动性：{{ liquidity.length }} 个标的。请结合回撤与数据缺口判断。</p></article><article><h2>数据缺口与风险</h2><p v-for="(item, index) in list(portfolio.data_gaps)" :key="index">{{ JSON.stringify(item) }}</p><p v-for="(item, index) in (Array.isArray(portfolio.warnings) ? portfolio.warnings : [])" :key="index">{{ String(item) }}</p></article></div></section>
        <section v-if="equity.length" class="chart-panel section-gap"><div class="section-head"><div><span class="eyebrow">BACKTEST</span><h2>回测结果与组合净值</h2></div><span>{{ equity.length }} 个样本点</span></div><svg v-if="equityPoints" viewBox="0 0 640 195" role="img" aria-label="回测净值曲线" preserveAspectRatio="none"><title>回测净值曲线</title><line x1="0" y1="170" x2="640" y2="170" class="axis" /><polyline :points="equityPoints" class="trend" /></svg><div class="detail-grid"><article><h2>回测摘要</h2><dl><div v-for="(value, key) in record(backtest.summary)" :key="key"><dt>{{ key }}</dt><dd>{{ typeof value === 'number' ? format(value) : String(value) }}</dd></div></dl></article><article><h2>交易明细</h2><p>{{ trades.length }} 笔交易。回测参数和样本限制应与结论一起阅读。</p><div class="table-scroll"><table v-if="trades.length"><thead><tr><th>日期</th><th>方向</th><th>价格</th></tr></thead><tbody><tr v-for="(trade, index) in trades.slice(0, 20)" :key="index"><td>{{ text(trade.date || trade.trade_date) }}</td><td>{{ text(trade.side || trade.action) }}</td><td>{{ format(trade.price) }}</td></tr></tbody></table></div></article></div></section>
        <section v-if="reports.length || fundamentalPoints.length" class="table-panel"><div class="section-head"><div><span class="eyebrow">FUNDAMENTALS</span><h2>财务报告与基本面指标</h2></div><span>{{ reports.length }} 份报告</span></div><div v-if="revenueBars.length" class="revenue-chart"><svg viewBox="0 0 640 170" role="img" aria-label="历年营收柱状图" preserveAspectRatio="none"><title>历年营收</title><line x1="0" y1="165" x2="640" y2="165" class="axis" /><rect v-for="(bar, index) in revenueBars" :key="index" :x="bar.x + 3" :y="160 - bar.height" :width="bar.width" :height="bar.height" class="volume-up"><title>{{ bar.date }}：{{ format(bar.revenue) }}</title></rect></svg></div><div v-if="reports.length" class="table-scroll"><table><thead><tr><th>报告期</th><th>营收</th><th>净利润</th><th>ROE</th><th>现金流</th></tr></thead><tbody><tr v-for="(report, index) in reports.slice(0, 12)" :key="index"><td>{{ text(report.report_date || report.date || report.period) }}</td><td>{{ format(report.revenue || report.operating_revenue) }}</td><td>{{ format(report.parent_net_profit || report.net_profit || report.netProfit) }}</td><td>{{ format(report.roe) }}</td><td>{{ format(report.operating_cash_flow || report.cash_flow) }}</td></tr></tbody></table></div><div v-if="fundamentalPoints.length" class="detail-grid"><article><h2>指标变化</h2><dl><div v-for="(point, index) in fundamentalPoints.slice(-8)" :key="index"><dt>{{ text(point.date || point.report_date || point.metric) }}</dt><dd>营收 {{ format(point.revenue) }} · 净利润 {{ format(point.parent_net_profit || point.net_profit) }}</dd></div></dl></article><article><h2>比较结论</h2><p>{{ text(fundamentalComparison.conclusion) }}</p><p>{{ text(fundamentalComparison.basis) }}</p></article></div></section>
        <section v-if="Object.keys(metrics).length || Object.keys(conclusion).length || warnings.length" class="detail-grid section-gap"><article><h2>量化信号摘要</h2><dl><div v-for="(value, key) in metrics" :key="key"><dt>{{ key }}</dt><dd>{{ typeof value === 'number' ? format(value) : String(value) }}</dd></div></dl><p v-if="conclusion.summary">{{ conclusion.summary }}</p></article><article><h2>研究限制与失效风险</h2><p v-for="(item, index) in warnings" :key="index">{{ String(item) }}</p><p v-if="conclusion.risk_disclaimer">{{ conclusion.risk_disclaimer }}</p><p v-if="!warnings.length && !conclusion.risk_disclaimer">请结合样本区间、参数和数据口径判断结果。</p></article></section>
        <section v-if="Object.keys(summary).length || Object.keys(indicators).length" class="detail-grid"><article><div class="section-head"><h2>指标与结论</h2></div><dl><div v-for="(value, key) in { ...summary, ...record(indicators.summary) }" :key="key"><dt>{{ key }}</dt><dd>{{ typeof value === 'object' ? JSON.stringify(value) : String(value) }}</dd></div></dl></article><article><div class="section-head"><h2>信号解释</h2></div><dl><div v-for="(value, key) in indicators" :key="key"><dt>{{ key }}</dt><dd>{{ typeof value === 'object' ? JSON.stringify(value) : String(value) }}</dd></div></dl></article></section>
        <section v-if="announcements.length || Object.keys(dataQuality).length" class="detail-grid"><article><div class="section-head"><h2>财务公告</h2></div><dl><div v-for="(item, index) in announcements.slice(0, 8)" :key="index"><dt>公告 {{ index + 1 }}</dt><dd>{{ text(item.title || item.name) }}</dd></div></dl><p v-if="!announcements.length">本次任务暂无公告。</p></article><article><div class="section-head"><h2>数据质量与风险结论</h2></div><dl><div v-for="(value, key) in dataQuality" :key="key"><dt>{{ key }}</dt><dd>{{ typeof value === 'object' ? JSON.stringify(value) : String(value) }}</dd></div></dl><p v-if="!Object.keys(dataQuality).length">请核对采样时点、缺失值与复权口径，再使用研究结论。</p></article></section>
        <section class="realtime"><div><h2>实时行情</h2><p>按需向本地行情服务查询，不将实时值伪装成历史收盘。</p></div><button type="button" @click="refreshQuote">刷新实时行情</button><span v-if="Object.keys(realTime).length">{{ JSON.stringify(realTime) }}</span><span v-if="realTimeError" class="error">{{ realTimeError }}</span></section>
      </template>
    </main>
  </div>
</template>
