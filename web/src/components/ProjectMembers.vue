<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ projectId: string }>();
const base = computed(() => `/api/projects/${encodeURIComponent(props.projectId)}/members`);
const members = ref<Row[]>([]);
const ownerId = ref('');
const email = ref('');
const role = ref('viewer');
const busy = ref(false);
const error = ref('');
const notice = ref('');
async function load() {
  if (!props.projectId) return;
  busy.value = true; error.value = '';
  try {
    const result = unwrap(await apiRequest<ApiEnvelope<Row>>(base.value));
    ownerId.value = String(result.ownerId || '');
    members.value = Array.isArray(result.memberships) ? result.memberships as Row[] : [];
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取成员失败'; }
  finally { busy.value = false; }
}
async function save() {
  if (!email.value.trim()) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(base.value, { method: 'PUT', body: JSON.stringify({ email: email.value.trim(), role: role.value }) });
    notice.value = '成员权限已保存'; email.value = ''; await load();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存失败'; }
  finally { busy.value = false; }
}
async function remove(item: Row) {
  const user = item.user as Row | undefined;
  if (!user?.id || !window.confirm(`移除 ${user.name || user.email}？`)) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(base.value, { method: 'DELETE', body: JSON.stringify({ userId: user.id }) });
    notice.value = '成员已移除'; await load();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '移除失败'; }
  finally { busy.value = false; }
}
watch(() => props.projectId, load);
onMounted(load);
</script>

<template>
  <div class="members-panel">
    <p>为已有账号设置当前项目的只读或可编辑权限。</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="members-notice">{{ notice }}</p>
    <div class="members-form"><label>成员邮箱<input v-model="email" type="email" placeholder="name@example.com" /></label><label>项目权限<select v-model="role"><option value="viewer">只读</option><option value="editor">可编辑</option></select></label><button class="primary-button" :disabled="busy || !email.trim()" @click="save">添加或更新</button></div>
    <h3>当前成员</h3><p v-if="!members.length">暂无可显示的成员。</p>
    <div v-for="item in members" :key="String(item.id)" class="member-row"><div><strong>{{ (item.user as Row)?.name || (item.user as Row)?.email }}</strong><small>{{ (item.user as Row)?.email }} · {{ item.role === 'owner' ? '所有者' : item.role === 'editor' ? '可编辑' : '只读' }}</small></div><button v-if="String((item.user as Row)?.id) !== ownerId" class="secondary-button" :disabled="busy" @click="remove(item)">移除</button></div>
  </div>
</template>

<style scoped>
.members-panel{padding:4px 0}.members-form{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:18px 0}.members-form label{display:grid;gap:6px;font-size:12px;color:#68746b}.members-form input,.members-form select{border:1px solid #dcded4;border-radius:6px;padding:9px;background:#fff;min-width:170px}.members-form button{padding:10px 14px}.member-row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-top:1px solid #e1e3d8}.member-row strong,.member-row small{display:block}.member-row small{color:#68746b;margin-top:5px}.members-notice{color:#2d775d}
</style>
