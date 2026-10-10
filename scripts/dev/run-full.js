#!/usr/bin/env node

const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const { loadProjectEnvironment } = require('../shared/load-env');
const { parseCliArgs, startWebDevServer } = require('./run-web');
const { ensureEnvironment } = require('./setup-env');

const rootDir = path.join(__dirname, '..', '..');
const marketDataDir = path.join(rootDir, 'services', 'market-data');
const isWindows = os.platform() === 'win32';

loadProjectEnvironment({ rootDir });

function envFlag(name, fallback) {
  const value = process.env[name]?.trim().toLowerCase();
  if (!value) return fallback;
  if (['0', 'false', 'no', 'off', 'disabled'].includes(value)) return false;
  if (['1', 'true', 'yes', 'on', 'enabled'].includes(value)) return true;
  return fallback;
}

function shouldRunMarketApi() {
  return (
    process.env.ALPHADECK_DEGRADATION_MODE?.trim().toLowerCase() !== 'offline' &&
    envFlag('ALPHADECK_MARKET_API_ENABLED', true)
  );
}

function shouldRunGenerationWorker() {
  return (
    envFlag('ALPHADECK_DEV_MANAGE_GENERATION_WORKER', true)
  );
}

function marketApiUrl() {
  return (
    process.env.ALPHADECK_MARKET_API_URL ||
    process.env.ALPHADECK_MARKET_API_BASE_URL ||
    `http://127.0.0.1:${process.env.ALPHADECK_MARKET_PORT || '8100'}`
  ).replace(/\/$/, '');
}

