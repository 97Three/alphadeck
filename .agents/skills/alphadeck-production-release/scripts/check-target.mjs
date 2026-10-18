#!/usr/bin/env node

import path from 'node:path';

function usage() {
  console.log(`Usage:
  check-target.mjs

Validate operator-provided AlphaDeck production coordinates. This command
does not connect to production or print credentials.`);
}

function fail(messages) {
  for (const message of messages) {
    console.error(`[release-target] ${message}`);
  }
  process.exit(2);
}

function read(name, fallback = '') {
  return String(process.env[name] || fallback).trim();
}

if (process.argv.includes('--help') || process.argv.includes('-h')) {
  usage();
  process.exit(0);
}

const target = {
  host: read('ALPHADECK_RELEASE_HOST'),
  publicUrl: read('ALPHADECK_PUBLIC_URL'),
  releaseRoot: read('ALPHADECK_RELEASE_ROOT', '/opt/alphadeck'),
  environmentFile: read(
    'ALPHADECK_RELEASE_ENV_FILE',
    '/etc/alphadeck/alphadeck.env',
  ),
  backupRoot: read('ALPHADECK_BACKUP_ROOT', '/var/backups/alphadeck'),
};

const errors = [];
if (!target.host) {
  errors.push('ALPHADECK_RELEASE_HOST is required; do not infer a production host');
} else if (
  target.host.startsWith('-') ||
  target.host.includes('://') ||
  !/^[A-Za-z0-9._@:[\]-]+$/.test(target.host)
) {
  errors.push('ALPHADECK_RELEASE_HOST must be one SSH host or configured alias');
}

if (!target.publicUrl) {
  errors.push('ALPHADECK_PUBLIC_URL is required for production smoke tests');
} else {
  try {
    const url = new URL(target.publicUrl);
    if (url.protocol !== 'https:') {
      errors.push('ALPHADECK_PUBLIC_URL must use HTTPS');
    }
    if (url.username || url.password) {
      errors.push('ALPHADECK_PUBLIC_URL must not contain credentials');
    }
  } catch {
    errors.push('ALPHADECK_PUBLIC_URL must be a valid absolute URL');
  }
}

for (const [label, value] of [
  ['ALPHADECK_RELEASE_ROOT', target.releaseRoot],
  ['ALPHADECK_RELEASE_ENV_FILE', target.environmentFile],
  ['ALPHADECK_BACKUP_ROOT', target.backupRoot],
]) {
  if (!path.posix.isAbsolute(value)) {
    errors.push(`${label} must be an absolute POSIX path`);
  } else if (
    /[\x00-\x1f]/.test(value) ||
    path.posix.normalize(value) !== value
  ) {
    errors.push(`${label} must be a normalized POSIX path without control characters`);
  }
}

if (errors.length > 0) fail(errors);

console.log(JSON.stringify({
  schemaVersion: 1,
  ok: true,
  ...target,
}, null, 2));
