<script setup lang="ts">
import { computed, ref } from 'vue';
import { apiRequest } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ cases: Row[]; queue: Row[] }>();
const emit = defineEmits<{ updated: [] }>();
const name = ref('');
const question = ref('');
const capabilityId = ref('asset_comparison');
const expectedSymbols = ref('');
const setName = ref('');
const selectedCases = ref<string[]>([]);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const capabilityOptions = computed(() => [...new Set(props.cases.map(item => String(item.capabilityId || '')).filter(Boolean))]);

async function mutate(body: Row) {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest('/api/evals', { method: 'POST', body: JSON.stringify(body) });
    notice.value = '已保存'; emit('updated');
    return true;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败'; return false; }
  finally { busy.value = false; }
}
async function addCase() {
  if (await mutate({ action: 'create-case', name: name.value.trim(), question: question.value.trim(), capabilityId: capabilityId.value, expectedSymbols: expectedSymbols.value, type: 'generated_project' })) {
    name.value = ''; question.value = ''; expectedSymbols.value = '';
  }
}
async function addSet() {
  if (await mutate({ action: 'create-eval-set', name: setName.value.trim(), caseIds: selectedCases.value })) {
    setName.value = ''; selectedCases.value = [];
  }
}
async function cancel(item: Row) {
  if (!window.confirm('取消这项评测任务？')) return;
  await mutate({ action: 'cancel-benchmark', queueId: item.id });
}
</script>

<template>
  <section class="eval-manager paper-panel module-section">
    <h2>管理评测内容</h2>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="eval-notice">{{ notice }}</p>
    <div class="eval-manager-grid">
      <div><h3>新增用例</h3><p>写一个真实用户会提出的问题，方便下次回归检查。</p><label>用例名称<input v-model="name" placeholder="例如：两只白酒股的近一年对比" /></label><label>用户问题<textarea v-model="question" rows="3" placeholder="比较贵州茅台与五粮液的走势和风险" /></label><label>研究方向<select v-model="capabilityId"><option v-for="id in capabilityOptions" :key="id" :value="id">{{ id }}</option></select></label><label>预期股票代码<input v-model="expectedSymbols" placeholder="600519, 000858（可选）" /></label><button class="primary-button" :disabled="busy || !name.trim() || !question.trim()" @click="addCase">保存用例</button></div>
      <div><h3>创建评测集</h3><p>把常用用例组成一组，启动评测时可直接选择。</p><label>评测集名称<input v-model="setName" placeholder="例如：白酒研究回归" /></label><div class="eval-cases"><label v-for="item in cases" :key="String(item.id)" class="eval-case"><input v-model="selectedCases" type="checkbox" :value="String(item.id)" /><span>{{ item.name || item.id }}</span></label></div><button class="primary-button" :disabled="busy || !setName.trim() || !selectedCases.length" @click="addSet">保存评测集（{{ selectedCases.length }} 项）</button></div>
    </div>
    <div v-if="queue.length" class="eval-queue"><h3>排队和运行中的评测</h3><div v-for="item in queue" :key="String(item.id)" class="eval-queue-row"><span>{{ item.id }} · {{ item.status }}</span><button v-if="['queued','running'].includes(String(item.status))" :disabled="busy" @click="cancel(item)">取消</button></div></div>
  </section>
</template>

<style scoped>
.eval-manager h2{font-size:17px}.eval-manager h3{font-size:14px}.eval-manager p{font-size:12px;color:#68746b}.eval-manager-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.eval-manager-grid>div{min-width:0}.eval-manager-grid label{display:grid;gap:5px;margin:12px 0;font-size:12px;color:#68746b}.eval-manager input,.eval-manager textarea,.eval-manager select{width:100%;padding:9px;border:1px solid #dcded4;border-radius:5px;background:white;font:inherit}.eval-manager button{padding:9px 12px}.eval-cases{max-height:230px;overflow:auto;border:1px solid #e1e3d8;padding:6px 10px}.eval-case{display:flex!important;align-items:center;gap:8px!important;margin:7px 0!important;color:#263c33!important}.eval-case input{width:auto}.eval-queue{margin-top:20px}.eval-queue-row{display:flex;justify-content:space-between;gap:12px;border-top:1px solid #e1e3d8;padding:10px 0;font-size:12px}.eval-notice{color:#2d775d!important}@media(max-width:720px){.eval-manager-grid{grid-template-columns:1fr}}
</style>
