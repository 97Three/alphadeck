<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute } from 'vue-router';

const route = useRoute();
const navigation = [
  { to: '/', label: '研究' },
  { to: '/strategy-platform', label: '策略' },
  { to: '/research-reports', label: '报告' },
  { to: '/business-knowledge', label: '知识' },
  { to: '/skills', label: '技能' },
  { to: '/eval-platform', label: '评测' },
  { to: '/ops-platform', label: '运行' },
  { to: '/settings', label: '设置' },
];
const menuOpen = ref(false);
const isResearch = computed(() => route.path === '/' || route.path.endsWith('/chat'));
</script>

<template>
  <div class="app-shell">
    <header class="global-header">
      <RouterLink to="/" class="brand" aria-label="AlphaDeck 研究中心首页">Quant<span>Pilot</span><small>研究中心</small></RouterLink>
      <nav :class="['global-nav', { 'is-open': menuOpen }]" aria-label="产品导航">
        <RouterLink v-for="item in navigation" :key="item.to" :to="item.to" :class="{ active: isResearch && item.to === '/' || (!isResearch && route.path.startsWith(item.to) && item.to !== '/') }" @click="menuOpen = false">{{ item.label }}</RouterLink>
      </nav>
      <div class="header-actions">
        <RouterLink to="/account/usage" class="account-link" aria-label="我的账号">我的账号</RouterLink>
        <button class="mobile-menu" type="button" :aria-expanded="menuOpen" aria-label="切换导航" @click="menuOpen = !menuOpen">☰</button>
      </div>
    </header>
    <RouterView />
  </div>
</template>
