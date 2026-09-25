# Repository landscape CLI

`@devex-metrics/repo-landscape` is a public, versioned CLI for reproducible
repository inventories and offline HTML reports. It scans an explicit set of
GitHub repositories; optional organization discovery **only admits names on a
reviewed allowlist**. The portable JSON v1 output can be consumed by a DevEx
adapter without importing any team deployment, baseline, or customer state.

The recommended **full analysis** reads local, full-history Git clones at
their pinned HEADs: repository size, language files and physical LOC, 90-day
activity, ADRs, manifest and CI artifact/dependency evidence, AI-file hashes,
age/lag/staleness, dependency edges and baseline comparison. An API-only scan
without `--repos-dir` produces the same required AI-file v1 fields but marks
`provenance.analysis: "ai_only"` and has no local-only metrics or edges. No
cloning, network publishing, or HTML hosting is performed by the CLI.

## Requirements and installation

- Node.js **22 or newer**, Python **3.11 or newer** on `PATH` (or set
  `REPO_LANDSCAPE_PYTHON` to a Python executable).
- Git on `PATH` for full local analysis. The package has **no consumer build
  step**, runtime Node dependencies, or Python dependencies beyond the standard
  library; the scanner and report template ship in the npm tarball.
- Install the package *after its first release is published*:
  `npm install --save-dev @devex-metrics/repo-landscape@0.1.0`. In a checkout
  of this source, use `node bin/repo-landscape.mjs` directly.

```json
{
  "schema_version": 1,
  "repositories": ["example/widget", "example/consumer"],
  "stale_after_days": 90
}
```

Save that as `landscape.config.json`. Repositories are mandatory selections;
each `owner/repo` must be readable. `stale_after_days` is optional (default
90). For organization discovery, add:

```json
{
  "discovery": [
    {
      "organization": "example",
      "reviewed_repositories": ["example/widget", "example/consumer"]
    }
  ]
}
```

The discovery entry is **in addition** to the required `schema_version` and
`repositories` fields (which may be `[]` if discovery selects repositories).
Only reviewed names actually present in the organization's API listing are
selected. Discovery uses `GH_TOKEN`, then `GITHUB_TOKEN` if available; API-only
scanning uses the same environment variable. Do not put a token in config.
For private repositories, give the token read-only access to every selected
repository and, for discovery, permission to list the organization. A 403,
404, rate-limit failure, missing required clone, truncated API tree, or invalid
full-history checkout **fails the whole scan**, without writing a new JSON
output. Unknown *file history* is represented explicitly, not as a fresh file.

## Scan and render

Clone each selected repository with full history under
`./repositories/<owner>/<repo>` (a flat `./repositories/<repo>` also works if
unambiguous). The clone's `origin` must match `owner/repo` on github.com, and
its local HEAD must agree with its `origin/<branch>` remote-tracking ref. A
separate `--expected-heads` JSON object can pin the exact HEAD for each
selection: `{"example/widget":"<40 lowercase hex SHA>",...}`.

```sh
npm exec -- repo-landscape scan \
  --config ./landscape.config.json \
  --repos-dir ./repositories \
  --expected-heads ./expected-heads.json \
  --output ./landscape.json \
  --cache ./.repo-landscape-cache.json \
  --as-of 2026-09-25T00:00:00Z
npm exec -- repo-landscape report --input ./landscape.json --output ./landscape.html
```

Omit `--expected-heads` if no manifest was captured. Omit `--repos-dir` for
an AI-only GitHub API scan; explicit and reviewed selection still applies.
Pass `--as-of` to reproduce the same UTC timestamp, age and activity windows.
The scanner sorts repositories, AI files and evidence; it refreshes relative
age and activity even on a cache hit. The optional cache stores only extracted
metadata keyed by scanner version, source and HEAD, never raw file bodies.
Paths are explicit; the CLI never silently uses sibling folders or overwrites
its baseline/input with output.

To compare against a prior caller-owned snapshot, pass
`--baseline ./previous.json` to `scan`. On an initial import **omit the flag**;
after a successful scan the caller can separately retain a copy as its
baseline. The engine reads, validates and compares the baseline but never
creates, rewrites, or migrates it. The current `repositories` array remains a
current snapshot; optional `comparison` lists per-repository file changes,
metric/HEAD deltas, AI readiness and added/removed dependency edges.
Keep team-owned baseline and inventory files in a private repository.

## Portable JSON v1

