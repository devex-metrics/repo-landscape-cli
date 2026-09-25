import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const load = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url)));
const ajv = addFormats(new Ajv2020({ allErrors: true }));
const config = ajv.compile(load('../schema/config-v1.schema.json'));
const landscape = ajv.compile(load('../schema/landscape-v1.schema.json'));
const fixture = load('./fixtures/landscape-v1.json');

test('config v1 accepts explicit/reviewed selections and rejects invalid input', () => {
  assert.equal(config(load('./fixtures/config.json')), true, JSON.stringify(config.errors));
  assert.equal(config({ schema_version: 1, repositories: [], discovery: [{
    organization: 'example', reviewed_repositories: ['example/widget'],
  }] }), true);
  assert.equal(config({ schema_version: 1, repositories: [] }), false);
  assert.equal(config({ schema_version: 1, repositories: ['example/widget'], stale_after_days: 0 }), false);
});

test('landscape v1 schema validates contract and unknown status shape', () => {
  assert.equal(landscape(fixture), true, JSON.stringify(landscape.errors));
  const incompleteFull = structuredClone(fixture);
  incompleteFull.provenance.analysis = 'full';
  assert.equal(landscape(incompleteFull), false);
  const unknown = structuredClone(fixture);
  const file = unknown.repositories[0].ai_files[0];
  file.status = 'unknown';
  file.last_changed = file.age_days = file.lag_days = file.stale = null;
  file.unknown_reason = 'No commit history returned';
  unknown.repositories[0].ai_summary.status = 'partial_unknown';
  unknown.repositories[0].ai_summary.unknown_count = 1;
  unknown.repositories[0].ai_summary.max_lag_days = null;
  assert.equal(landscape(unknown), true, JSON.stringify(landscape.errors));
  delete file.unknown_reason;
  assert.equal(landscape(unknown), false);
  assert.equal(landscape({ ...fixture, schema_version: 2 }), false);
});

test('launcher reports version and offline report renders without a consumer build', () => {
  const launcher = new URL('../bin/repo-landscape.mjs', import.meta.url);
  const version = execFileSync(process.execPath, [fileURLToPath(launcher), '--version'], { encoding: 'utf8' });
  assert.match(version, /repo-landscape 0\.1\.0/);
  const directory = mkdtempSync(join(tmpdir(), 'landscape-report-'));
  try {
    const html = join(directory, 'report.html');
    execFileSync(process.execPath, [fileURLToPath(launcher), 'report', '--input',
      fileURLToPath(new URL('./fixtures/landscape-v1.json', import.meta.url)), '--output', html]);
    const page = readFileSync(html, 'utf8');
    assert.match(page, /example\/widget/);
    assert.match(page, /--cp-accent/);
    assert.doesNotMatch(page, /<script[^>]+src=|<link[^>]+href=/);
    const scripts = [...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
    new vm.Script(scripts.at(-1)[1]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('release workflow requires reviewed main ancestry and tag-version equality before OIDC publish', () => {
  const workflow = readFileSync(new URL('../.github/workflows/publish.yml', import.meta.url), 'utf8');
  assert.match(workflow, /git merge-base --is-ancestor HEAD origin\/main/);
  assert.match(workflow, /"\$RELEASE_TAG" != "v\$version"/);
  assert.match(workflow, /id-token: write/);
  assert.match(workflow, /npm publish --provenance --access public/);
  assert.doesNotMatch(workflow, /NPM_TOKEN|NODE_AUTH_TOKEN|workflow_dispatch/);
});
