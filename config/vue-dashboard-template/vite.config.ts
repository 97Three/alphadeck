import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import vue from '@vitejs/plugin-vue';

const dataFile = resolve(process.cwd(), 'data_file/final/dashboard-data.json');
const virtualId = '\0alphadeck:dashboard-data';

function dashboardData(): Plugin {
  return {
    name: 'alphadeck-dashboard-data',
    resolveId(id) { return id === 'virtual:dashboard-data' ? virtualId : undefined; },
    load(id) {
      if (id !== virtualId) return;
      try {
        const value = JSON.parse(readFileSync(dataFile, 'utf8'));
        return `export default ${JSON.stringify(value)};`;
      } catch { return 'export default {};'; }
    },
    configureServer(server) {
      if (existsSync(dataFile)) server.watcher.add(dataFile);
      server.watcher.on('change', file => {
        if (file !== dataFile) return;
        const module = server.moduleGraph.getModuleById(virtualId);
        if (module) { server.moduleGraph.invalidateModule(module); server.ws.send({ type: 'full-reload' }); }
      });
    },
  };
}

const marketPort = Number(process.env.ALPHADECK_SANDBOX_MARKET_PORT || 8000);
export default defineConfig({
  plugins: [vue(), dashboardData()],
  cacheDir: 'dist/.vite',
  server: {
    host: '127.0.0.1',
    watch: { usePolling: true, interval: 250 },
    proxy: { '/api/market': { target: `http://127.0.0.1:${marketPort}`, changeOrigin: true, rewrite: path => path.replace(/^\/api\/market/, '/api/v1') } },
  },
  preview: { host: '127.0.0.1' },
  build: { outDir: 'dist', sourcemap: false },
});
