<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { apiRequest } from '../lib/api';

const router = useRouter();
const email = ref('');
const password = ref('');
const pending = ref(false);
const error = ref('');

async function signIn() {
  if (!email.value.trim() || !password.value || pending.value) return;
  pending.value = true; error.value = '';
  try {
    await apiRequest('/api/auth/sign-in/email', { method: 'POST', body: JSON.stringify({ email: email.value.trim(), password: password.value }) });
    await router.push('/');
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '登录失败'; }
  finally { pending.value = false; }
}
</script>

<template>
  <main class="auth-page"><section class="auth-panel"><div class="eyebrow">ALPHADECK</div><h1>登录研究中心</h1><p>使用平台分配的账号继续研究。</p><form @submit.prevent="signIn"><label for="email">邮箱</label><input id="email" v-model="email" type="email" autocomplete="username" required /><label for="password">密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" required /><p v-if="error" class="inline-error" role="alert">{{ error }}</p><button class="primary-button full-width" type="submit" :disabled="pending">{{ pending ? '正在登录…' : '登录 →' }}</button></form></section></main>
</template>
