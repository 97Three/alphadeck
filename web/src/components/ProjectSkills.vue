<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { apiRequest, unwrap, type ApiEnvelope } from '../lib/api';

type Row = Record<string, unknown>;
const props = defineProps<{ projectId: string }>();
const target = ref('pi-agent');
const market = ref<Row>({});
const selectedId = ref('');
const version = ref('');
const overwrite = ref(false);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const skills = computed(() => Array.isArray(market.value.skills) ? market.value.skills as Row[] : []);
const targets = computed(() => Array.isArray(market.value.targets) ? market.value.targets as Row[] : []);
const selected = computed(() => skills.value.find(item => item.id === selectedId.value));
const deployment = computed(() => (market.value.project as Row | undefined)?.deployment as Row | undefined);
const releases = computed(() => Array.isArray(selected.value?.releases) ? (selected.value.releases as Row[]).filter(item => item.installable) : []);
const installed = computed(() => Array.isArray(deployment.value?.skillIds) && (deployment.value.skillIds as string[]).includes(selectedId.value));

async function load() {
  error.value = '';
  try {
    const query = new URLSearchParams({ projectId: props.projectId, target: target.value });
    market.value = unwrap(await apiRequest<ApiEnvelope<Row>>(`/api/skills/market?${query}`));
    if (!skills.value.some(item => item.id === selectedId.value)) selectedId.value = String(skills.value[0]?.id || '');
    version.value = String(selected.value?.version || '');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取技能目录失败'; }
}

async function change(action: 'install' | 'uninstall' | 'rollback') {
  if (!selected.value || busy.value) return;
  const label = action === 'install' ? `安装 ${selected.value.name} v${version.value}` : action === 'uninstall' ? `卸载 ${selected.value.name}` : '恢复上一次安装的完整技能集';
  if (!window.confirm(`确认在当前项目的 ${target.value} 中${label}？`)) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await apiRequest('/api/skills/installations', { method: 'POST', body: JSON.stringify({
      projectId: props.projectId, target: target.value, action,
      skillId: selectedId.value, version: version.value,
      expectedRevision: deployment.value?.id || null, overwriteModified: overwrite.value,
    }) });
    notice.value = '项目技能已更新'; overwrite.value = false; await load();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '技能操作失败'; }
  finally { busy.value = false; }
}

watch([target, () => props.projectId], load);
watch(selectedId, () => { version.value = String(selected.value?.version || ''); });
onMounted(load);
</script>

<template>
  <div class="project-skills">
    <div class="skills-heading"><div><h3>项目技能</h3><p>安装后会固定当前项目使用的技能版本。平台更新不会自动改动此项目。</p></div><button class="secondary-button" @click="load">刷新</button></div>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p><p v-if="notice" class="skills-notice">{{ notice }}</p>
    <div class="skills-controls"><label>目标 Agent<select v-model="target"><option v-for="item in targets" :key="String(item.id)" :value="String(item.id)">{{ item.label || item.id }}</option><option v-if="!targets.length" value="pi-agent">PI Agent</option></select></label><span>{{ deployment ? `固定版本 ${deployment.revision}` : '尚未安装技能集' }}</span></div>
    <div class="skills-layout"><div class="skills-list"><button v-for="item in skills" :key="String(item.id)" :class="{ active: item.id === selectedId }" @click="selectedId = String(item.id)"><strong>{{ item.name }}</strong><small>v{{ item.version }} · {{ item.health === 'ok' ? '已验证' : '待检查' }}</small></button></div><div v-if="selected" class="skills-detail"><h3>{{ selected.name }}</h3><p>{{ selected.boundary }}</p><p>{{ installed ? '已安装到此项目' : '此项目未安装' }}</p><label>安装版本<select v-model="version"><option v-for="release in releases" :key="String(release.version)" :value="String(release.version)">v{{ release.version }}</option></select></label><label class="skills-check"><input v-model="overwrite" type="checkbox" />覆盖受管技能的本地修改</label><div class="skills-actions"><button :disabled="busy || !selected.packageVerified || !version" @click="change('install')">{{ installed ? '更新版本' : '安装' }}</button><button :disabled="busy || !installed" @click="change('uninstall')">卸载</button><button :disabled="busy || !deployment?.canRollback" @click="change('rollback')">恢复上一次安装</button></div><p v-if="!selected.packageVerified">当前技能包未通过完整性检查，暂不能安装。</p></div></div>
  </div>
</template>

<style scoped>
.skills-heading,.skills-controls,.skills-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.skills-heading p,.skills-detail p{font-size:12px;color:#68746b;line-height:1.6}.skills-controls{margin:16px 0;padding:12px;background:#f6f5f0}.skills-controls label,.skills-detail label{display:grid;gap:6px;font-size:12px;color:#68746b}.skills-controls select,.skills-detail select{padding:8px;border:1px solid #dcded4;border-radius:5px;background:white}.skills-layout{display:grid;grid-template-columns:minmax(190px,1fr) minmax(0,2fr);gap:16px}.skills-list{max-height:420px;overflow:auto;border:1px solid #e1e3d8}.skills-list button{display:block;width:100%;padding:10px;text-align:left;background:white;border:0;border-bottom:1px solid #e1e3d8}.skills-list button.active{background:#f5f1e8}.skills-list strong,.skills-list small{display:block}.skills-list small{font-size:11px;color:#68746b}.skills-detail{border:1px solid #e1e3d8;padding:15px}.skills-check{display:flex!important;align-items:center;gap:7px!important;margin:12px 0}.skills-actions{justify-content:flex-start;margin-top:16px}.skills-actions button{padding:8px 12px;border:1px solid #dcded4;border-radius:5px;background:#fff}.skills-notice{color:#2d775d}@media(max-width:720px){.skills-layout{grid-template-columns:1fr}}
</style>
