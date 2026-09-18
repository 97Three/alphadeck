<script setup lang="ts">
import { computed } from 'vue';

type Block = { kind: 'heading' | 'paragraph' | 'list' | 'quote' | 'code'; text: string; level?: number; items?: string[] };
const props = defineProps<{ content: string }>();

function parse(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let paragraph: string[] = [];
  let list: string[] = [];
  let code: string[] | null = null;
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: 'paragraph', text: paragraph.join(' ') });
    if (list.length) blocks.push({ kind: 'list', text: '', items: list });
    paragraph = []; list = [];
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith('```')) {
      flush();
      if (code) { blocks.push({ kind: 'code', text: code.join('\n') }); code = null; }
      else code = [];
      continue;
    }
    if (code) { code.push(raw); continue; }
    if (!line) { flush(); continue; }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) { flush(); blocks.push({ kind: 'heading', text: heading[2], level: heading[1].length }); continue; }
    const bullet = /^(?:[-*+]\s+|\d+[.)]\s+)(.+)$/.exec(line);
    if (bullet) { if (paragraph.length) flush(); list.push(bullet[1]); continue; }
    if (line.startsWith('> ')) { flush(); blocks.push({ kind: 'quote', text: line.slice(2) }); continue; }
    if (list.length) flush();
    paragraph.push(line);
  }
  flush();
  if (code) blocks.push({ kind: 'code', text: code.join('\n') });
  return blocks;
}

const blocks = computed(() => parse(props.content));
function inline(text: string): string {
  // Escape before adding the small, supported Markdown subset. No source HTML is rendered.
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}
</script>

<template>
  <div class="rich-text">
    <template v-for="(block, index) in blocks" :key="index">
      <h2 v-if="block.kind === 'heading' && block.level === 1" v-html="inline(block.text)" />
      <h3 v-else-if="block.kind === 'heading'" v-html="inline(block.text)" />
      <p v-else-if="block.kind === 'paragraph'" v-html="inline(block.text)" />
      <blockquote v-else-if="block.kind === 'quote'" v-html="inline(block.text)" />
      <pre v-else-if="block.kind === 'code'"><code>{{ block.text }}</code></pre>
      <ul v-else><li v-for="(item, itemIndex) in block.items" :key="itemIndex" v-html="inline(item)" /></ul>
    </template>
  </div>
</template>

<style scoped>
.rich-text{font-size:13px;line-height:1.78;overflow-wrap:anywhere}.rich-text :deep(p){margin:0 0 10px}.rich-text :deep(p:last-child){margin-bottom:0}.rich-text :deep(h2){font-size:18px;margin:18px 0 8px}.rich-text :deep(h3){font-size:15px;margin:16px 0 7px}.rich-text :deep(ul){margin:6px 0 12px;padding-left:21px}.rich-text :deep(li){margin:5px 0}.rich-text :deep(code){font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:#eeeae0;padding:1px 4px;border-radius:3px}.rich-text :deep(pre){overflow:auto;background:#252e2a;color:#f3f0e8;border-radius:7px;padding:13px 15px;margin:12px 0}.rich-text :deep(pre code){background:transparent;color:inherit;padding:0}.rich-text :deep(blockquote){border-left:3px solid #ad7730;padding:7px 12px;margin:10px 0;background:#f4f1e8;color:#536057}
</style>
