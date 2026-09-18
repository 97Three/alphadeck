<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ userId: string }>();
const catalog = ref<Row>({});
const details = ref<Row>({});
const permissionProfileId = ref('');
const quotaProfileId = ref('');
const permissionOverrides = ref<Row[]>([]);
const quotaOverrides = ref<Row[]>([]);
const permissionKey = ref('');
const permissionEffect = ref('allow');
const quotaMetric = ref('');
const quotaLimit = ref('');
const changeReason = ref('');
const busy = ref(false);
const error = ref('');
const notice = ref('');
const permissionProfiles = () => Array.isArray(catalog.value.permissionProfiles) ? catalog.value.permissionProfiles as Row[] : [];
const quotaProfiles = () => Array.isArray(catalog.value.quotaProfiles) ? catalog.value.quotaProfiles as Row[] : [];
const actions = () => Array.isArray(catalog.value.actions) ? catalog.value.actions as Row[] : [];
const quotas = () => Array.isArray(details.value.quotas) ? details.value.quotas as Row[] : [];
const isAdmin = () => (details.value.user as Row | undefined)?.isAdmin === true;

async function load() {
  if (!props.userId) return;
  busy.value = true; error.value = '';
  try {
    const [catalogResult, detailsResult] = await Promise.all([
      apiRequest<ApiEnvelope<Row>>('/api/admin/access-control'),
      apiRequest<ApiEnvelope<Row>>(`/api/admin/users/${encodeURIComponent(props.userId)}/access`),
    ]);
    catalog.value = unwrap(catalogResult);
    details.value = unwrap(detailsResult);
    permissionProfileId.value = String((details.value.permissionProfile as Row | undefined)?.id || '');
    quotaProfileId.value = String((details.value.quotaProfile as Row | undefined)?.id || '');
    permissionOverrides.value = Array.isArray(details.value.permissionOverrides) ? (details.value.permissionOverrides as Row[]).map(item => ({ permissionKey: item.permissionKey, effect: item.effect, expiresAt: item.expiresAt || null })) : [];
    quotaOverrides.value = Array.isArray(details.value.quotaOverrides) ? (details.value.quotaOverrides as Row[]).map(item => ({ metric: item.metric, isUnlimited: item.isUnlimited, limit: item.limit, enforcement: item.enforcement, windowType: item.windowType, windowSeconds: item.windowSeconds, reservationTtlSeconds: item.reservationTtlSeconds, expiresAt: item.expiresAt || null })) : [];
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取权限与配额失败'; }
  finally { busy.value = false; }
}
function addPermission() {
  if (!permissionKey.value) return;
  permissionOverrides.value = [...permissionOverrides.value.filter(item => item.permissionKey !== permissionKey.value), { permissionKey: permissionKey.value, effect: permissionEffect.value, expiresAt: null }];
}
function addQuota() {
  if (!quotaMetric.value || !/^\d+$/.test(quotaLimit.value) || Number(quotaLimit.value) <= 0) { error.value = '配额上限必须是正整数'; return; }
  const rule = quotaProfiles().flatMap(profile => Array.isArray(profile.rules) ? profile.rules as Row[] : []).find(item => item.metric === quotaMetric.value);
  if (!rule) { error.value = '没有找到该指标的配额规则'; return; }
  quotaOverrides.value = [...quotaOverrides.value.filter(item => item.metric !== quotaMetric.value), {
    metric: quotaMetric.value, isUnlimited: false, limit: quotaLimit.value,
    enforcement: rule.enforcement, windowType: rule.windowType,
    windowSeconds: rule.windowSeconds, reservationTtlSeconds: rule.reservationTtlSeconds,
    expiresAt: null,
  }];
  error.value = '';
}
async function save() {
  if (changeReason.value.trim().length < 3) { error.value = '请填写至少 3 个字的变更原因'; return; }
  const user = details.value.user as Row | undefined;
  if (!user) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest(`/api/admin/users/${encodeURIComponent(props.userId)}/access`, {
      method: 'PATCH', body: JSON.stringify({
        expectedAccessVersion: user.accessVersion,
        reason: changeReason.value.trim(),
        permissionProfileId: permissionProfileId.value || null,
        quotaProfileId: quotaProfileId.value || null,
        permissionOverrides: permissionOverrides.value,
        quotaOverrides: quotaOverrides.value,
      }),
    });
    notice.value = '权限与配额已保存，审计记录已生成'; changeReason.value = ''; await load();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存失败'; }
  finally { busy.value = false; }
}
watch(() => props.userId, load);
onMounted(load);
</script>

<template>
  <section v-if="userId" class="access-panel paper-panel module-section">
    <div class="panel-heading"><strong>权限与使用配额</strong><button class="secondary-button" :disabled="busy" @click="load">刷新</button></div>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="access-notice">{{ notice }}</p>
    <p v-if="isAdmin()">管理员拥有全部权限与无限配额。这里显示实际用量。</p>
    <template v-else>
      <div class="access-form"><label>权限模板<select v-model="permissionProfileId"><option value="">默认模板</option><option v-for="item in permissionProfiles()" :key="String(item.id)" :value="String(item.id)">{{ item.name }}</option></select></label><label>配额模板<select v-model="quotaProfileId"><option value="">默认模板</option><option v-for="item in quotaProfiles()" :key="String(item.id)" :value="String(item.id)">{{ item.name }}</option></select></label></div>
      <h3>单项权限</h3><div class="access-form"><label>操作<select v-model="permissionKey"><option value="">选择操作</option><option v-for="item in actions()" :key="String(item.key)" :value="String(item.key)">{{ item.name || item.label || item.key }}</option></select></label><label>处理<select v-model="permissionEffect"><option value="allow">允许</option><option value="deny">拒绝</option></select></label><button class="secondary-button" :disabled="!permissionKey" @click="addPermission">添加例外</button></div>
      <div v-for="item in permissionOverrides" :key="String(item.permissionKey)" class="access-row"><span>{{ item.permissionKey }} · {{ item.effect === 'allow' ? '允许' : '拒绝' }}</span><button class="secondary-button" @click="permissionOverrides = permissionOverrides.filter(row => row.permissionKey !== item.permissionKey)">移除例外</button></div>
      <h3>单项配额</h3><div class="access-form"><label>指标<select v-model="quotaMetric"><option value="">选择指标</option><option v-for="item in quotas()" :key="String(item.metric)" :value="String(item.metric)">{{ item.metric }}</option></select></label><label>上限<input v-model="quotaLimit" type="number" min="1" placeholder="正整数" /></label><button class="secondary-button" :disabled="!quotaMetric || !quotaLimit" @click="addQuota">添加例外</button></div>
      <div v-for="item in quotaOverrides" :key="String(item.metric)" class="access-row"><span>{{ item.metric }} · {{ item.isUnlimited ? '不限' : item.limit }}</span><button class="secondary-button" @click="quotaOverrides = quotaOverrides.filter(row => row.metric !== item.metric)">移除例外</button></div>
      <div class="access-form"><label>变更原因<input v-model="changeReason" placeholder="说明为什么调整" /></label><button class="primary-button" :disabled="busy || changeReason.trim().length < 3" @click="save">保存权限与配额</button></div>
    </template>
    <h3>当前有效配额</h3><div v-for="item in quotas()" :key="String(item.metric)" class="access-row"><span>{{ item.metric }}</span><small>已用 {{ item.used }} · 预留 {{ item.reserved }} · 上限 {{ item.unlimited ? '不限' : item.limit }}</small></div>
  </section>
</template>

<style scoped>
.access-panel{margin-top:16px}.access-panel h3{font-size:14px;margin:20px 0 8px}.access-panel p{color:#68746b}.access-form{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:15px 0}.access-form label{display:grid;gap:6px;font-size:12px;color:#68746b}.access-form input,.access-form select{min-width:180px;border:1px solid #dcded4;border-radius:6px;padding:9px;background:#fff}.access-form button{padding:10px 14px}.access-row{display:flex;justify-content:space-between;gap:12px;border-top:1px solid #e1e3d8;padding:10px 0;align-items:center}.access-row small{color:#68746b}.access-notice{color:#2d775d!important}
</style>
