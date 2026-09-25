# Repository landscape CLI

This is the public, portable engine for repository landscape reports. Do not
add customer data, repository inventories, team handles, baselines, tokens,
private documentation, or implicit organization defaults.

- Keep the scanner Python 3.11+ standard-library only. Node 22+ is a launcher
  and package manager; the published tarball must include the Python scanner,
  JSON schema, and self-contained HTML template. Consumers must not build it.
- Keep `schema/landscape-v1.schema.json`, the scanner output, synthetic tests,
  and README contract in sync. A breaking output change requires a new schema
  version. Sort repositories and AI files; `--as-of` must make repeated scans
  with the same repository heads produce identical output with or without cache.
- Explicitly selected repositories are required: a failed repository or GitHub
  API permission/rate-limit request fails the scan, without creating a
  success-shaped inventory. A missing file history may be represented as
  unknown, never as fresh. Never silently select unreviewed org repositories.
- A baseline is caller-owned read-only input. Do not overwrite or silently
  migrate it; comparison output is separate from the unchanged current scan
  and does not contain the baseline's private state.
- HTML reports must be offline and safe for arbitrary repository/file names.
  Publishing generated JSON or HTML is an explicit caller decision, never a
  default action of the CLI or its workflows.

Run `npm test`, `npm run build`, and `npm pack --dry-run` before publishing.
The publish workflow runs only on a manually pushed `v*` tag matching
`package.json`, after an npm trusted publisher has been configured.
