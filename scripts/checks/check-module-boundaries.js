#!/usr/bin/env node
const { spawnSync } = require('node:child_process');
const result = spawnSync('npm', ['run', "check:frontend-boundary"], { stdio: 'inherit', shell: false });
process.exit(result.status ?? 1);
