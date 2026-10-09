#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');

const root = path.resolve(__dirname, '../..');
const web = path.join(root, 'web');
const production = process.argv.includes('--production');
const originalEnv = { ...process.env };

function parseEnv(file) {
  if (!fs.existsSync(file)) return {};
  const data = {};
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*$/);
    if (!match) continue;
    let value = match[2];
    if (value.length > 1 && (value[0] === '"' || value[0] === "'") && value.at(-1) === value[0]) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, '');
    }
    data[match[1]] = value;
  }
  return data;
}

const localEnvFile = path.join(root, '.env.local');
const env = {
  ...parseEnv(path.join(root, '.env')),
  ...parseEnv(localEnvFile),
  ...originalEnv,
};
if (!/^[0-9a-f]{64}$/i.test(env.ENCRYPTION_KEY || '')) {
  const key = crypto.randomBytes(32).toString('hex');
  fs.appendFileSync(localEnvFile, `\nENCRYPTION_KEY="${key}"\n`);
  env.ENCRYPTION_KEY = key;
}
env.NO_PROXY = [env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(',');
env.no_proxy = env.NO_PROXY;
const platformUrl = new URL(env.ALPHADECK_PLATFORM_API_URL || 'http://127.0.0.1:38084');
const marketUrl = new URL(env.ALPHADECK_MARKET_API_URL || 'http://127.0.0.1:8103');
const webPort = Number(env.WEB_PORT || 3200);
const children = [];
let stopping = false;

function launch(label, command, args, cwd = root, extraEnv = {}) {
  const child = spawn(command, args, {
    cwd, env: { ...env, ...extraEnv }, stdio: 'inherit',
    detached: process.platform !== 'win32', shell: process.platform === 'win32',
  });
  child.on('error', error => console.error(`${label} 启动失败：${error.message}`));
  child.on('exit', code => {
    if (!stopping && code !== 0) {
      console.error(`${label} 已退出（${code ?? 'unknown'}）`);
      stop(code || 1);
    }
  });
  children.push(child);
  return child;
}
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children.reverse()) {
    if (!child.pid || child.exitCode !== null) continue;
    try { process.platform === 'win32' ? child.kill('SIGTERM') : process.kill(-child.pid, 'SIGTERM'); }
    catch { try { child.kill('SIGTERM'); } catch { /* 已停止 */ } }
  }
  setTimeout(() => process.exit(code), 350).unref();
}
async function healthy(url, pathName) {
  try {
    const response = await fetch(new URL(pathName, url), { signal: AbortSignal.timeout(1200) });
    return response.ok;
  } catch { return false; }
}
async function waitFor(label, url, pathName, child, attempts = 60) {
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (await healthy(url, pathName)) return;
    if (child?.exitCode !== null && child?.exitCode !== undefined) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`${label} 未就绪：${new URL(pathName, url)}`);
}

async function main() {
  if (process.platform === 'win32') console.log('建议在 WSL 中运行，以支持生成看板的隔离预览。');
  if (env.ALPHADECK_MARKET_API_ENABLED !== '0' && !(await healthy(marketUrl, '/health'))) {
    const marketPython = path.join(root, 'services', 'market-data', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const installed = process.platform !== 'win32' && fs.existsSync(marketPython) &&
      ['uvicorn', 'fastapi', 'psycopg', 'baostock', 'akshare'].every(name =>
        fs.existsSync(path.join(root, 'services', 'market-data', '.venv', 'lib', 'python3.14', 'site-packages', name)));
    const market = installed
      ? launch('行情服务', marketPython, ['-m', 'uvicorn', 'alphadeck_market_data.api:app', '--host', marketUrl.hostname, '--port', marketUrl.port], root, {
        PYTHONPATH: [path.join(root, 'services', 'market-data', 'src'), env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        ALPHADECK_MARKET_HOST: marketUrl.hostname,
        ALPHADECK_MARKET_PORT: marketUrl.port,
      })
      : launch('行情服务', 'uv', ['run', '--directory', 'services/market-data', '--extra', 'baostock', '--extra', 'akshare', 'alphadeck-market-api'], root, {
      ALPHADECK_MARKET_HOST: marketUrl.hostname,
      ALPHADECK_MARKET_PORT: marketUrl.port,
    });
    await waitFor('行情服务', marketUrl, '/health', market, installed ? 120 : 600);
  }
  if (!(await healthy(platformUrl, '/api/health'))) {
    const platform = launch('Python 平台 API', 'uv', ['run', '--directory', 'services/platform-api', '--locked', 'uvicorn', 'app:app', '--host', platformUrl.hostname, '--port', platformUrl.port]);
    await waitFor('Python 平台 API', platformUrl, '/api/health', platform);
  }
  env.PI_AGENT_DISPATCH_MODE = 'worker';
  launch('生成 Worker', 'uv', ['run', '--directory', 'services/platform-api', '--locked', 'python', '-m', 'alphadeck_platform.worker_cli']);
  launch('评测 Worker', 'uv', ['run', '--directory', 'services/platform-api', '--locked', 'python', '-m', 'alphadeck_platform.evaluation_worker']);
  if (!production) {
    const vite = path.join(web, 'node_modules', 'vite', 'bin', 'vite.js');
    if (!fs.existsSync(vite)) throw new Error('缺少 Vue 依赖；先在 web 目录运行 npm ci。');
    launch('Vue 工作台', process.execPath, [vite, '--host', '127.0.0.1', '--port', String(webPort), '--strictPort'], web);
    console.log(`Vue 工作台：http://127.0.0.1:${webPort}`);
  } else {
    if (!fs.existsSync(path.join(web, 'dist', 'index.html'))) throw new Error('缺少 Vue 构建；先运行 npm run build:vue。');
    console.log(`Vue 工作台由 Python API 提供：http://${platformUrl.host}`);
  }
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));
main().catch(error => { console.error(error.message); stop(1); });
