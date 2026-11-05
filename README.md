# Repository landscape CLI

`@devex-metrics/repo-landscape` is a public, versioned CLI for reproducible
repository inventories and offline HTML reports. It scans an explicit set of
GitHub repositories; optional organization discovery **only admits names on a
reviewed allowlist**. The portable JSON v1 output can be consumed by a DevEx
adapter without importing any team deployment, baseline, or customer state.

The recommended **full analysis** reads local, full-history Git clones at
their pinned HEADs: repository size, language files and physical LOC, 90-day
activity, ADRs, manifest and CI artifact/dependency evidence, AI-file hashes,
age/lag/staleness, pinned commit drift, static content cues, repository references,
dependency candidates and baseline comparison. An API-only scan
without `--repos-dir` produces the same required AI-file v1 fields but marks
`provenance.analysis: "ai_only"` and has no local-only metrics or edges. No
cloning, network publishing, or HTML hosting is performed by the CLI.

## Requirements and installation

- Node.js **22 or newer**, Python **3.11 or newer** on `PATH` (or set
  `REPO_LANDSCAPE_PYTHON` to a Python executable).
- Git on `PATH` for full local analysis. The package has **no consumer build
  step**, runtime Node dependencies, or Python dependencies beyond the standard
  library; the scanner and report template ship in the npm tarball.
- Install the published package:
  `npm install --save-dev @devex-metrics/repo-landscape`. To install a
  specific release, append its version (for example, `@0.1.0`). In a checkout
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
The API reader pins HEAD to the newest default-branch commit at or before
`--as-of` (the scan start when omitted), so a push during a scan cannot make
a snapshot newer than its `generated_at`; a repository with no such commit
fails the scan. It resolves the Git tree SHA from the pinned HEAD commit, not
from the commit SHA itself. GitHub may return 404 for the canonical empty Git tree
(`4b825dc642cb6eb9a060e54bf8d69288fbee4904`); **only that exact tree**
is treated as containing zero files. Any other tree lookup failure still fails.

## Scan and render

Clone each selected repository with full history under
`./repositories/<owner>/<repo>` (a flat `./repositories/<repo>` also works if
unambiguous). Only Git checkout roots count as matches: ordinary owner
containers and subdirectories of another clone do not cause ambiguity, including
when the owner and repository have the same name. Two checkout roots at the
supported paths are still rejected as ambiguous; unsafe paths or broken Git
metadata fail the scan. The clone's `origin` must match `owner/repo` on github.com, and
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

### Readiness and information extensions (engine 0.2.0, schema 1)

These are **additive optional v1 fields**. Historical snapshots, including
0.1.2 baselines, remain valid without them and are never rewritten. New scans
advertise supported rules in `provenance.capabilities` and explain limits in
`capability_warnings`. Consumers should validate historical input against v1
independently from checking capabilities required of a fresh deployment.
Do not treat absent older measurements as zero, removed files, or fresh evidence.

Both local and API scans add the following AI-file fields:

| Field | Deterministic meaning |
|---|---|
| `commits_since` | Reachable commits in `last_path_commit..pinned_HEAD`, excluding the path commit. Local `git rev-list --count` and GitHub compare `ahead_by` use the same graph-distance meaning. Null when path history is unavailable. |
| `commits_since_change` | Reachable commits with committer timestamp **at or after** `last_changed` and at or before `generated_at`, inclusive. Matches the existing DevEx timestamp-window signal, not graph distance. Equal timestamps, merges and clock skew can make it differ from `commits_since + 1`. |
| `content_signal` | `{score, scope, actions, verification, guardrails}` or null. Four static booleans, 25 points each; never an assessment of quality, correctness, effectiveness or tool configuration validity. |
| `content_signal_reason` | Why content was not scored, when `content_signal` is null. |

Scoring uses exactly the hashed pinned blob bytes, never the dirty working
tree. API bytes must also match the tree's Git blob SHA. Only strict UTF-8
files of at most **64 KiB** are scored, in sorted repository/path order.
The scoring and timestamp-count budgets are **150 repositories, 1,000 files,
200 files per repository**. Discovery, hashes and freshness remain present
outside those budgets. Content is never executed or retained in the cache.
The four cues intentionally match the DevEx static rubric:

