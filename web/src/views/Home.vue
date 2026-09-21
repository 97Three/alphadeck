<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { apiRequest, type ApiEnvelope } from '../lib/api';
import { MODELS, modelReadiness, type ModelStatus } from '../lib/models';
import { createProject, modelStatus, projects, submitResearch, uploadImage, type Project } from '../lib/research';

const router = useRouter();
const route = useRoute();
const taskList = ref<Project[]>([]);
const loading = ref(true);
const listError = ref('');
const question = ref(typeof route.query.question === 'string' ? route.query.question : '');
const mode = ref<'act' | 'chat'>('act');
const selectedModel = ref<string>(MODELS[0].id);
const selectedCapability = ref(typeof route.query.capabilityId === 'string' ? route.query.capabilityId : 'stock_diagnosis');
const capabilities = ref<Array<{ id: string; name: string; description: string; status: string }>>([]);
const images = ref<File[]>([]);
const status = ref<ModelStatus | null>(null);
const modelError = computed(() => modelReadiness(status.value, selectedModel.value));
const submitting = ref(false);
const submitStep = ref('');
const submitError = ref('');
const pendingProjectId = ref('');
const recent = computed(() => [...taskList.value].sort((a, b) => Date.parse(b.updatedAt || b.createdAt || '') - Date.parse(a.updatedAt || a.createdAt || '')));
const runningTasks = computed(() => taskList.value.filter(item => ['running', 'building', 'initializing', 'preview_running'].includes(item.status || '')).length);
const readyTasks = computed(() => taskList.value.filter(item => Boolean(item.previewUrl) || item.status === 'active').length);
const suggestedQuestions = computed(() => [
  { id: 'stock_diagnosis', label: '从一只股票开始', question: '分析贵州茅台近一年的走势、波动和主要风险，给出可复核的数据依据。', hint: '适合先看行情与风险' },
  { id: 'asset_comparison', label: '比较两只股票', question: '比较贵州茅台与五粮液近一年的走势、回撤和估值差异，说明各自风险。', hint: '适合练习横向研究' },
  { id: 'portfolio_risk', label: '检查一个组合', question: '分析一个包含贵州茅台和五粮液的组合，关注集中度、相关性与回撤风险。', hint: '适合看组合风险' },
].filter(item => capabilities.value.some(capability => capability.id === item.id)));
function useSuggestion(item: { id: string; question: string }) { selectedCapability.value = item.id; question.value = item.question; focusQuestion(); }

