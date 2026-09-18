<script setup lang="ts">
import { computed, ref } from 'vue';
import wiki from '../data/financial-knowledge.json';

const query = ref('');
const collectionId = ref('all');
const type = ref('all');
const selectedId = ref(wiki.pages[0]?.id || '');
const showProtocol = ref(false);
const types: Record<string, string> = { concept: '概念', indicator: '指标', workflow: '流程', risk: '风控', source: '数据源' };
const activeCollection = computed(() => wiki.collections.find(item => item.id === collectionId.value));
const visible = computed(() => wiki.pages.filter(page => {
  const inCollection = !activeCollection.value || activeCollection.value.pages.includes(page.id);
  const inType = type.value === 'all' || page.type === type.value;
  const content = [page.title, page.summary, page.domain, page.decisionUse, page.formula, ...page.sources, ...page.caveats].join(' ').toLowerCase();
  return inCollection && inType && content.includes(query.value.trim().toLowerCase());
}));
const selected = computed(() => wiki.pages.find(item => item.id === selectedId.value && visible.value.includes(item)) || visible.value[0]);
const related = computed(() => selected.value?.links.map(link => wiki.pages.find(page => `[[${page.title}]]` === link)).filter((page): page is typeof wiki.pages[number] => !!page) || []);
function openEntry(pageId: string) { collectionId.value = 'all'; type.value = 'all'; query.value = ''; selectedId.value = pageId; }
</script>

<template>
  <div class="wiki-page"><div class="wiki-heading"><div><strong>金融知识库</strong><p>理解数据口径、指标用途与风险边界；内容沿用原版的 11 篇知识页面。</p></div><span>{{ wiki.pages.length }} 篇 · {{ wiki.collections.length }} 个主题 <button class="protocol-button" @click="showProtocol = !showProtocol">{{ showProtocol ? '收起维护协议' : '查看维护协议' }}</button></span></div>
    <div class="wiki-entry"><button v-for="entry in wiki.entryPoints" :key="entry.id" @click="openEntry(entry.pageIds[0])"><strong>{{ entry.title }}</strong><small>{{ entry.description }}</small></button></div>
    <section v-if="showProtocol" class="wiki-protocol paper-panel"><h3>知识库如何保持可靠</h3><p>{{ wiki.purpose.statement }}</p><div class="wiki-operations"><article v-for="operation in wiki.operations" :key="operation.id"><strong>{{ operation.title }}</strong><p>{{ operation.description }}</p><ul><li v-for="check in operation.checks" :key="check">{{ check }}</li></ul></article></div><h4>页面质量规则</h4><ul><li v-for="rule in wiki.schemaRules" :key="rule">{{ rule }}</li></ul><details><summary>更新记录</summary><p v-for="item in wiki.log" :key="item.date">{{ item.date }} · {{ item.event }}</p></details></section>
    <div class="wiki-layout"><aside class="paper-panel wiki-index"><input v-model="query" aria-label="搜索金融知识" placeholder="搜索概念、公式或数据源"/><div class="wiki-filters"><select v-model="collectionId" aria-label="知识主题"><option value="all">全部主题</option><option v-for="item in wiki.collections" :key="item.id" :value="item.id">{{ item.name }}</option></select><select v-model="type" aria-label="知识类型"><option value="all">全部类型</option><option v-for="(label,key) in types" :key="key" :value="key">{{ label }}</option></select></div><button v-for="page in visible" :key="page.id" :class="{ selected: selected?.id === page.id }" @click="selectedId = page.id"><strong>{{ page.title }}</strong><small>{{ types[page.type] }} · {{ page.domain }}</small></button><p v-if="!visible.length">没有匹配内容。试试其他关键词或主题。</p></aside>
      <article v-if="selected" class="paper-panel wiki-article"><div class="wiki-meta">{{ types[selected.type] }} / {{ selected.domain }}</div><h2>{{ selected.title }}</h2><p class="wiki-summary">{{ selected.summary }}</p><section v-if="selected.formula"><h3>公式或口径</h3><code>{{ selected.formula }}</code></section><section><h3>什么时候用</h3><p>{{ selected.decisionUse }}</p></section><section><h3>使用前检查</h3><p>{{ selected.qualityGate }}</p></section><section><h3>容易踩的坑</h3><ul><li v-for="item in selected.caveats" :key="item">{{ item }}</li></ul></section><section><h3>依据与来源</h3><ul><li v-for="item in selected.sources" :key="item"><code>{{ item }}</code></li></ul></section><section v-if="related.length"><h3>接着读</h3><div class="wiki-related"><button v-for="page in related" :key="page.id" @click="selectedId = page.id">{{ page.title }} →</button></div></section></article></div>
  </div>
</template>

<style scoped>
.wiki-page{margin-top:14px}.wiki-heading{display:flex;justify-content:space-between;gap:15px}.wiki-heading strong{font-size:17px}.wiki-heading p,.wiki-heading span{font-size:12px;color:#69736b}.wiki-layout{display:grid;grid-template-columns:300px minmax(0,1fr);gap:13px;margin-top:10px}.wiki-index{max-height:710px;overflow:auto}.wiki-index input,.wiki-index select{box-sizing:border-box;width:100%;border:1px solid #dcded4;background:white;padding:8px}.wiki-filters{display:flex;gap:6px;margin:7px 0}.wiki-index>button{width:100%;text-align:left;border:0;border-bottom:1px solid #e1e3d8;background:transparent;padding:10px;cursor:pointer}.wiki-index>button.selected,.wiki-index>button:hover{background:#f5f1e8}.wiki-index strong,.wiki-index small{display:block}.wiki-index small{font-size:11px;color:#69736b;margin-top:4px}.wiki-article{min-height:530px}.wiki-meta{font-size:11px;color:#a06922;letter-spacing:.08em}.wiki-article h2{font-size:28px;margin:8px 0 12px}.wiki-summary{font-size:15px;line-height:1.7;color:#33483d}.wiki-article section{border-top:1px solid #e1e3d8;padding:12px 0}.wiki-article h3{font-size:13px;margin:0 0 7px}.wiki-article p,.wiki-article li{font-size:12px;line-height:1.8}.wiki-article code{font-size:11px;white-space:normal;overflow-wrap:anywhere}.wiki-related{display:flex;gap:8px;flex-wrap:wrap}.wiki-related button{border:1px solid #e1e3d8;background:white;padding:7px;cursor:pointer;font-size:12px}@media(max-width:750px){.wiki-layout{grid-template-columns:1fr}.wiki-index{max-height:280px}}
.protocol-button{margin-left:10px;border:1px solid #dedfd5;background:white;padding:6px 9px;cursor:pointer}.wiki-entry{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}.wiki-entry button{text-align:left;border:1px solid #e1e3d8;background:#fffefa;padding:10px;cursor:pointer}.wiki-entry button:hover{background:#f5f1e8}.wiki-entry strong,.wiki-entry small{display:block}.wiki-entry strong{font-size:12px}.wiki-entry small{font-size:11px;color:#69736b;margin-top:4px;line-height:1.4}.wiki-protocol{margin-top:10px}.wiki-protocol p,.wiki-protocol li{font-size:12px;line-height:1.7}.wiki-operations{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.wiki-operations article{border:1px solid #e1e3d8;padding:10px}.wiki-operations strong{font-size:12px}@media(max-width:900px){.wiki-entry{grid-template-columns:repeat(2,1fr)}}@media(max-width:650px){.wiki-operations{grid-template-columns:1fr}}
</style>
