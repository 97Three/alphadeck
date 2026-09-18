<script setup lang="ts">
defineOptions({ name: 'DataList' });
const props = withDefaults(defineProps<{ value: unknown; depth?: number; limit?: number }>(), { depth: 0, limit: 30 });
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const entries = () => Object.entries(record(props.value)).slice(0, props.limit);
const states: Record<string, string> = { ready: '可用', planned: '规划中', blocked: '不可用', healthy: '正常', degraded: '已降级', core_analysis: '核心分析', quant_analysis: '量化分析', stock: '股票', etf: 'ETF', index: '指数', qfq: '前复权', hfq: '后复权', daily: '日线', weekly: '周线', monthly: '月线', UNKNOWN: '未知', CNY: '人民币' };
const scalar = (value: unknown) => value == null || value === '' ? '—' : typeof value === 'boolean' ? (value ? '是' : '否') : typeof value === 'string' ? states[value] || value : String(value);
const labels: Record<string, string> = {
  status: '状态', missing: '缺失项', sourceChanged: '源码已变化', packageChanged: '技能包已变化', versionMismatch: '版本不一致',
  id: '编号', name: '名称', title: '标题', description: '说明', version: '版本', summary: '摘要', detail: '详情',
  source: '来源', provider: '数据源', symbol: '股票代码', reason: '原因', message: '消息', createdAt: '创建时间', updatedAt: '更新时间',
  requestId: '请求编号', projectId: '项目编号', capabilityId: '研究能力', model: '模型', score: '评分', passed: '是否通过',
  total: '总数', warning: '警告', error: '错误', attempts: '尝试次数', durationMs: '耗时（毫秒）',
  shortName: '简称', inputHint: '提问示例', tags: '研究主题', groupId: '能力分组', agentType: '执行类型',
  executionCapabilityId: '执行能力', dataEndpoints: '数据接口', expectedArtifacts: '预期产物',
  blockedCapabilities: '不可用能力', readyCapabilities: '可用能力', plannedCapabilities: '规划中能力',
  dataProviders: '数据源', availableProviders: '可用数据源', degradedProviders: '降级数据源',
  marketApiReachable: '行情服务可连接', skillErrors: '技能异常',
  latestClose: '最新收盘价', latestChangePct: '最新涨跌幅', volume: '成交量', amount: '成交额',
  open: '开盘价', high: '最高价', low: '最低价', close: '收盘价', turnover: '换手率',
  trendStatus: '趋势状态', sector: '板块', sectorTags: '所属板块', tradeDate: '交易日',
  bars: 'K 线', timeframe: '周期', adjustment: '复权方式', fetchedAt: '获取时间', firstTs: '最早日期', lastTs: '最新日期', rowCount: '记录数', previousClose: '前收盘价', returnPct: '区间收益', totalVolume: '总成交量', totalAmount: '总成交额',
  changePercent: '涨跌幅', changeAmount: '涨跌额', marketCap: '总市值', floatMarketCap: '流通市值', peTtm: '市盈率', pbMrq: '市净率', quoteTime: '行情时间', asOf: '数据截至', dataQualityStatus: '数据质量', cacheStatus: '缓存状态',
  candidates: '候选标的', items: '记录', results: '结果', members: '标的', issues: '发现的问题', checkedSymbols: '检查标的', passedSymbols: '通过标的', warningSymbols: '警告标的', failedSymbols: '失败标的', issueCount: '问题数',
  code: '代码', assetType: '标的类型', currency: '计价货币', timezone: '时区', secid: '行情源代码', exchange: '交易所', dataProvider: '实际行情源', market: '市场', sourceStrategy: '采集方式',
};
const label = (key: string) => labels[key] || key.replace(/([a-z])([A-Z])/g, '$1 $2');
</script>

<template>
  <span v-if="value == null || typeof value !== 'object'">{{ scalar(value) }}</span>
  <div v-else-if="Array.isArray(value)" class="data-list-array"><p v-if="!value.length" class="data-list-empty">暂无记录</p><div v-for="(item, index) in value.slice(0, limit)" :key="index" class="data-list-item"><span class="data-list-index">{{ index + 1 }}</span><DataList :value="item" :depth="depth + 1" :limit="depth ? 8 : 20" /></div><p v-if="value.length > limit" class="data-list-more">还有 {{ value.length - limit }} 项，可缩小查询范围查看。</p></div>
  <dl v-else class="data-list-record"><div v-for="[key, item] in entries()" :key="key"><dt :title="key">{{ label(key) }}</dt><dd><DataList v-if="depth < 2 && item !== null && typeof item === 'object'" :value="item" :depth="depth + 1" :limit="8" /><span v-else>{{ item !== null && typeof item === 'object' ? (Array.isArray(item) ? `${item.length} 项` : `${Object.keys(item).length} 个字段`) : scalar(item) }}</span></dd></div></dl>
</template>

<style scoped>
.data-list-array{display:grid;gap:10px}.data-list-item{display:flex;gap:10px;padding:10px;border:1px solid #e1e3d8;border-radius:6px;background:#fffefa;min-width:0}.data-list-index{color:#ab7834;font-size:11px;flex:none}.data-list-record{margin:0;min-width:0}.data-list-record>div{display:grid;grid-template-columns:minmax(90px,145px) minmax(0,1fr);gap:10px;padding:7px 0;border-bottom:1px solid #e7e8e0;font-size:12px}.data-list-record dt{color:#6a756c;overflow-wrap:anywhere}.data-list-record dd{margin:0;overflow-wrap:anywhere;min-width:0}.data-list-empty,.data-list-more{color:#7b847b;font-size:12px}.data-list-record .data-list-record>div{grid-template-columns:minmax(70px,105px) minmax(0,1fr)}
</style>
