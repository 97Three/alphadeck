<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ projectId: string }>();
const records = ref<Row[]>([]);
const key = ref('');
const value = ref('');
const scope = ref('global');
const busy = ref(false);
const error = ref('');
const notice = ref('');
async function load() {
  if (!props.projectId) return;
  busy.value = true; error.value = '';
  try {
    const data = unwrap(await apiRequest<ApiEnvelope<unknown>>(`/api/projects/${encodeURIComponent(props.projectId)}/memory/preferences`));
    records.value = Array.isArray(data) ? data as Row[] : Array.isArray((data as Row)?.preferences) ? (data as Row).preferences as Row[] : [];
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取偏好失败'; }
  finally { busy.value = false; }
}
async function remember() {
  if (!key.value.trim() || !value.value.trim()) return;
  if (!window.confirm(`确认记住这条${scope.value === 'global' ? '全局' : '项目'}偏好？`)) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(`/api/projects/${encodeURIComponent(props.projectId)}/memory/preferences`, {
      method: 'POST', body: JSON.stringify({
        eventId: `memory-confirmation:${crypto.randomUUID()}`,
        key: key.value.trim(), value: value.value.trim(),
        evidenceText: `用户在 AlphaDeck 项目管理中确认记住偏好：${key.value.trim()} = ${value.value.trim()}`,
        scope: scope.value,
      }),
    });
    notice.value = '已保存偏好'; key.value = ''; value.value = ''; await load();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存偏好失败'; }
  finally { busy.value = false; }
}
watch(() => props.projectId, load);
onMounted(load);
</script>

<template>
  <div class="project-memory">
    <p>明确确认的研究偏好可以在后续对话中召回。外部 Memory 服务需要单独启动。</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="memory-notice">{{ notice }}</p>
    <div class="memory-form"><label>偏好名称<input v-model="key" placeholder="例如 研究表达方式" /></label><label>希望记住的内容<input v-model="value" placeholder="例如 先给结论，再解释依据" /></label><label>作用范围<select v-model="scope"><option value="global">我的所有项目</option><option value="project">仅当前项目</option></select></label><button class="primary-button" :disabled="busy || !key.trim() || !value.trim()" @click="remember">记住偏好</button></div>
    <h3>已保存偏好</h3><p v-if="!records.length">目前没有可显示的偏好。</p><div v-for="item in records" :key="String(item.recordId || item.id)" class="memory-row"><strong>{{ item.key }}</strong><span>{{ item.value }}</span></div>
  </div>
</template>

<style scoped>
.project-memory p{color:#68746b;font-size:13px}.memory-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:18px 0}.memory-form label{display:grid;gap:6px;font-size:12px;color:#68746b}.memory-form input,.memory-form select{border:1px solid #dcded4;border-radius:6px;padding:9px;background:#fff}.memory-form button{align-self:end;padding:10px 14px}.memory-row{display:flex;gap:15px;padding:10px 0;border-top:1px solid #e1e3d8}.memory-row strong{min-width:150px}.memory-notice{color:#2d775d!important}@media(max-width:760px){.memory-form{grid-template-columns:1fr}}
</style>