async function loadProjects() {
  loading.value = true;
  try { taskList.value = await projects(); listError.value = ''; }
  catch (error) { listError.value = error instanceof Error ? error.message : '项目读取失败'; }
  finally { loading.value = false; }
}
async function checkModel() {
  try { status.value = await modelStatus(); }
  catch (error) { status.value = { pi: { error: error instanceof Error ? error.message : '模型检查失败' } }; }
}
async function startResearch() {
  const prompt = question.value.trim() || (images.value.length ? '请分析上传的图片材料' : '');
  if (!prompt || submitting.value) return;
  submitError.value = '';
  submitting.value = true;
  try {
    submitStep.value = '检查所选模型';
    await checkModel();
    if (modelError.value) throw new Error(modelError.value);
    submitStep.value = '创建研究空间';
    const created = pendingProjectId.value ? { id: pendingProjectId.value } : await createProject(prompt, selectedModel.value, selectedCapability.value);
    pendingProjectId.value = created.id;
    const uploaded = [];
    for (const [index, file] of images.value.entries()) {
      submitStep.value = `上传研究材料（${index + 1}/${images.value.length}）`;
      uploaded.push(await uploadImage(created.id, file));
    }
    submitStep.value = mode.value === 'act' ? '启动看板研究' : '启动分析问答';
    await submitResearch(created.id, prompt, selectedModel.value, mode.value, true, selectedCapability.value, uploaded);
    pendingProjectId.value = '';
    images.value = [];
    await router.push({ path: `/${created.id}/chat`, query: { mode: mode.value } });
  } catch (error) {
    submitError.value = error instanceof Error ? error.message : '研究任务启动失败';
    await loadProjects();
  } finally { submitting.value = false; submitStep.value = ''; }
}
async function renameTask(task: Project) {
  const name = window.prompt('修改研究名称', task.name)?.trim();
  if (!name || name === task.name) return;
  try {
    await apiRequest<ApiEnvelope<Project>>(`/api/projects/${encodeURIComponent(task.id)}`, { method: 'PUT', body: JSON.stringify({ name }) });
    await loadProjects();
  } catch (error) { listError.value = error instanceof Error ? error.message : '修改失败'; }
}
async function deleteTask(task: Project) {
  if (!window.confirm(`删除研究“${task.name}”？此操作会删除该项目的工作空间。`)) return;
  try {
    await apiRequest(`/api/projects/${encodeURIComponent(task.id)}`, { method: 'DELETE' });
    await loadProjects();
  } catch (error) { listError.value = error instanceof Error ? error.message : '删除失败'; }
}
function taskStatus(task: Project): string {
  if (task.previewUrl || task.status === 'active' || task.status === 'preview_running') return '已交付';
  if (task.status === 'failed' || task.status === 'error') return '需要处理';
  if (['running', 'building', 'initializing'].includes(task.status || '')) return '进行中';
  return '草稿';
}
function focusQuestion() { document.getElementById('research-question')?.focus(); }
function addImages(event: Event) {
  const input = event.target as HTMLInputElement;
  const incoming = Array.from(input.files || []).filter(file => ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type));
  images.value = [...images.value, ...incoming].slice(0, 8);
  input.value = '';
}
async function loadCapabilities() {
  try {
    const payload = await apiRequest<ApiEnvelope<{ capabilities: typeof capabilities.value }>>('/api/quant/capabilities');
    capabilities.value = (payload.data?.capabilities || []).filter(item => item.status === 'ready');
    if (!capabilities.value.some(item => item.id === selectedCapability.value)) selectedCapability.value = capabilities.value[0]?.id || 'stock_diagnosis';
  } catch { capabilities.value = [{ id: 'stock_diagnosis', name: '个股诊断', description: '行情、财务与公告', status: 'ready' }]; }
}
async function loadDefaultModel() {
  try {
    const settings = await apiRequest<{ cliSettings?: { pi?: { model?: string } } }>('/api/settings/global');
    const model = settings.cliSettings?.pi?.model;
    if (model && MODELS.some(item => item.id === model)) selectedModel.value = model;
  } catch { /* The page still works when settings are unavailable. */ }
}
onMounted(() => { void loadProjects(); void loadDefaultModel(); void loadCapabilities(); void checkModel(); });
</script>