async function probeHealth(url, timeoutMs = 1200, healthPath = '/health') {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${url}${healthPath}`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function stopChild(child) {
  if (!child?.pid) return;
  try {
    if (isWindows) {
      child.kill('SIGTERM');
    } else {
      process.kill(-child.pid, 'SIGTERM');
    }
  } catch {
    try {
      child.kill('SIGTERM');
    } catch {
      // 子进程已经退出。
    }
  }
}

async function startMarketApiIfNeeded() {
  if (!shouldRunMarketApi()) {
    console.log('↪️  Market API disabled by degradation configuration.');
    return { child: null, managed: false };
  }

  const url = marketApiUrl();
  if (await probeHealth(url)) {
    process.env.ALPHADECK_MARKET_API_ENABLED = '1';
    console.log(`✅ Market API already healthy at ${url}`);
    return { child: null, managed: false };
  }

  const parsedUrl = new URL(url);
  const host = parsedUrl.hostname || '127.0.0.1';
  const port = parsedUrl.port || '8100';
  const noProxy = [process.env.NO_PROXY, process.env.no_proxy, '127.0.0.1', 'localhost']
    .filter(Boolean)
    .join(',');

  console.log(`🚀 Starting market-data service on ${url}`);
  const child = spawn(
    'uv',
    ['run', '--extra', 'baostock', '--extra', 'akshare', 'alphadeck-market-api'],
    {
      cwd: marketDataDir,
      stdio: 'inherit',
      shell: isWindows,
      detached: !isWindows,
      env: {
        ...process.env,
        NO_PROXY: noProxy,
        no_proxy: noProxy,
        ALPHADECK_MARKET_HOST: host,
        ALPHADECK_MARKET_PORT: port,
      },
    }
  );

  child.once('error', (error) => {
    console.error(`❌ Failed to start market-data: ${error.message}`);
  });

  for (let attempt = 1; attempt <= 40; attempt += 1) {
    if (await probeHealth(url)) {
      process.env.ALPHADECK_MARKET_API_ENABLED = '1';
      console.log(`✅ Market API ready at ${url}`);
      return { child, managed: true };
    }
    if (child.exitCode !== null) {
      throw new Error(`market-data exited before becoming ready (code ${child.exitCode})`);
    }
    await delay(500);
  }

  stopChild(child);
  throw new Error(`market-data did not become healthy within 20 seconds: ${url}/health`);
}

function startGenerationWorkerIfNeeded() {
  if (!shouldRunGenerationWorker()) {
    console.log('↪️  Generation dispatch uses inline mode or an externally managed Worker.');
    return { child: null, managed: false };
  }
  return startWorker('generation');
}

function startEvaluationWorkerIfNeeded() {
  if (!envFlag('ALPHADECK_DEV_MANAGE_EVAL_WORKER', true)) {
    return { child: null, managed: false };
  }
  return startWorker('evaluation');
}

function startWorker(kind) {
  console.log(`🚀 Starting durable ${kind} Worker`);
  const moduleName = kind === 'generation' ? 'alphadeck_platform.worker_cli' : 'alphadeck_platform.evaluation_worker';
  const child = spawn(
    'uv',
    ['run', '--directory', 'services/platform-api', '--locked', 'python', '-m', moduleName],
    { cwd: rootDir, stdio: 'inherit', shell: isWindows, detached: !isWindows, env: process.env }
  );
  child.once('error', (error) => {
    console.error(`❌ Failed to start ${kind} Worker: ${error.message}`);
  });
  return { child, managed: true };
}

async function startPlatformApiIfNeeded() {
  const url = (process.env.ALPHADECK_PLATFORM_API_URL || 'http://127.0.0.1:38081').replace(/\/$/, '');
  const parsed = new URL(url);
  if (parsed.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname) || parsed.username || parsed.password || parsed.pathname !== '/') {
    throw new Error('Managed Python API requires a local HTTP URL without credentials or a path.');
  }
  if (await probeHealth(url, 1200, '/api/health')) return { child: null, managed: false };
  console.log(`🚀 Starting Python platform API on ${url}`);
  const child = spawn('uv', ['run', '--directory', 'services/platform-api', '--locked', 'uvicorn', 'app:app', '--host', parsed.hostname.replace(/[\[\]]/g, ''), '--port', parsed.port || '80'], {
    cwd: rootDir, stdio: 'inherit', shell: isWindows, detached: !isWindows, env: process.env,
  });
  child.once('error', error => console.error(`❌ Python platform API failed: ${error.message}`));
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (await probeHealth(url, 1200, '/api/health')) return { child, managed: true };
    if (child.exitCode !== null) break;
    await delay(500);
  }
  stopChild(child);
  throw new Error('Python platform API did not become healthy.');
}

async function main() {
  process.env.PI_AGENT_DISPATCH_MODE = 'worker';
  process.env.ALPHADECK_PYTHON_PLATFORM = '1';
  const production = process.argv.includes('--production');
  if (production) process.env.NODE_ENV = 'production';
  const { preferredPort, passthrough } = parseCliArgs(process.argv.slice(2).filter(arg => arg !== '--production'));
  if (!production) await ensureEnvironment({ preferredPort });
  const market = await startMarketApiIfNeeded();
  let platform = { child: null, managed: false };
  let web;
  let worker = { child: null, managed: false };
  let evaluationWorker = { child: null, managed: false };

  try {
    platform = await startPlatformApiIfNeeded();
    if (production) {
      const port = preferredPort || Number(process.env.PORT || process.env.WEB_PORT || '3100');
      const host = process.env.ALPHADECK_WEB_HOST || '127.0.0.1';
      const child = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'start', '--hostname', host, '--port', String(port), ...passthrough], {
        cwd: rootDir, stdio: 'inherit', detached: !isWindows, env: process.env,
      });
      child.once('error', error => console.error(`Next.js production startup failed: ${error.message}`));
      web = { child, port, url: `http://${host}:${port}` };
    } else {
      web = await startWebDevServer({ preferredPort, passthrough, stdio: 'inherit', detached: !isWindows });
    }
    worker = startGenerationWorkerIfNeeded();
    evaluationWorker = startEvaluationWorkerIfNeeded();
  } catch (error) {
    if (platform.managed) stopChild(platform.child);
    if (market.managed) stopChild(market.child);
    if (worker.managed) stopChild(worker.child);
    if (evaluationWorker.managed) stopChild(evaluationWorker.child);
    throw error;
  }

  let shuttingDown = false;
  const shutdown = (exitCode = 0) => {
    if (shuttingDown) return;
    shuttingDown = true;
    stopChild(web.child);
    if (platform.managed) stopChild(platform.child);
    if (market.managed) stopChild(market.child);
    if (worker.managed) stopChild(worker.child);
    if (evaluationWorker.managed) stopChild(evaluationWorker.child);
    setTimeout(() => process.exit(exitCode), 250).unref();
  };

  process.on('SIGINT', () => shutdown(0));
  process.on('SIGTERM', () => shutdown(0));

  web.child.on('exit', (code) => {
    if (!shuttingDown) shutdown(typeof code === 'number' ? code : 1);
  });
  if (market.managed) {
    market.child.on('exit', (code) => {
      if (!shuttingDown) {
        console.error(`❌ market-data exited unexpectedly (code ${code ?? 'unknown'})`);
        shutdown(typeof code === 'number' && code !== 0 ? code : 1);
      }
    });
  }
  for (const [kind, managedWorker] of [['platform', platform], ['generation', worker], ['evaluation', evaluationWorker]]) {
    if (!managedWorker.managed) continue;
    managedWorker.child.on('exit', (code) => {
      if (!shuttingDown) {
        console.error(`❌ ${kind} Worker exited unexpectedly (code ${code ?? 'unknown'})`);
        shutdown(typeof code === 'number' && code !== 0 ? code : 1);
      }
    });
  }
}

if (require.main === module) {
  main().catch((error) => {
    console.error('\n❌ Failed to launch AlphaDeck development stack');
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}

module.exports = {
  envFlag,
  marketApiUrl,
  probeHealth,
  shouldRunMarketApi,
  shouldRunGenerationWorker,
  startGenerationWorkerIfNeeded,
  startEvaluationWorkerIfNeeded,
  startMarketApiIfNeeded,
};
