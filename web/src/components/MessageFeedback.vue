<script setup lang="ts">
import { computed, ref } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';
import DataList from './DataList.vue';

type Row = Record<string, unknown>;
const props = defineProps<{ projectId: string; requestId: string; metadata?: Row }>();
const expanded = ref(false);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const knowledge = ref<Row | null>(null);
const revisions = ref<string[]>([]);
const candidate = computed(() => props.metadata?.personalizationCandidate && typeof props.metadata.personalizationCandidate === 'object' ? props.metadata.personalizationCandidate as Row : null);
const candidateValue = ref('');
const candidateScope = ref('project');
const candidateDismissed = ref(false);
const metrics = computed(() => props.metadata?.turnMetrics && typeof props.metadata.turnMetrics === 'object' ? props.metadata.turnMetrics as Row : null);
const base = computed(() => `/api/projects/${encodeURIComponent(props.projectId)}`);

async function open() {
  expanded.value = !expanded.value;
  if (!expanded.value || knowledge.value !== null || loading.value) return;
  loading.value = true;
  const [knowledgeResult, memoryResult] = await Promise.allSettled([
    apiRequest<ApiEnvelope<Row>>(`${base.value}/knowledge/uses/${encodeURIComponent(props.requestId)}`),
    apiRequest<ApiEnvelope<Row>>(`${base.value}/memory/uses/${encodeURIComponent(props.requestId)}`),
  ]);
  knowledge.value = knowledgeResult.status === 'fulfilled' ? unwrap(knowledgeResult.value) || {} : {};
  if (memoryResult.status === 'fulfilled') {
    const payload = unwrap(memoryResult.value);
    revisions.value = Array.isArray(payload?.revisionIds) ? payload.revisionIds.filter((item): item is string => typeof item === 'string') : [];
  }
  loading.value = false;
}
async function rateKnowledge(outcome: 'helped' | 'neutral' | 'harmed') {
  busy.value = true; error.value = '';
  try {
    knowledge.value = unwrap(await apiRequest<ApiEnvelope<Row>>(`${base.value}/knowledge/outcomes`, {
      method: 'POST', body: JSON.stringify({ requestId: props.requestId, eventId: `knowledge-feedback:${props.requestId.slice(0, 160)}:${outcome}`, outcome }),
    }));
    notice.value = '知识反馈已记录';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '反馈失败'; }
  finally { busy.value = false; }
}
async function rateMemory(kind: 'helpful' | 'rejected') {
  busy.value = true; error.value = '';
  try {
    await Promise.all(revisions.value.map((revisionId, index) => apiRequest(`${base.value}/memory/outcomes`, {
      method: 'POST', body: JSON.stringify({ requestId: props.requestId, revisionId, eventId: `memory-feedback:${props.requestId.slice(0, 96)}:${index}:${kind}`, kind, weight: 1 }),
    })));
    notice.value = '记忆反馈已记录';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '反馈失败'; }
  finally { busy.value = false; }
}
async function saveCandidate() {
  if (!candidate.value || !String(candidate.value.key || '') || !candidateValue.value.trim()) return;
  busy.value = true; error.value = '';
  try {
    const value = candidateValue.value.trim();
    await apiRequest(`${base.value}/memory/preferences`, { method: 'POST', body: JSON.stringify({
      eventId: `memory-candidate:${props.requestId.replace(/[^A-Za-z0-9._:-]/g, '-').slice(0, 120)}`,
      key: candidate.value.key, value, evidenceText: `用户通过 AlphaDeck“记住偏好”面板明确确认：${value}`,
      confidence: 1, scope: candidateScope.value,
    }) });
    candidateDismissed.value = true;
    notice.value = '偏好已保存，后续研究可以使用';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '偏好保存失败'; }
  finally { busy.value = false; }
}
if (candidate.value) {
  candidateValue.value = String(candidate.value.value || '');
  candidateScope.value = candidate.value.scope === 'global' ? 'global' : 'project';
}
</script>

<template>
  <div class="message-feedback">
    <div v-if="candidate && !candidateDismissed" class="feedback-candidate">
      <strong>这次研究发现一条可能值得记住的偏好</strong>
      <p>{{ candidate.reason || '请确认是否保存；不会自动写入你的记忆。' }}</p>
      <label>偏好内容<input v-model="candidateValue" maxlength="1024" /></label>
      <label>使用范围<select v-model="candidateScope"><option value="project">只在这个项目</option><option value="global">我的所有项目</option></select></label>
      <div class="feedback-actions"><button :disabled="busy || !candidateValue.trim()" @click="saveCandidate">确认记住</button><button @click="candidateDismissed = true">暂不保存</button></div>
    </div>
    <button class="feedback-toggle" type="button" :aria-expanded="expanded" @click="open">{{ expanded ? '收起本轮依据' : '查看本轮依据与反馈' }} {{ metrics ? '· 含运行指标' : '' }}</button>
    <div v-if="expanded" class="feedback-body">
      <p v-if="loading">正在读取本轮依据…</p>
      <template v-else>
        <div v-if="metrics"><strong>运行指标</strong><DataList :value="metrics" :limit="10" /></div>
        <div v-if="knowledge && Number(knowledge.citationCount || 0) > 0"><strong>业务知识</strong><p>引用 {{ knowledge.citationCount }} 处 · 涉及 {{ knowledge.revisionCount || 0 }} 个版本</p><div v-if="knowledge.feedbackAvailable" class="feedback-actions"><button :disabled="busy" @click="rateKnowledge('helped')">有帮助</button><button :disabled="busy" @click="rateKnowledge('neutral')">一般</button><button :disabled="busy" @click="rateKnowledge('harmed')">有误导</button></div><p v-else-if="knowledge.feedbackOutcome">已记录：{{ knowledge.feedbackOutcome }}</p></div>
        <div v-if="revisions.length"><strong>本轮使用的个人记忆</strong><p>召回 {{ revisions.length }} 条记录</p><div class="feedback-actions"><button :disabled="busy" @click="rateMemory('helpful')">有帮助</button><button :disabled="busy" @click="rateMemory('rejected')">不符合我的偏好</button></div></div>
        <p v-if="!metrics && !revisions.length && !Number(knowledge?.citationCount || 0)">本轮没有可显示的知识或记忆归因。</p>
      </template>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="feedback-notice">{{ notice }}</p>
    </div>
  </div>
</template>

<style scoped>
.message-feedback{margin-top:12px;border-top:1px solid #e5e4dc;padding-top:9px}.feedback-toggle{border:0;background:transparent;color:#9a651e;padding:3px 0;font-size:11px;font-weight:700}.feedback-body{margin-top:9px;background:#f7f5ef;border:1px solid #e6e2d8;border-radius:6px;padding:12px;font-size:12px}.feedback-body>div+div{border-top:1px solid #e4e1d8;padding-top:10px;margin-top:10px}.feedback-body strong,.feedback-candidate strong{display:block;margin-bottom:5px}.feedback-body p,.feedback-candidate p{color:#657169;margin:5px 0}.feedback-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}.feedback-actions button{border:1px solid #d8d7ca;border-radius:5px;background:#fffefa;padding:6px 9px;font-size:11px}.feedback-candidate{padding:12px;background:#f5efe2;border-left:3px solid #ab762c;margin-bottom:11px}.feedback-candidate label{display:grid;gap:4px;font-size:11px;margin:8px 0}.feedback-candidate input,.feedback-candidate select{border:1px solid #d7d4c9;border-radius:5px;background:white;padding:7px}.feedback-notice{color:#327356!important}
</style>