The complete, tested schemas are
[`schema/config-v1.schema.json`](schema/config-v1.schema.json) and
[`schema/landscape-v1.schema.json`](schema/landscape-v1.schema.json). The core
adapter contract is:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-25T00:00:00Z",
  "scanner_version": "0.1.0",
  "provenance": {
    "source": "github_api",
    "snapshot": "pinned_head",
    "analysis": "ai_only",
    "stale_after_days": 90
  },
  "selection": {
    "mode": "explicit",
    "explicit_repositories": ["example/widget"],
    "discovery": [],
    "selected_repositories": ["example/widget"]
  },
  "repositories": [
    {
      "full_name": "example/widget",
      "head_sha": "0000000000000000000000000000000000000000",
      "head_committed_at": "2026-09-20T00:00:00Z",
      "ai_files": [
        {
          "path": "AGENTS.md",
          "kind": "agents",
          "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "last_changed": "2026-09-19T00:00:00Z",
          "age_days": 6,
          "lag_days": 1,
          "stale": false,
          "status": "known",
          "evidence": {
            "source": "github_api",
            "head_sha": "0000000000000000000000000000000000000000",
            "blob_sha": "1111111111111111111111111111111111111111",
            "last_commit_sha": "2222222222222222222222222222222222222222"
          }
        }
      ],
      "ai_summary": {
        "count": 1,
        "stale_count": 0,
        "max_lag_days": 1,
        "unknown_count": 0,
        "status": "known"
      }
    }
  ],
  "edges": []
}
```

Full local records also include `title`, `summary`, `repo_type`, `metrics`
(`files`, `bytes`, `source_loc`, legacy `estimated_code_lines`), `languages`
(files and physical LOC), `extensions`, `git` (90-entry commit trend,
contributor count without identities and path hotspots), `architecture`
(ADR paths), `manifests`, `produces`, `consumes`, and `analysis_warnings`.
Evidence-based top-level `edges` link consumers to producers. Output
never contains local machine paths, Git author emails, configured team
handles, or source file contents; it **does** contain repository names,
README summaries, paths, dependency names, and Git-derived facts.

`age_days` is whole UTC days since the file's last path commit at `generated_at`.
`lag_days` is whole UTC days between that path commit and the pinned HEAD
commit; `stale` means `lag_days >= stale_after_days` (not age alone). If path
history is unavailable, `status: "unknown"`, `unknown_reason`, and null
temporal/stale fields prevent a false green result. Git blob SHA-1 is retained
as evidence, while **`sha256` hashes the actual file content bytes**.
`comparison` is present only with `--baseline`: it lists added, removed,
changed, unchanged, and unknown file paths plus metric and edge changes.
Neither a cache hit nor an
unknown status can silently convert a required repository failure to success.

The report is self-contained: no CDN, external fonts, telemetry, or network
requests. It embeds the JSON safely and displays provenance, inventory,
dependency graph/evidence, AI-file lag and baseline changes. There is **no
Pages workflow**: hosting JSON/HTML is a separate, deliberate privacy
decision by the caller. Review recipients, repository names and evidence
before uploading either artifact.

## Development and release

Run `npm ci`, `npm test`, `npm run build`, and `npm pack --dry-run`. The test
suite creates synthetic, local Git fixtures; it does not call private
repositories. Changes to output v1 must update the JSON schema, tests, and
adapter documentation together; incompatible changes need a new schema
version.

**Do not tag or publish yet.** `@devex-metrics/repo-landscape` ownership and
first publication have not been verified. npm trusted publishing cannot be
configured for a package before the first package version exists. A package
owner must first bootstrap the namespace/package manually with a distinct
lower version (for example `0.0.0`), approved credentials, and a reviewed
release procedure; do not manually publish the version intended for the
tag-driven release. Then configure the npm trusted
publisher for GitHub repository `devex-metrics/repo-landscape-cli` and
`.github/workflows/publish.yml`. No token is committed or used by this
workflow. Only after that setup and reviewed, signed changes have merged,
manually push a `v<package.json version>` release tag. The tag workflow
checks equality and that the tag commit is already reachable from the
reviewed, signed `main` branch, runs tests/build/pack and publishes using npm CLI
**>=11.5.1** and GitHub Actions OIDC (`id-token: write`,
`npm publish --provenance --access public`). There is no dispatch-based
version bump or automatic release.

The source is MIT-licensed as a clean, generic implementation; it does not
include any private repository baseline, inventory, customer material, or
team-specific content.