- **Scope:** a Markdown heading starting with scope, purpose, context,
  about this repo/project, or repository; or the phrase this repository/repo/project.
- **Actions:** a `-`, `*`, or numbered Markdown list item starting with run, add,
  update, write, use, check, verify, review, create, change, keep, test, or build.
- **Verification:** a heading starting with testing, validation, verification,
  or checks; or run/execute followed by a recognized npm, pnpm, yarn, pytest,
  go test, cargo test, or dotnet test command.
- **Guardrails:** do not, don't, never, must not, or avoid.

`ai_summary` adds `min_age_days`, `max_age_days`, `min_lag_days`,
`min_commits_since`, `max_commits_since`, `history_known_count`,
`commits_since_known_count`, `commits_since_change_known_count`,
`content_scored_count`, and `content_signal_mean`. Extrema summarize **known**
observations only; consult coverage counts before drawing conclusions. The mean
is null unless every discovered file was scored (and there is at least one).
`by_kind` contains `{kind,count,known_count,stale_count,unknown_count}` for every
supported AI kind, including zero-count kinds. Missing a kind is not a quality
failure; repositories need not use every tool.

Repository `activity` is `{commits_30d,commits_90d,complete,unknown_reason?}`:
counts use inclusive **elapsed** 30/90-day timestamp windows and pinned history.
These are distinct from the existing local `git` UTC **calendar-day** trend
windows, whose behavior is unchanged. API timestamp history is paged in batches
of 100, up to 500 commits per query; reaching that cap without a short terminal
page leaves counts null with incomplete coverage. Duplicate, malformed or
out-of-window history fails the scan. Permission/rate-limit failures still fail
the whole scan, rather than producing a success-shaped empty inventory.

`attention` is `{activity,priority,reasons}` and is explicitly **rule-based**.
Activity is high at >=50 commits/30 elapsed days, medium at >=10, low otherwise,
and unknown without complete counts. Missing/stale AI files with high/medium
activity produce high attention; any missing/stale/incomplete readiness evidence
otherwise produces medium attention; complete current evidence is low, or
unknown if activity is unknown. Attention does not infer team ownership or
prioritize by undisclosed membership.

### Optional configured-team aggregate counts

Only an explicit `team_author_emails` array in caller-owned config enables
local team attribution. Values match raw Git **author email exactly**, including
case; there is no mailmap, name/handle inference, home-directory configuration,
or extra permission request. Supply every desired alias explicitly. Do not
commit private membership configuration into this public package.

`team_activity` is
`{available,complete,matching,commit_count,commits_30d,commits_90d,unknown_reason?}`.
For local full history, `matching: "exact_author_email"` enumerates every
reachable commit; lifetime and elapsed-window matched counts are comparable
to all reachable `git.commit_count` and `activity` counts. A known zero means
no configured author email matched, not no team exists. No configuration or
API mode produces unavailable/null counts, never zero. No author identities
or the configured list are written to output/cache.

API linked-GitHub-author team attribution remains **consumer enrichment**:
REST author emails are not interchangeable with authenticated linked logins.
Keep existing GraphQL logic and its complete-pagination checks where needed.
Likewise, a local distinct-author-email `git.contributor_count` is not a count
of all GitHub users, viewers, collaborators, or configured team members.

### References, report detail and comparisons

Full local scans recognize qualified selected `owner/repo` mentions and
canonical `https://github.com/owner/repo` URLs in pinned documentation, AI,
manifest and CI text files <=64 KiB. Top-level `references` records
`{source,target,kind:"repository reference",confidence:"low",
evidence:{path,line,head_sha}}`. Direction means *mentions*, not *depends on*.
Only explicitly selected targets are linked. The scanner neither follows those
references nor selects additional repositories. References are kept separate
from consumer-to-producer `edges` (medium-confidence normalized artifact
matches). Unmatched artifact declarations and unsupported topology capabilities
are surfaced as warnings, not promoted to relationships.

