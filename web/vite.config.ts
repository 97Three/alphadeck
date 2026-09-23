import { fileURLToPath, URL } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('..', import.meta.url)), '');
  const platformUrl = env.ALPHADECK_PLATFORM_API_URL || 'http://127.0.0.1:38084';
  return {
    plugins: [vue()],
    resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    server: {
      host: '127.0.0.1',
      port: Number(env.WEB_PORT || 3200),
      watch: { usePolling: true, interval: 250 },
      proxy: {
        '/api': { target: platformUrl, changeOrigin: false, xfwd: true, ws: true, timeout: 3_600_000, proxyTimeout: 3_600_000 },
        '/internal': { target: platformUrl, changeOrigin: false, xfwd: true },
      },
    },
    build: { outDir: 'dist', sourcemap: false },
  };
});
