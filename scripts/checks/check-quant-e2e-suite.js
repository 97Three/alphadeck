#!/usr/bin/env node

const path = require('path');
const { spawnSync } = require('child_process');
const { loadQuantE2eSuite } = require('./quant-e2e-suite');

const root = process.cwd();
const runRuntimeControls = process.argv.includes('--run-runtime-controls');

function main() {
  const suite = loadQuantE2eSuite({ root, requireReleaseCoverage: true });
  console.log(
    `[e2e-suite] ok: ${suite.id} live-model=${suite.caseIds.length} ` +
    `product-controls=${suite.productControlCaseIds.length} runtime-tests=${suite.runtimeTestFiles.length}`,
  );
  if (!runRuntimeControls) return;

  const executable = 'uv';
  const result = spawnSync(executable, ['run', '--directory', 'services/platform-api', '--locked', '--group', 'dev', 'pytest', '-q', ...suite.runtimeTestFiles.map(file => path.relative('services/platform-api', file))], {
    cwd: root,
    env: process.env,
    shell: false,
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
  console.log('[e2e-suite] repair/recovery/security runtime controls passed');
}

try {
  main();
} catch (error) {
  console.error('[e2e-suite] failed:', error instanceof Error ? error.message : error);
  process.exit(1);
}
