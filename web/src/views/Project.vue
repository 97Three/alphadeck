<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { apiRequest, type ApiEnvelope } from '../lib/api';
import ProjectTools from '../components/ProjectTools.vue';
import RichText from '../components/RichText.vue';
import MessageFeedback from '../components/MessageFeedback.vue';
import { MODELS, modelLabel } from '../lib/models';
import { activeRequests, generation, messages, project, projects, submitResearch, uploadImage, type ChatMessage, type Generation, type Project } from '../lib/research';

const route = useRoute();
const router = useRouter();
const projectId = computed(() => String(route.params.projectId || ''));
const task = ref<Project | null>(null);
const taskList = ref<Project[]>([]);
const chat = ref<ChatMessage[]>([]);
const run = ref<Generation | null>(null);
const activeCount = ref(0);
const selectedTab = ref<'result' | 'process' | 'evidence' | 'files' | 'manage'>('result');
const tabs = [
  { id: 'result', label: '研究结果' },
  { id: 'process', label: '执行过程' },
  { id: 'evidence', label: '数据证据' },
  { id: 'files', label: '项目文件' },
  { id: 'manage', label: '项目管理' },
] as const;
const prompt = ref('');
const attachments = ref<File[]>([]);
const mode = ref<'act' | 'chat'>(route.query.mode === 'chat' ? 'chat' : 'act');
const hasAnswer = computed(() => chat.value.some(item => item.role === 'assistant' && item.metadata?.answerOnly === true));
const selectedModel = ref<string>(MODELS[0].id);
const sending = ref(false);
const pausing = ref(false);
const error = ref('');
const loading = ref(true);
const fileEntries = ref<Array<{ name: string; path: string; type?: string; isDirectory?: boolean }>>([]);
const currentDir = ref('.');
const selectedFile = ref('');
const fileContent = ref('');
const fileDirty = ref(false);
let interval: ReturnType<typeof setInterval> | undefined;
let stream: EventSource | undefined;
let refreshing = false;

const taskState = computed(() => {
  if (mode.value === 'chat' && activeCount.value > 0) return '回答中';
  if (mode.value === 'chat' && hasAnswer.value) return '已回答';
  if (run.value?.status === 'ready') return '已交付';
  if (run.value?.status === 'failed') return '需要处理';
  if (activeCount.value > 0 || run.value?.status === 'running') return '生成中';
  return task.value?.status || '待研究';
});
const previewUrl = computed(() => (run.value?.previewUrl || task.value?.previewUrl || '').replace(/^http:\/\/localhost:/, 'http://127.0.0.1:'));
const previewLoaded = ref(false);
watch(previewUrl, () => { previewLoaded.value = false; });
const currentStage = computed(() => mode.value === 'chat' && !previewUrl.value ? (activeCount.value ? '模型正在回答，结果会出现在下方研究对话。' : '问答结果在下方研究对话中。') : run.value?.stepSummary || run.value?.errorMessage || (activeCount.value ? '研究正在运行，完成后会更新看板。' : '等待新研究任务。'));
const latestUserQuestion = computed(() => [...chat.value].reverse().find(item => item.role === 'user')?.content || task.value?.initialPrompt || '');

