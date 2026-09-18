<script setup lang="ts">
import { computed, ref } from 'vue';
type Row = Record<string, unknown>;
const props = defineProps<{ catalog: Row }>();
const search = ref('');
const selectedId = ref('');
const categories = computed<Row[]>(() => Array.isArray(props.catalog.categories) ? props.catalog.categories as Row[] : []);
const factors = computed<Row[]>(() => categories.value.flatMap(category => Array.isArray(category.factors) ? category.factors as Row[] : []));
const visible = computed(() => factors.value.filter(item => `${item.name || ''} ${item.category || ''} ${item.rationale || ''} ${item.formula || ''}`.toLowerCase().includes(search.value.trim().toLowerCase())));
const selected = computed<Row | null>(() => factors.value.find(item => item.id === selectedId.value) || visible.value[0] || null);
const list = (value: unknown): string[] => Array.isArray(value) ? value.map(String) : [];
const status = (value: unknown) => value === 'ready' ? '计算口径已具备' : value === 'partial' ? '部分口径待补' : '待补数据';
</script>

<template>
  <div class="factor-catalog">
    <div class="factor-intro"><div><strong>因子研究目录</strong><p>先看因子的用途、公式和数据缺口，再决定是否用于筛选或策略。</p></div><span>{{ factors.length }} 个因子 · {{ categories.length }} 个类别</span></div>
    <div class="factor-layout"><section class="paper-panel factor-list"><input v-model="search" aria-label="搜索因子" placeholder="搜索因子、公式或适用场景"/><button v-for="item in visible" :key="String(item.id)" :class="{ selected: selected?.id === item.id }" @click="selectedId = String(item.id)"><strong>{{ item.name }}</strong><small>{{ item.category }} · {{ status(item.status) }}</small></button><p v-if="!visible.length">没有匹配的因子。</p></section>
      <section v-if="selected" class="paper-panel factor-detail"><div class="panel-heading"><strong>{{ selected.name }}</strong><span>{{ status(selected.status) }}</span></div><p class="lead">{{ selected.rationale }}</p><dl><div><dt>观察周期</dt><dd>{{ selected.horizon || '—' }}</dd></div><div><dt>计算公式</dt><dd><code>{{ selected.formula || '暂无公式' }}</code></dd></div><div><dt>当前可用数据</dt><dd>{{ list(selected.currentData).join('、') || '暂无' }}</dd></div><div><dt>缺少的数据</dt><dd>{{ list(selected.missingData).join('、') || '无' }}</dd></div></dl><h3>适用场景</h3><ul><li v-for="item in list(selected.useCases)" :key="item">{{ item }}</li></ul><h3>使用边界</h3><ul><li v-for="item in list(selected.guardrails)" :key="item">{{ item }}</li></ul><h3>补齐数据的方向</h3><ul><li v-for="item in list(selected.enrichmentPlan)" :key="item">{{ item }}</li></ul></section></div>
  </div>
</template>

<style scoped>
.factor-catalog{margin-top:14px}.factor-intro{display:flex;justify-content:space-between;gap:15px;align-items:end}.factor-intro strong{font-size:17px}.factor-intro p,.factor-intro span{font-size:12px;color:#69736b}.factor-layout{display:grid;grid-template-columns:300px minmax(0,1fr);gap:13px;margin-top:10px}.factor-list{max-height:650px;overflow:auto}.factor-list input{width:100%;box-sizing:border-box;padding:9px;border:1px solid #dcded4;margin-bottom:10px}.factor-list button{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid #e1e3d8;background:transparent;padding:10px;cursor:pointer}.factor-list button.selected,.factor-list button:hover{background:#f5f1e8}.factor-list strong,.factor-list small{display:block}.factor-list small{color:#69736b;margin-top:4px}.factor-detail .lead{font-size:13px;line-height:1.7;color:#33483d}.factor-detail dl>div{display:grid;grid-template-columns:110px minmax(0,1fr);padding:9px 0;border-bottom:1px solid #e1e3d8;gap:10px;font-size:12px}.factor-detail dt{color:#69736b}.factor-detail dd{margin:0;overflow-wrap:anywhere}.factor-detail code{white-space:normal}.factor-detail h3{font-size:13px;margin:17px 0 5px}.factor-detail li{font-size:12px;line-height:1.6;margin:5px 0}@media(max-width:750px){.factor-layout{grid-template-columns:1fr}.factor-list{max-height:260px}}
</style>