<template>
  <div class="research-layout home-layout">
    <aside class="task-rail">
      <div class="rail-heading"><strong>研究任务</strong><span>{{ taskList.length }} 项</span></div>
      <button class="rail-create" type="button" @click="focusQuestion">＋ 发起一项研究</button>
      <div class="rail-caption">任务轨道</div>
      <p v-if="loading" class="rail-note">正在读取任务…</p>
      <p v-else-if="listError" class="rail-note error-text">{{ listError }}</p>
      <p v-else-if="recent.length === 0" class="rail-note">研究任务会出现在这里。</p>
      <div v-for="task in recent" :key="task.id" class="rail-task-row">
        <RouterLink :to="`/${task.id}/chat`" class="rail-task-link"><strong>{{ task.name }}</strong><small>{{ taskStatus(task) }} · {{ task.updatedAt ? new Date(task.updatedAt).toLocaleDateString('zh-CN') : '刚刚' }}</small></RouterLink>
        <div class="rail-task-actions"><button type="button" :aria-label="`重命名 ${task.name}`" @click="renameTask(task)">改名</button><button type="button" :aria-label="`删除 ${task.name}`" @click="deleteTask(task)">删除</button></div>
      </div>
    </aside>
    <main class="workspace-main home-main">
      <div class="breadcrumb">研究中心　/　工作台</div>
      <section class="welcome-block"><div class="eyebrow">RESEARCH CENTER</div><h1>从一个问题，开始一项研究。</h1><p>提出问题，选择模型和输出方式；研究进度、数据与结果会归入同一项任务。</p></section>
      <section class="composer-card" aria-label="发起研究">
        <label for="research-question" class="field-label">你想研究什么？</label>
        <textarea id="research-question" v-model="question" rows="4" placeholder="例如：比较贵州茅台与五粮液近一年的走势、波动和估值，并生成研究看板" :disabled="submitting" @keydown.ctrl.enter="startResearch" />
        <div class="attachment-row"><label class="secondary-button" for="research-images">＋ 添加图片材料</label><input id="research-images" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden @change="addImages" /><span v-if="images.length">{{ images.length }} 张，最多 8 张</span><button v-for="(file, index) in images" :key="`${file.name}-${index}`" type="button" @click="images.splice(index, 1)">{{ file.name }} ×</button></div>
        <div class="composer-controls">
          <div class="field-group"><label for="capability-select">研究方向</label><select id="capability-select" v-model="selectedCapability" :disabled="submitting"><option v-for="item in capabilities" :key="item.id" :value="item.id">{{ item.name }}</option></select></div>
          <div class="field-group"><label for="output-mode">输出方式</label><select id="output-mode" v-model="mode" :disabled="submitting"><option value="act">生成看板</option><option value="chat">只做问答</option></select></div>
          <div class="field-group model-field"><label for="model-select">分析模型</label><select id="model-select" v-model="selectedModel" :disabled="submitting"><option v-for="model in MODELS" :key="model.id" :value="model.id">{{ model.label }}</option></select></div>
          <button class="primary-button" type="button" :disabled="(!question.trim() && !images.length) || submitting" @click="startResearch">{{ submitting ? submitStep + '…' : '开始研究 →' }}</button>
        </div>
        <p class="capability-help">{{ capabilities.find(item => item.id === selectedCapability)?.description || '选择与问题最接近的研究方向。' }}</p>
        <div class="model-readiness" :class="{ 'error-text': modelError }"><span>{{ modelError || '当前模型已通过连接检查' }}</span><button type="button" @click="checkModel">重新检查模型</button><RouterLink to="/settings">模型设置</RouterLink></div>
        <p v-if="submitError" class="inline-error" role="alert">{{ submitError }}。问题已保留，可直接重试。</p>
      </section>
      <section class="home-guide" aria-label="研究起步"><div class="home-guide-heading"><div><div class="eyebrow">START HERE</div><h2>选一个入口，先跑通一项研究</h2><p>示例只帮你起步，问题可以随时改。策略数据尚未补齐时，先从个股诊断开始。</p></div><div class="home-pulse"><strong>{{ taskList.length }}</strong><span>研究任务</span><strong>{{ runningTasks }}</strong><span>进行中</span><strong>{{ readyTasks }}</strong><span>有看板</span></div></div><div class="home-starters"><button v-for="(item,index) in suggestedQuestions" :key="item.id" type="button" @click="useSuggestion(item)"><span>0{{ index + 1 }} / {{ item.hint }}</span><strong>{{ item.label }}</strong><p>{{ item.question }}</p><em>填入问题 →</em></button><div v-if="!suggestedQuestions.length" class="home-starter-empty">正在读取可用的研究方向。你也可以直接在上方输入自己的问题。</div></div></section>
      <div class="section-heading"><h2>最近的研究</h2><span>按最近更新排序</span></div>
      <div v-if="recent.length === 0" class="home-first-step"><div><span>01</span><strong>提出问题</strong><p>先选择一个研究方向，写清股票、时间范围和想看的指标。</p></div><div><span>02</span><strong>确认数据</strong><p>打开策略实验室检查行情覆盖；缺数时再补齐股票池。</p></div><div><span>03</span><strong>查看交付</strong><p>研究任务会保留对话、生成过程、看板和验收记录。</p></div></div>
      <div v-else class="recent-grid"><RouterLink v-for="task in recent.slice(0, 4)" :key="task.id" :to="`/${task.id}/chat`" class="recent-card"><div class="recent-card-top"><span>{{ taskStatus(task) }}</span><span>↗</span></div><strong>{{ task.name }}</strong><small>{{ task.initialPrompt || '打开研究任务，查看进度与成果' }}</small></RouterLink></div>
    </main>
  </div>
</template>
