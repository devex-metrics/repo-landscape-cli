#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scanner = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'python', 'landscape.py');
const choices = process.env.REPO_LANDSCAPE_PYTHON
  ? [[process.env.REPO_LANDSCAPE_PYTHON]]
  : process.platform === 'win32'
    ? [['python'], ['py', '-3']]
    : [['python3'], ['python']];

for (const [command, ...prefix] of choices) {
  const probe = spawnSync(command, [...prefix, '--version'], { encoding: 'utf8' });
  if (probe.error?.code === 'ENOENT') continue;
  const match = /^Python (\d+)\.(\d+)/.exec(`${probe.stdout || ''}${probe.stderr || ''}`.trim());
  if (probe.status !== 0 || !match || Number(match[1]) < 3 ||
      (Number(match[1]) === 3 && Number(match[2]) < 11)) {
    console.error(`repo-landscape: ${command} must be Python 3.11 or newer`);
    process.exit(1);
  }
  const result = spawnSync(command, [...prefix, scanner, ...process.argv.slice(2)], {
    stdio: 'inherit',
  });
  if (result.error) {
    console.error(`repo-landscape: unable to start ${command}: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

console.error('repo-landscape: Python 3.11+ not found; set REPO_LANDSCAPE_PYTHON to its executable');
process.exit(1);