The offline report exposes per-kind freshness/coverage, min/max age/lag/drift,
cue flags and hashes, activity/attention rules, branch/lifetime/30d/90d counts,
aggregate authors, every collected daily trend/hotspot, language files/LOC,
extensions, ADR/manifest paths, produced/consumed artifact versions and source
paths, warnings, pinned evidence and separate references. Details are collapsible
and follow the repository/status filters. API-only or historical missing facts
remain explicitly unknown or not analyzed.

Comparison adds per-repository `file_deltas` for intersecting file paths:
`{path,age_days,lag_days,commits_since,commits_since_change,content_score}`.
Each value is current minus baseline or null if either observation is absent.
Existing `metric_changes` also reports readiness range/coverage/mean and
aggregate-author changes; availability changes use null without a numeric
delta. The report shows baseline/current readiness, file deltas and actual
added/removed dependency candidates. `comparison.edges.complete` is false with
an `unknown_reason` and empty change lists if artifact dependencies were not
analyzed in both snapshots; absence of API-only edges is not interpreted as
removed dependencies. Reference evidence is a current-snapshot
view, not an inferred historical topology change.

### Parity boundaries

| Original analysis fact/view | Portable engine/report |
|---|---|
| AI discovery, per-file freshness and commit drift | Both sources; pinned evidence, coverage and unknowns retained |
| Rich AI readiness view | Kind coverage, min/max ranges, exact static DevEx cues, activity and documented attention rules |
| Repository size, languages, activity, ADRs, artifact versions | Existing local facts now disclosed in report drilldowns |
| Team-vs-total commits | Explicit local author-email config; authenticated API linked-login attribution remains caller enrichment |
| Repository-name mentions | Separate low-confidence selected-repository references with path/line evidence |
| Telemetry-token / curated registry relations | Not defaulted: opaque heuristics and caller-owned registries do not prove runtime topology |
| Team ownership, subjective quality, tool setup correctness | Not inferred; requires explicit context or actual validation outside this deterministic scanner |
| Raw contributor identities, commit subjects/authors, absolute paths | Intentionally excluded from portable parity |

### Consumer rollout

Publish a reviewed **0.2.0** release before pinning it in the team wrapper,
template/mirrors, DevEx adapter or dashboard. Keep immutable old baselines
unchanged. Update fresh-scan capability checks separately from historical v1
validation, and extend strict adapter allowlists/types for the optional fields.
Pass scanner signals through rather than re-scoring them; retain consumer
GraphQL enrichment when counts are null/unavailable (especially API histories
above the REST budget or configured linked-login team attribution). Do not
replace `commits_since_change` with `commits_since`; their semantics differ.
Do not silently remove current enrichment until its callers and validation
paths accept engine signals. Re-render reports from fresh JSON and verify all
consumer deployment surfaces reference the same **published** package version.

## Development and release

Run `npm ci`, `npm test`, `npm run build`, and `npm pack --dry-run`. The test
suite creates synthetic, local Git fixtures; it does not call private
repositories. Changes to output v1 must update the JSON schema, tests, and
adapter documentation together; incompatible changes need a new schema
version.

Version `0.1.0` is published on npm from the signed `v0.1.0` tag on reviewed
`main`. The npm trusted publisher for GitHub Actions uses owner
`devex-metrics`, repository `repo-landscape-cli`, and workflow filename
`publish.yml` (matching `.github/workflows/publish.yml` exactly). Before each
future release, verify this configuration in the package settings on npmjs.com.
Merge reviewed, signed changes, including the new `package.json` version, into
`main`; then create a signed tag (for example, `git tag -s vX.Y.Z`) on that
commit and manually push it. Do not publish manually. No npm token is
committed or used by the workflow. The tag workflow checks that the tag equals
`v<package.json version>` and that its commit is reachable from `main`, runs
tests/build/pack, and publishes using npm CLI **>=11.5.1** and GitHub Actions
OIDC (`id-token: write`, `npm publish --provenance --access public`). There is
no dispatch-based version bump or automatic release.

The source is MIT-licensed as a clean, generic implementation; it does not
include any private repository baseline, inventory, customer material, or
team-specific content.
