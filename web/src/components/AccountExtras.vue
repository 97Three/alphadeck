<script setup lang="ts">
import { computed, ref } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';
import DataList from './DataList.vue';

type Row = Record<string, unknown>;
const props = defineProps<{ kind: 'memory' | 'sessions'; data: unknown }>();
const emit = defineEmits<{ updated: [] }>();
const busy = ref(false);
const error = ref('');
const notice = ref('');
const editing = ref('');
const value = ref('');
const reason = ref('');
const historyId = ref('');
const history = ref<unknown[]>([]);
const record = computed<Row>(() => props.data && typeof props.data === 'object' && !Array.isArray(props.data) ? props.data as Row : {});
const preferences = computed<Row[]>(() => Array.isArray(record.value.preferences) ? record.value.preferences as Row[] : []);
const sessions = computed<Row[]>(() => Array.isArray(props.data) ? props.data as Row[] : Array.isArray(record.value.sessions) ? record.value.sessions as Row[] : []);

async function revoke(payload: Row) {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest('/api/account/sessions', { method: 'DELETE', body: JSON.stringify(payload) });
    notice.value = '登录会话已撤销'; emit('updated');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '撤销失败'; }
  finally { busy.value = false; }
}
async function revisions(id: string) {
  if (historyId.value === id) { historyId.value = ''; return; }
  busy.value = true; error.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<unknown[]>>(`/api/account/memory/preferences/${encodeURIComponent(id)}/revisions`));
    history.value = Array.isArray(result) ? result : [];
    historyId.value = id;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取历史失败'; }
  finally { busy.value = false; }
}
async function correct(item: Row) {
  const id = String(item.recordId || '');
  if (!value.value.trim() || !reason.value.trim()) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(`/api/account/memory/preferences/${encodeURIComponent(id)}/corrections`, {
      method: 'POST', body: JSON.stringify({
        eventId: `memory-correction:${crypto.randomUUID()}`,
        value: value.value.trim(),
        reason: reason.value.trim(),
        evidenceText: `用户在 AlphaDeck 账号记忆中心明确修正：${value.value.trim()}`,
        expectedRevisionId: item.revisionId,
      }),
    });
    editing.value = ''; value.value = ''; reason.value = ''; notice.value = '偏好已修正'; emit('updated');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '修正失败'; }
  finally { busy.value = false; }
}
</script>

<template>
  <div class="account-extra">
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="module-notice">{{ notice }}</p>
    <template v-if="kind === 'sessions'">
      <div class="account-extra-head"><h3>登录设备</h3><button class="secondary-button" :disabled="busy" @click="revoke({ allOthers: true })">撤销其他会话</button></div>
      <p v-if="!sessions.length">没有可展示的活跃会话。</p>
      <div v-for="session in sessions" :key="String(session.id)" class="extra-row">
        <div><strong>{{ session.isCurrent ? '当前设备' : '其他设备' }}</strong><small>{{ session.ipAddress || 'IP 未记录' }} · {{ session.userAgent || '浏览器' }} · {{ session.updatedAt }}</small></div>
        <button v-if="!session.isCurrent" class="secondary-button" :disabled="busy" @click="revoke({ sessionId: session.id })">撤销</button>
      </div>
    </template>
    <template v-else>
      <p v-if="record.preferences === null">记忆服务暂时不可用。</p>
      <p v-else-if="!preferences.length">还没有保存的偏好。可以在项目对话里明确要求“记住偏好”。</p>
      <article v-for="item in preferences" :key="String(item.recordId)" class="extra-row memory-row">
        <div><strong>{{ item.key }}</strong><p>{{ item.value }}</p><small>版本 {{ item.sequence }} · 置信度 {{ Math.round(Number(item.confidence || 0) * 100) }}%</small></div>
        <div class="extra-actions"><button class="secondary-button" :disabled="busy" @click="revisions(String(item.recordId))">{{ historyId === item.recordId ? '收起历史' : '版本历史' }}</button><button class="secondary-button" :disabled="busy" @click="editing = String(item.recordId); value = String(item.value || '')">修正</button></div>
        <div v-if="historyId === item.recordId" class="extra-detail"><DataList v-for="(revision, index) in history" :key="index" :value="revision" :limit="8" /></div>
        <div v-if="editing === item.recordId" class="extra-detail extra-form"><label>正确的偏好内容<textarea v-model="value" /></label><label>为什么修改<textarea v-model="reason" /></label><div><button class="secondary-button" @click="editing = ''">取消</button><button class="primary-button" :disabled="busy || !value.trim() || !reason.trim()" @click="correct(item)">保存修正</button></div></div>
      </article>
    </template>
  </div>
</template>

<style scoped>
.account-extra{margin-top:18px}.account-extra-head,.extra-row,.extra-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.extra-row{border-top:1px solid #e1e3d8;padding:14px 0;flex-wrap:wrap}.extra-row>div:first-child{min-width:0;flex:1}.extra-row strong,.extra-row small{display:block}.extra-row small{margin-top:5px;color:#68746b;overflow-wrap:anywhere}.memory-row{display:flex}.memory-row p{margin:7px 0}.extra-detail{flex-basis:100%;padding:10px;background:#f7f6f0;border-radius:6px}.extra-form label{display:grid;gap:5px;margin-bottom:10px}.extra-form textarea{min-height:65px;border:1px solid #dcded4;border-radius:6px;padding:8px}.extra-form>div{display:flex;gap:8px}.extra-actions{justify-content:flex-end}
</style>