async function refresh() {
  if (!projectId.value || refreshing) return;
  refreshing = true;
  try {
    const [projectResult, messagesResult, generationResult, activeResult] = await Promise.allSettled([
      project(projectId.value), messages(projectId.value), generation(projectId.value), activeRequests(projectId.value),
    ]);
    if (projectResult.status === 'fulfilled') {
      task.value = projectResult.value;
      if (projectResult.value.selectedModel) selectedModel.value = projectResult.value.selectedModel;
    } else if (!task.value) throw projectResult.reason;
    if (messagesResult.status === 'fulfilled') chat.value = messagesResult.value;
    if (generationResult.status === 'fulfilled') run.value = generationResult.value;
    if (activeResult.status === 'fulfilled') activeCount.value = activeResult.value.activeCount;
    error.value = '';
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取研究任务失败';
    if (error.value.includes('登录') || error.value.includes('AUTHENTICATION')) void router.push('/login');
  } finally { refreshing = false; loading.value = false; }
}
async function loadRail() {
  try { taskList.value = await projects(); } catch { /* 任务详情仍可单独打开。 */ }
}
function connectStream() {
  stream?.close();
  stream = new EventSource(`/api/chat/${encodeURIComponent(projectId.value)}/stream`);
  stream.onmessage = () => { void refresh(); };
  stream.onerror = () => { /* 定时同步继续运行，连接恢复交给 EventSource。 */ };
}
function initialize() {
  loading.value = true;
  task.value = null; chat.value = []; run.value = null; activeCount.value = 0;
  void refresh(); void loadRail();
  connectStream();
  if (interval) clearInterval(interval);
  interval = setInterval(() => { if (!document.hidden) void refresh(); }, 2500);
}
async function sendMessage() {
  const question = prompt.value.trim() || (attachments.value.length ? '请分析上传的图片材料' : '');
  if (!question || sending.value) return;
  sending.value = true; error.value = '';
  try {
    const uploaded = [];
    for (const file of attachments.value) uploaded.push(await uploadImage(projectId.value, file));
    await submitResearch(projectId.value, question, selectedModel.value, mode.value, false, undefined, uploaded);
    prompt.value = '';
    attachments.value = [];
    await refresh();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '提交失败'; }
  finally { sending.value = false; }
}
function addAttachments(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files || []).filter(file => ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type));
  attachments.value = [...attachments.value, ...files].slice(0, 8);
  input.value = '';
}
async function pause() {
  pausing.value = true;
  try { await apiRequest<ApiEnvelope<unknown>>(`/api/chat/${encodeURIComponent(projectId.value)}/pause`, { method: 'POST', body: '{}' }); await refresh(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '暂停失败'; }
  finally { pausing.value = false; }
}
async function startPreview() {
  try { await apiRequest(`/api/projects/${encodeURIComponent(projectId.value)}/preview/start`, { method: 'POST', body: '{}' }); await refresh(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '预览启动失败'; }
}
async function stopPreview() {
  if (!window.confirm('停止当前看板预览？')) return;
  try { await apiRequest(`/api/projects/${encodeURIComponent(projectId.value)}/preview/stop`, { method: 'POST', body: JSON.stringify({ intent: 'explicit-user-stop' }) }); await refresh(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '停止预览失败'; }
}
async function retryInitialization() {
  if (!window.confirm('重新提交最初的研究问题，创建一项新任务？')) return;
  try { await apiRequest(`/api/projects/${encodeURIComponent(projectId.value)}/retry-initialization`, { method: 'POST', body: JSON.stringify({ selectedModel: selectedModel.value }) }); await refresh(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '重试失败'; }
}
async function installDependencies() {
  try { await apiRequest(`/api/projects/${encodeURIComponent(projectId.value)}/install-dependencies`, { method: 'POST', body: '{}' }); await refresh(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '安装依赖失败'; }
}
async function loadFiles(dir = currentDir.value) {
  try {
    const payload = await apiRequest<ApiEnvelope<{ entries: typeof fileEntries.value }>>(`/api/projects/${encodeURIComponent(projectId.value)}/files?path=${encodeURIComponent(dir)}`);
    fileEntries.value = payload.data?.entries || [];
    currentDir.value = dir;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '文件读取失败'; }
}
function parentDir() { const parts = currentDir.value.split('/').filter(part => part && part !== '.'); parts.pop(); void loadFiles(parts.length ? parts.join('/') : '.'); }
async function openFile(path: string) {
  if (fileDirty.value && !window.confirm('放弃当前文件的未保存修改？')) return;
  try {
    const payload = await apiRequest<ApiEnvelope<{ content: string }>>(`/api/projects/${encodeURIComponent(projectId.value)}/files/content?path=${encodeURIComponent(path)}`);
    selectedFile.value = path;
    fileContent.value = payload.data?.content || '';
    fileDirty.value = false;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '文件打开失败'; }
}
async function saveFile() {
  if (!selectedFile.value) return;
  try {
    await apiRequest(`/api/projects/${encodeURIComponent(projectId.value)}/files/content`, { method: 'PUT', body: JSON.stringify({ path: selectedFile.value, content: fileContent.value }) });
    fileDirty.value = false;
    await refresh();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '文件保存失败'; }
}
watch(projectId, initialize);
watch(selectedTab, value => { if (value === 'files') void loadFiles(); });
onMounted(initialize);
onBeforeUnmount(() => { stream?.close(); if (interval) clearInterval(interval); });
</script>

<template>
  <div class="research-layout project-layout">
    <aside class="task-rail">
      <div class="rail-heading"><strong>研究任务</strong><span>最近</span></div>
      <RouterLink to="/" class="rail-create">＋ 发起一项研究</RouterLink>
      <div class="rail-caption">任务轨道</div>
      <RouterLink v-for="item in taskList" :key="item.id" :to="`/${item.id}/chat`" :class="['rail-task-link', 'project-rail-link', { active: item.id === projectId }]"><strong>{{ item.name }}</strong><small>{{ item.status || '研究任务' }}</small></RouterLink>
    </aside>
    <main class="workspace-main project-main">
      <div class="breadcrumb"><RouterLink to="/">研究中心</RouterLink>　/　任务　/　{{ task?.name || projectId }}</div>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <div v-if="loading" class="loading-state">正在读取研究任务…</div>
      <template v-else>
        <div class="project-heading"><div><div class="eyebrow">MISSION · {{ projectId }}</div><h1>{{ task?.name || '研究任务' }}</h1><p>{{ latestUserQuestion || '查看研究过程、数据和看板成果' }}</p></div><span class="status-pill">{{ taskState }}</span></div>
        <div class="project-quick-actions"><button v-if="previewUrl" class="secondary-button" type="button" @click="stopPreview">停止预览</button><button v-if="task?.status === 'failed' || run?.status === 'failed'" class="secondary-button" type="button" @click="retryInitialization">重新开始</button><button class="secondary-button" type="button" @click="installDependencies">安装页面依赖</button></div>
        <div class="workspace-tabs" role="tablist" aria-label="任务详情"><button v-for="tab in tabs" :key="tab.id" type="button" role="tab" :aria-selected="selectedTab === tab.id" :class="{ active: selectedTab === tab.id }" @click="selectedTab = tab.id">{{ tab.label }}</button></div>
        <div v-if="selectedTab === 'result'" class="result-grid"><section class="paper-panel result-preview"><div class="panel-heading"><strong>{{ mode === 'chat' && !previewUrl ? '分析问答' : '研究看板' }}</strong><span>{{ mode === 'chat' && !previewUrl ? '无需看板验收' : run?.validationStatus === 'passed' ? '验收通过' : '等待验收' }}</span></div><template v-if="previewUrl"><div class="preview-frame-head"><div><strong>{{ previewLoaded ? '看板预览' : '正在载入预览' }}</strong><p>{{ currentStage }}</p></div><a :href="previewUrl" target="_blank" rel="noopener noreferrer">独立打开 ↗</a></div><div class="preview-frame-shell"><div v-if="!previewLoaded" class="preview-frame-loading"><span class="preview-symbol">◫</span><strong>看板正在载入</strong><p>可先在下方查看研究对话与运行依据。</p></div><iframe :src="previewUrl" title="生成的研究看板" referrerpolicy="no-referrer" @load="previewLoaded = true" /></div></template><div v-else class="preview-empty"><span class="preview-symbol">◫</span><strong>{{ mode === 'chat' ? (hasAnswer ? '问答已完成' : '正在准备回答') : taskState === '需要处理' ? '这次生成没有完成' : '看板正在准备' }}</strong><p>{{ currentStage }}</p><div v-if="mode !== 'chat'" class="preview-path"><span>01 · 取数</span><span>02 · 分析</span><span>03 · 生成</span><span>04 · 验收</span></div><button v-if="run?.status === 'preview_pending'" type="button" class="secondary-button" @click="startPreview">启动预览</button></div></section><section class="paper-panel delivery-panel"><div class="panel-heading"><strong>交付档案</strong><span>与任务同步</span></div><dl><div><dt>输出方式</dt><dd>{{ mode === 'act' ? '生成看板' : '分析问答' }}</dd></div><div><dt>分析模型</dt><dd>{{ modelLabel(selectedModel) }}</dd></div><div><dt>当前阶段</dt><dd>{{ run?.stepSummary || taskState }}</dd></div><div><dt>验收状态</dt><dd>{{ mode === 'chat' && !previewUrl ? '无需看板验收' : run?.validationStatus || '待验收' }}</dd></div><div><dt>验收回执</dt><dd>{{ mode === 'chat' && !previewUrl ? '不适用' : run?.acceptedReceiptId || '待生成' }}</dd></div></dl><button v-if="activeCount > 0" class="secondary-button full-width" type="button" :disabled="pausing" @click="pause">{{ pausing ? '正在暂停…' : '暂停当前任务' }}</button><a v-if="previewUrl" class="primary-button full-width" :href="previewUrl" target="_blank" rel="noopener noreferrer">在新窗口打开看板 ↗</a></section></div>
        <section v-else-if="selectedTab === 'process'" class="paper-panel process-panel"><div class="panel-heading"><strong>执行过程</strong><span>{{ taskState }}</span></div><ol class="process-steps"><li :class="{ done: !!run?.requestId }">理解研究问题</li><li :class="{ done: ['ready', 'preview_pending'].includes(run?.status || '') }">准备行情与数据</li><li :class="{ done: ['ready', 'preview_pending'].includes(run?.status || '') }">分析与页面生成</li><li :class="{ done: run?.status === 'ready' }">浏览器验收与交付</li></ol><p class="process-note">{{ run?.stepSummary || run?.errorMessage || (activeCount ? '研究任务正在运行。' : '当前没有运行中的研究任务。') }}</p></section>
        <section v-else-if="selectedTab === 'evidence'" class="paper-panel evidence-panel"><div class="panel-heading"><strong>数据与验收证据</strong><span>可追溯</span></div><dl><div><dt>任务 ID</dt><dd>{{ run?.requestId || '待创建' }}</dd></div><div><dt>验收检查</dt><dd>{{ run?.validationStatus || '待运行' }}</dd></div><div><dt>预览状态</dt><dd>{{ run?.previewStatus || '未启动' }}</dd></div><div><dt>交付回执</dt><dd>{{ run?.acceptedReceiptId || '未生成' }}</dd></div></dl><p>详细证据和报告保存在任务工作空间中，可从“项目文件”查看。</p></section>
        <section v-else-if="selectedTab === 'files'" class="paper-panel files-panel"><div class="panel-heading"><strong>项目文件 · {{ currentDir }}</strong><span>{{ fileEntries.length }} 项</span></div><div class="file-workspace"><div class="file-list"><button v-if="currentDir !== '.'" type="button" @click="parentDir">← 返回上级</button><button v-for="entry in fileEntries" :key="entry.path" type="button" @click="entry.isDirectory || entry.type === 'directory' ? loadFiles(entry.path) : openFile(entry.path)">{{ entry.isDirectory || entry.type === 'directory' ? '▸' : '·' }} {{ entry.name }}</button></div><div class="file-editor"><div class="file-editor-head"><span>{{ selectedFile || '选择一个文件' }}</span><button v-if="selectedFile" type="button" :disabled="!fileDirty" @click="saveFile">保存修改</button></div><textarea v-if="selectedFile" v-model="fileContent" spellcheck="false" aria-label="文件内容" @input="fileDirty = true" /><p v-else>选择左侧文件查看内容。</p></div></div></section>
        <ProjectTools v-else :project-id="projectId" />
        <div class="conversation-heading"><h2>研究对话</h2><span>{{ chat.length }} 条消息 · 操作审批在「项目管理」</span></div><div class="conversation-list"><div v-if="chat.length === 0" class="conversation-empty"><strong>研究过程会从这里展开</strong><p>提交问题后，可以在这里看到分析回复、工具调用和本轮依据。需要批准的操作可在「项目管理」处理。</p><button type="button" class="secondary-button" @click="selectedTab = 'manage'">查看项目管理</button></div><article v-for="message in chat" :key="message.id" :class="['message', `message-${message.role}`]"><div class="message-meta"><strong>{{ message.role === 'user' ? '你' : message.messageType === 'tool_result' || message.role === 'tool' ? '工具结果' : message.role === 'assistant' ? '研究助手' : '系统' }}</strong><span>{{ message.createdAt ? new Date(message.createdAt).toLocaleString('zh-CN') : '' }}</span></div><details v-if="message.messageType === 'tool_result' || message.role === 'tool'" class="tool-message"><summary>{{ message.metadata?.name || message.metadata?.toolName || '查看工具返回内容' }}</summary><pre>{{ message.content }}</pre></details><RichText v-else class="message-content" :content="message.content" /><MessageFeedback v-if="message.requestId && ((message.metadata?.isPiAgentFinal === true && message.metadata?.validationPassed === true) || message.metadata?.personalizationCandidate)" :project-id="projectId" :request-id="message.requestId" :metadata="message.metadata" /></article></div>
        <div class="followup-composer"><label for="followup-question">继续这项研究</label><textarea id="followup-question" v-model="prompt" rows="3" placeholder="继续提问，或补充你希望看板展示的内容" @keydown.ctrl.enter="sendMessage" /><div class="attachment-row"><label class="secondary-button" for="followup-images">＋ 添加图片材料</label><input id="followup-images" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden @change="addAttachments" /><span v-if="attachments.length">{{ attachments.length }} 张，最多 8 张</span><button v-for="(file, index) in attachments" :key="`${file.name}-${index}`" type="button" @click="attachments.splice(index, 1)">{{ file.name }} ×</button></div><div class="composer-controls"><div class="field-group"><label for="followup-mode">输出方式</label><select id="followup-mode" v-model="mode"><option value="act">更新看板</option><option value="chat">只做问答</option></select></div><div class="field-group model-field"><label for="followup-model">分析模型</label><select id="followup-model" v-model="selectedModel"><option v-for="item in MODELS" :key="item.id" :value="item.id">{{ item.label }}</option></select></div><button class="primary-button" type="button" :disabled="(!prompt.trim() && !attachments.length) || sending" @click="sendMessage">{{ sending ? '提交中…' : '发送问题 →' }}</button></div></div>
      </template>
    </main>
  </div>
</template>
