import { readFile, stat } from 'node:fs/promises';

const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
for (const file of ['../python/landscape.py', '../assets/report.html', '../schema/landscape-v1.schema.json']) {
  if (!(await stat(new URL(file, import.meta.url))).isFile()) throw new Error(`Missing package asset: ${file}`);
}
const template = await readFile(new URL('../assets/report.html', import.meta.url), 'utf8');
if (template.split('__LANDSCAPE_DATA__').length !== 2) {
  throw new Error('HTML template must contain exactly one data placeholder');
}
const schema = JSON.parse(await readFile(new URL('../schema/landscape-v1.schema.json', import.meta.url), 'utf8'));
if (schema.properties.schema_version.const !== 1) throw new Error('Expected landscape schema v1');
if (pkg.bin['repo-landscape'] !== './bin/repo-landscape.mjs') throw new Error('Missing CLI entry point');
console.log(`Bundled scanner, HTML template and v1 schema verified for ${pkg.name}@${pkg.version}`);
