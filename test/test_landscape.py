import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("landscape", ROOT / "python" / "landscape.py")
landscape = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(landscape)
START = "2026-09-01T12:00:00+00:00"
UPDATE = "2026-09-20T12:00:00+00:00"
AS_OF = "2026-10-01T12:00:00Z"


class LandscapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="landscape-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clones = self.root / "clones"
        self.clones.mkdir()
        self.config = self.root / "config.json"
        self.output = self.root / "landscape.json"
        self.cache = self.root / "cache.json"
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "repositories": ["example/widget"],
            "stale_after_days": 10,
        }), encoding="utf-8")

    def git(self, cwd, *args, date=None):
        env = os.environ.copy()
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, env=env).stdout.decode().strip()

    def create_clone(self, name="example/widget", flat=False):
        owner, basename = name.split("/")
        bare = self.root / (basename + ".git")
        seed = self.root / (basename + "-seed")
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                       capture_output=True, check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)],
                       capture_output=True, check=True)
        self.git(seed, "config", "user.name", "Fixture")
        self.git(seed, "config", "user.email", "fixture@example.invalid")
        (seed / "AGENTS.md").write_bytes(b"Synthetic instructions\n")
        (seed / "package.json").write_text(json.dumps(
            {"name": "@example/widget" if basename == "widget" else "@example/consumer",
             "version": "1.0.0",
             "dependencies": {"@example/widget": "^1.0.0"} if basename != "widget" else {}}
        ), encoding="utf-8")
        (seed / "src").mkdir()
        (seed / "src" / "code.py").write_bytes(b"one\r\ntwo\rthree\n\xffend")
        (seed / "docs" / "adr").mkdir(parents=True)
        (seed / "docs" / "adr" / "0001.md").write_text("# Decision\n", encoding="utf-8")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-q", "-m", "Initial synthetic content", date=START)
        self.git(seed, "remote", "add", "origin", str(bare))
        self.git(seed, "push", "-q", "-u", "origin", "main")
        checkout = self.clones / (basename if flat else name)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(checkout)],
                       capture_output=True, check=True)
        self.git(checkout, "remote", "set-url", "origin", f"https://github.com/{name}.git")
        self.git(checkout, "config", "user.name", "Fixture")
        self.git(checkout, "config", "user.email", "fixture@example.invalid")
        if basename == "widget":
            (checkout / "src" / "code.py").write_bytes(b"one\r\ntwo\rthree\n\xffend\nnew\n")
            self.git(checkout, "add", ".")
            self.git(checkout, "commit", "-q", "-m", "Change source only", date=UPDATE)
            self.git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        return checkout

    def scan(self, **options):
        args = argparse.Namespace(config=str(self.config), output=str(self.output),
                                  baseline=None, cache=str(self.cache), as_of=AS_OF,
                                  repos_dir=str(self.clones), expected_heads=None)
        for key, value in options.items():
            setattr(args, key, value)
        return landscape.run_scan(args)

    def validate_schema(self):
        subprocess.run(["node", str(ROOT / "test" / "validate.mjs"), str(self.output)],
                       cwd=ROOT, capture_output=True, check=True)

    def test_full_local_scan_schema_cache_determinism_and_baseline(self):
        checkout = self.create_clone()
        head = self.git(checkout, "rev-parse", "HEAD")
        manifest = self.root / "heads.json"
        manifest.write_text(json.dumps({"example/widget": head}), encoding="utf-8")
        first = self.scan(expected_heads=str(manifest))
        self.validate_schema()
        self.assertEqual(first["provenance"]["analysis"], "full")
        self.assertEqual(first["repositories"][0]["metrics"]["source_loc"], 5)
        self.assertEqual(first["repositories"][0]["git"]["commit_count"], 2)
        self.assertEqual(len(first["repositories"][0]["git"]["commits_90d_trend"]), 90)
        self.assertEqual(first["repositories"][0]["architecture"]["adr_count"], 1)
        file = first["repositories"][0]["ai_files"][0]
        self.assertEqual(file["sha256"], hashlib.sha256(b"Synthetic instructions\n").hexdigest())
        self.assertEqual(file["lag_days"], 19)
        self.assertTrue(file["stale"])
        original = self.output.read_bytes()
        self.scan(expected_heads=str(manifest))
        self.assertEqual(original, self.output.read_bytes())
        self.assertNotIn(str(self.root).encode(), original)
        self.assertNotIn(b"fixture@example.invalid", original)
        baseline = self.root / "baseline.json"
        baseline.write_bytes(original)
        (checkout / "AGENTS.md").write_text("Updated synthetic instructions\n", encoding="utf-8")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-q", "-m", "Refresh instructions", date=UPDATE)
        self.git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        second = self.scan(expected_heads=None, baseline=str(baseline))
        self.validate_schema()
        self.assertEqual(second["comparison"]["repositories"][0]["changed_files"], ["AGENTS.md"])
        self.assertIn("head_sha", second["comparison"]["repositories"][0]["metric_changes"])
        self.assertEqual(second["comparison"]["summary"]["repositories_changed"], 1)
        self.assertEqual(baseline.read_bytes(), original)
        later = self.scan(as_of="2026-10-03T12:00:00Z")
        self.assertEqual(later["repositories"][0]["head_sha"], second["repositories"][0]["head_sha"])
        self.assertEqual(later["repositories"][0]["ai_files"][0]["age_days"],
                         second["repositories"][0]["ai_files"][0]["age_days"] + 2)
        self.assertEqual(later["repositories"][0]["ai_files"][0]["lag_days"],
                         second["repositories"][0]["ai_files"][0]["lag_days"])

    def test_edges_and_flat_clone_layout(self):
        self.create_clone(flat=True)
        self.create_clone("example/consumer")
        self.config.write_text(json.dumps({"schema_version": 1, "repositories": [
            "example/widget", "example/consumer"
        ]}), encoding="utf-8")
        result = self.scan()
        self.validate_schema()
        self.assertEqual([repo["full_name"] for repo in result["repositories"]],
                         ["example/consumer", "example/widget"])
        self.assertEqual(result["edges"][0]["source"], "example/consumer")
        self.assertEqual(result["edges"][0]["target"], "example/widget")

    def test_missing_or_mismatched_required_clone_does_not_replace_output(self):
        self.output.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "missing"):
            self.scan()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing")
        checkout = self.create_clone()
        self.git(checkout, "remote", "set-url", "origin", "https://github.com/elsewhere/widget.git")
        with self.assertRaisesRegex(landscape.ScanError, "wrong GitHub origin"):
            self.scan()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing")

    def test_self_named_owner_clone_layouts(self):
        for name, flat in (("profile/profile", False), ("account/account", True)):
            with self.subTest(flat=flat):
                checkout = self.create_clone(name, flat=flat)
                if flat:
                    (checkout / name.split("/")[1]).mkdir()
                self.config.write_text(json.dumps({
                    "schema_version": 1, "repositories": [name],
                }), encoding="utf-8")
                head = self.git(checkout, "rev-parse", "HEAD")
                manifest = self.root / "heads.json"
                manifest.write_text(json.dumps({name: head}), encoding="utf-8")
                first = self.scan(expected_heads=str(manifest))
                self.assertEqual(first["repositories"][0]["head_sha"], head)
                original = self.output.read_bytes()
                self.scan(expected_heads=str(manifest))
                self.assertEqual(self.output.read_bytes(), original)
                self.scan(expected_heads=str(manifest), cache=None)
                self.assertEqual(self.output.read_bytes(), original)

    def test_flat_clone_subdirectory_is_not_a_second_checkout(self):
        owner = self.create_clone("profile/profile", flat=True)
        (owner / "widget").mkdir()
        checkout = self.create_clone("profile/widget", flat=True)
        self.assertEqual(landscape.local_checkout(self.clones, "profile/widget"), checkout)

    def test_non_checkout_candidates_are_ignored(self):
        checkout = self.create_clone()
        alternate = self.clones / "widget"
        alternate.mkdir()
        self.assertEqual(landscape.local_checkout(self.clones, "example/widget"), checkout)
        alternate.rmdir()
        alternate.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "safe directory"):
            landscape.local_checkout(self.clones, "example/widget")

    def test_duplicate_checkout_roots_are_ambiguous(self):
        checkout = self.create_clone()
        subprocess.run(["git", "clone", "-q", str(checkout), str(self.clones / "widget")],
                       capture_output=True, check=True)
        self.output.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "ambiguous"):
            self.scan()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing")

    def test_broken_alternate_checkout_fails_instead_of_being_ignored(self):
        self.create_clone()
        alternate = self.clones / "widget"
        alternate.mkdir()
        (alternate / ".git").write_text("gitdir: missing\n", encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "Git rev-parse failed"):
            self.scan()

    def test_linked_worktree_is_a_checkout_root(self):
        checkout = self.create_clone()
        original = self.root / "original-checkout"
        checkout.rename(original)
        linked = self.clones / "widget"
        self.git(original, "worktree", "add", "-q", "-b", "linked", str(linked))
        self.git(linked, "update-ref", "refs/remotes/origin/linked", "HEAD")
        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(landscape.local_checkout(self.clones, "example/widget"), linked)

    def test_only_container_is_a_missing_checkout(self):
        (self.clones / "widget").mkdir()
        with self.assertRaisesRegex(landscape.ScanError, "missing"):
            self.scan()

    def test_checkout_validation_still_rejects_invalid_git_state(self):
        checkout = self.create_clone()
        head = self.git(checkout, "rev-parse", "HEAD")
        shallow = checkout / ".git" / "shallow"
        shallow.write_text(head + "\n", encoding="ascii")
        with self.assertRaisesRegex(landscape.ScanError, "shallow"):
            self.scan()
        shallow.unlink()
        self.git(checkout, "checkout", "--detach", "-q")
        with self.assertRaisesRegex(landscape.ScanError, "tracked branch"):
            self.scan()
        self.git(checkout, "checkout", "-q", "main")
        self.git(checkout, "update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaisesRegex(landscape.ScanError, "tracking ref"):
            self.scan()

    def test_out_of_root_candidate_is_rejected(self):
        self.create_clone()
        alternate = self.clones / "widget"
        try:
            alternate.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        with self.assertRaisesRegex(landscape.ScanError, "safe directory"):
            self.scan()

    def test_expected_heads_and_tracking_mismatches_fail(self):
        checkout = self.create_clone()
        manifest = self.root / "heads.json"
        manifest.write_text(json.dumps({"example/widget": "0" * 40}), encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "HEAD mismatch"):
            self.scan(expected_heads=str(manifest))
        (checkout / "src" / "code.py").write_bytes(b"another commit\n")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-q", "-m", "Unpushed update", date=UPDATE)
        with self.assertRaisesRegex(landscape.ScanError, "HEAD mismatch"):
            self.scan(expected_heads=None)

    def test_cached_scan_still_checks_required_clone_access(self):
        checkout = self.create_clone()
        self.scan()
        original = self.output.read_bytes()
        self.git(checkout, "remote", "set-url", "origin", "https://github.com/unreviewed/widget.git")
        with self.assertRaisesRegex(landscape.ScanError, "wrong GitHub origin"):
            self.scan()
        self.assertEqual(self.output.read_bytes(), original)

    def test_corrupt_cache_fails_without_replacing_snapshot(self):
        self.create_clone()
        self.scan()
        original = self.output.read_bytes()
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        entry = next(iter(cache["entries"].values()))
        entry["ai_files"][0]["evidence"]["head_sha"] = "0" * 40
        self.cache.write_text(json.dumps(cache), encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "cached AI-file evidence"):
            self.scan()
        self.assertEqual(self.output.read_bytes(), original)

    def test_baseline_output_collision_and_invalid_config(self):
        self.create_clone()
        with self.assertRaisesRegex(landscape.ScanError, "distinct"):
            self.scan(baseline=str(self.output))
        self.config.write_text('{"schema_version":1,"schema_version":1,"repositories":[]}',
                               encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "Duplicate JSON key"):
            self.scan()

    def test_report_escapes_untrusted_repository_text(self):
        self.create_clone()
        result = self.scan()
        result["repositories"][0]["summary"] = "</script><script>alert('unsafe')</script>"
        self.output.write_text(json.dumps(result), encoding="utf-8")
        html = self.root / "report.html"
        landscape.run_report(argparse.Namespace(input=str(self.output), output=str(html)))
        page = html.read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert", page)
        self.assertIn("\\u003c/script>", page)
        self.assertIn("Repository landscape", page)

    def test_api_unknown_history_and_truncated_tree_fail_closed(self):
        blob_sha = hashlib.sha1(b"blob 9\0synthetic", usedforsecurity=False).hexdigest()
        class FakeApi:
            truncated = False
            no_commits = False
            tree_sha = "c" * 40
            def get(self, path, params=None):
                if path == "/repos/example/widget":
                    return {"full_name": "example/widget", "default_branch": "main"}
                if path == "/repos/example/widget/commits" and params.get("sha") == "main":
                    if params != {"sha": "main", "until": "2026-10-01T12:00:00Z", "per_page": 1}:
                        raise AssertionError(f"HEAD must be pinned to --as-of: {params}")
                    if self.no_commits:
                        return []
                    return [{"sha": "a" * 40, "commit": {
                        "committer": {"date": UPDATE}, "tree": {"sha": self.tree_sha}
                    }}]
                if path == "/repos/example/widget/git/trees/" + "c" * 40:
                    self.assert_equal_tree_params(params)
                    return {"truncated": self.truncated, "tree": [
                        {"type": "blob", "path": "AGENTS.md", "sha": blob_sha}
                    ]}
                if "/git/trees/" in path:
                    raise landscape.ScanError(f"GitHub API HTTP 404 for {path}")
                if path == "/repos/example/widget/git/blobs/" + blob_sha:
                    return {"encoding": "base64", "content": base64.b64encode(b"synthetic").decode()}
                if path.endswith("/commits"):
                    if "since" in params:
                        if params["sha"] != "a" * 40:
                            raise AssertionError("Activity history must use pinned HEAD")
                        return []
                    if params != {"sha": "a" * 40, "path": "AGENTS.md", "per_page": 1}:
                        raise AssertionError(f"File history must be pinned to commit SHA: {params}")
                    return []
                if path == "/orgs/example/repos":
                    return [{"full_name": "example/widget"}, {"full_name": "example/unreviewed"}]
                raise AssertionError(path)
            @staticmethod
            def assert_equal_tree_params(params):
                if params != {"recursive": 1}:
                    raise AssertionError(f"Expected complete recursive tree request: {params}")
        api = FakeApi()
        names, selection = landscape.select_repositories([], [{
            "organization": "example", "reviewed_repositories": ["example/widget"]
        }], api)
        self.assertEqual(names, ["example/widget"])
        self.assertEqual(selection["discovery"][0]["discovered_count"], 2)
        self.assertEqual(selection["selected_repositories"], ["example/widget"])
        result = landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                     {"entries": {}}, api)
        self.assertEqual(result["ai_files"][0]["status"], "unknown")
        self.assertIsNone(result["ai_files"][0]["stale"])
        self.assertEqual(result["ai_summary"]["status"], "partial_unknown")
        self.assertEqual(result["head_sha"], "a" * 40)
        self.assertEqual(result["ai_files"][0]["evidence"]["head_sha"], "a" * 40)
        api.truncated = True
        with self.assertRaisesRegex(landscape.ScanError, "complete Git tree"):
            landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                {"entries": {}}, api)
        api.truncated = False
        api.tree_sha = landscape.EMPTY_GIT_TREE_SHA
        empty = landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                    {"entries": {}}, api)
        self.assertEqual(empty["ai_files"], [])
        self.assertEqual(empty["ai_summary"]["count"], 0)
        api.tree_sha = "e" * 40
        with self.assertRaisesRegex(landscape.ScanError, "HTTP 404"):
            landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                {"entries": {}}, api)
        api.tree_sha = "not-a-tree-sha"
        with self.assertRaisesRegex(landscape.ScanError, "HEAD tree SHA"):
            landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                {"entries": {}}, api)
        api.tree_sha = "c" * 40
        api.no_commits = True
        with self.assertRaisesRegex(landscape.ScanError, "at or before --as-of"):
            landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                {"entries": {}}, api)

    def test_static_cues_exact_rubric_and_bounded_strict_content(self):
        complete = b"# Scope\nThis repo is synthetic.\n- Run npm test\nNever commit credentials.\n"
        self.assertEqual(landscape.file_signals(complete)["content_signal"], {
            "score": 100, "scope": True, "actions": True, "verification": True, "guardrails": True,
        })
        for text, expected in [
            ("# Overview\nAlways required.\n+ Ensure consistency\nnpm test\n", 0),
            ("##about this project\n1) Write a fixture.\nExecute pnpm run lint.\nDon't skip checks.", 100),
            ("## Checks\n", 25),
            ("A purpose heading is not necessarily a Scope heading.", 0),
            ("- Review changes\n", 25),
            ("Avoid generated output.", 25),
            ("\ufeff# Scope\r- Run npm test\rNever skip checks.", 100),
            ("#\u00a0Scope\u2028-\u00a0Review changes\u2029Avoid secrets.", 75),
            ("١) Run checks", 0),
            ("this repository\u00e9", 25),
            ("# Chec\u212as", 0),
        ]:
            with self.subTest(text=text):
                self.assertEqual(landscape.file_signals(text.encode())["content_signal"]["score"], expected)
        for content, within, reason in [
            (b"\xff", True, "strict UTF-8"),
            (b"x" * (64 * 1024 + 1), True, "64 KiB"),
            (complete, False, "200-file"),
        ]:
            signal = landscape.file_signals(content, within)
            self.assertIsNone(signal["content_signal"])
            self.assertIn(reason, signal["content_signal_reason"])
        self.assertIsNotNone(landscape.file_signals(b"x" * (64 * 1024))["content_signal"])

    def test_readiness_rollups_team_privacy_references_and_old_baseline(self):
        checkout = self.create_clone()
        (checkout / "AGENTS.md").write_text(
            "# Scope\nThis repository is synthetic.\n- Run npm test\nNever skip checks.\n",
            encoding="utf-8")
        (checkout / "README.md").write_text(
            "# Synthetic\nSee https://github.com/example/consumer and example/consumer.\n",
            encoding="utf-8")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-q", "-m", "Synthetic readiness", date=UPDATE)
        self.git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.create_clone("example/consumer")
        self.config.write_text(json.dumps({
            "schema_version": 1, "repositories": ["example/widget", "example/consumer"],
            "team_author_emails": ["fixture@example.invalid"],
        }), encoding="utf-8")
        result = self.scan()
        self.validate_schema()
        repo = result["repositories"][1]
        self.assertEqual(repo["ai_files"][0]["commits_since"], 0)
        # Includes both commits at UPDATE, not just the path's own commit.
        self.assertEqual(repo["ai_files"][0]["commits_since_change"], 2)
        self.assertEqual(repo["ai_summary"]["content_signal_mean"], 100)
        self.assertEqual(repo["ai_summary"]["content_scored_count"], 1)
        self.assertEqual(repo["ai_summary"]["min_age_days"], 11)
        self.assertEqual(repo["ai_summary"]["min_lag_days"], 0)
        self.assertEqual(len(repo["ai_summary"]["by_kind"]), len(landscape.AI_KINDS))
        self.assertEqual(repo["team_activity"]["commit_count"], 3)
        self.assertTrue(repo["team_activity"]["complete"])
        self.assertEqual(repo["activity"]["commits_30d"], 3)
        reference = next(item for item in result["references"]
                         if item["source"] == "example/widget" and item["target"] == "example/consumer")
        self.assertEqual(reference["evidence"]["line"], 2)
        self.assertNotIn(b"fixture@example.invalid", self.output.read_bytes())
        self.assertNotIn(b"fixture@example.invalid", self.cache.read_bytes())
        original = self.output.read_bytes()
        self.scan(cache=None)
        self.assertEqual(original, self.output.read_bytes())
        self.scan()
        self.assertEqual(original, self.output.read_bytes())
        old = json.loads((ROOT / "test" / "fixtures" / "landscape-v1.json").read_text())
        baseline = self.root / "old.json"
        baseline.write_text(json.dumps(old), encoding="utf-8")
        baseline_bytes = baseline.read_bytes()
        comparison = self.scan(baseline=str(baseline))["comparison"]
        widget = next(item for item in comparison["repositories"] if item["full_name"] == "example/widget")
        self.assertIsNone(widget["file_deltas"][0]["content_score"])
        self.assertIsNone(widget["metric_changes"]["content_signal_mean"]["baseline"])
        self.assertNotIn("delta", widget["metric_changes"]["content_signal_mean"])
        self.assertEqual(baseline.read_bytes(), baseline_bytes)
        self.validate_schema()

    def test_unknown_signals_attention_budgets_and_exact_team_matching(self):
        checkout = self.create_clone()
        (checkout / "AGENTS.md").write_bytes(b"\xff")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-q", "-m", "Synthetic non-text", date=UPDATE)
        self.git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.config.write_text(json.dumps({
            "schema_version": 1, "repositories": ["example/widget"],
            "team_author_emails": ["Fixture@example.invalid"],
        }), encoding="utf-8")
        result = self.scan()
        repo = result["repositories"][0]
        self.assertIsNone(repo["ai_summary"]["content_signal_mean"])
        self.assertEqual(repo["ai_summary"]["content_scored_count"], 0)
        self.assertEqual(repo["team_activity"]["commit_count"], 0)
        self.assertEqual(repo["attention"]["priority"], "medium")
        self.assertIn("incomplete", repo["attention"]["reasons"][0])
        self.validate_schema()
        head = self.git(checkout, "rev-parse", "HEAD")
        original_git_text = landscape.git_text
        def missing_email(repo, *args, **kwargs):
            if "--format=%H%x09%ae%x09%cI" in args:
                return f"{head}\t\t{UPDATE}"
            return original_git_text(repo, *args, **kwargs)
        with patch.object(landscape, "git_text", side_effect=missing_email):
            activity = landscape.git_activity(checkout, head, landscape.timestamp(AS_OF),
                                               {"fixture@example.invalid"})
        self.assertFalse(activity["team_activity"]["available"])
        self.assertIsNone(activity["team_activity"]["commit_count"])
        prototype = repo["ai_files"][0]
        large = []
        for index in range(151):
            file = dict(prototype, content_signal=landscape.file_signals(b"This repo")["content_signal"])
            large.append({
                "full_name": f"example/r{index}", "ai_files": [file], "ai_summary": {},
                "activity": {"commits_30d": 50, "commits_90d": 50, "complete": True},
            })
        landscape.add_readiness(large)
        self.assertIsNone(large[-1]["ai_files"][0]["content_signal"])
        self.assertIsNone(large[-1]["ai_files"][0]["commits_since_change"])
        self.assertEqual(large[-1]["ai_summary"]["content_scored_count"], 0)
        large[0]["ai_files"] = [dict(prototype) for _ in range(1001)]
        landscape.add_readiness(large[:1])
        self.assertIsNone(large[0]["ai_files"][-1]["content_signal"])

    def test_api_known_signals_pinned_comparison_and_permission_failure(self):
        content = b"# Purpose\n- Run npm test\nNever skip validation.\n"
        blob_sha = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content,
                                usedforsecurity=False).hexdigest()
        class FakeApi:
            fail = False
            corrupt_blob = False
            def get(self, path, params=None):
                if path == "/repos/example/widget":
                    return {"full_name": "example/widget", "default_branch": "main"}
                if "/git/trees/" in path:
                    return {"truncated": False, "tree": [{"type": "blob", "path": "AGENTS.md", "sha": blob_sha}]}
                if "/git/blobs/" in path:
                    return {"encoding": "base64", "content": base64.b64encode(
                        b"wrong bytes" if self.corrupt_blob else content).decode()}
                if "/compare/" in path:
                    if self.fail:
                        raise landscape.ScanError("GitHub API HTTP 403")
                    if path != "/repos/example/widget/compare/" + "d" * 40 + "..." + "a" * 40:
                        raise AssertionError(path)
                    return {"status": "ahead", "ahead_by": 1}
                if path.endswith("/commits"):
                    if "path" in params:
                        return [{"sha": "d" * 40, "commit": {"committer": {"date": START}}}]
                    head = {"sha": "a" * 40, "commit": {
                        "committer": {"date": UPDATE}, "tree": {"sha": "c" * 40},
                    }}
                    if "since" in params:
                        if params["sha"] != "a" * 40:
                            raise AssertionError("Unpinned history")
                        return [head, {"sha": "d" * 40, "commit": {"committer": {"date": START}}}]
                    return [head]
                raise AssertionError(path)
        api = FakeApi()
        result = landscape.run_scan(argparse.Namespace(
            config=str(self.config), output=str(self.output), baseline=None,
            cache=str(self.cache), as_of=AS_OF, repos_dir=None, expected_heads=None), api)
        file = result["repositories"][0]["ai_files"][0]
        self.assertEqual(file["commits_since"], 1)
        self.assertEqual(file["commits_since_change"], 2)
        self.assertEqual(file["content_signal"]["score"], 100)
        self.assertEqual(result["provenance"]["analysis"], "ai_only")
        self.assertFalse(result["repositories"][0]["team_activity"]["available"])
        self.validate_schema()
        original = self.output.read_bytes()
        api.fail = True
        with self.assertRaisesRegex(landscape.ScanError, "403"):
            landscape.run_scan(argparse.Namespace(
                config=str(self.config), output=str(self.output), baseline=None,
                cache=None, as_of=AS_OF, repos_dir=None, expected_heads=None), api)
        self.assertEqual(self.output.read_bytes(), original)
        api.fail = False
        api.corrupt_blob = True
        with self.assertRaisesRegex(landscape.ScanError, "blob hash mismatch"):
            landscape.run_scan(argparse.Namespace(
                config=str(self.config), output=str(self.output), baseline=None,
                cache=None, as_of=AS_OF, repos_dir=None, expected_heads=None), api)
        self.assertEqual(self.output.read_bytes(), original)

    def test_api_history_budget_is_unknown_and_duplicates_fail(self):
        class FakeApi:
            duplicate = False
            requests = []
            def get(self, path, params):
                self.requests.append(params)
                return [{"sha": f"{(0 if self.duplicate else params['page'] * 100 + index):040x}",
                         "commit": {"committer": {"date": UPDATE}}} for index in range(100)]
        api = FakeApi()
        activity = landscape.api_activity("example/widget", "a" * 40, landscape.timestamp(AS_OF), api)
        self.assertFalse(activity["complete"])
        self.assertIsNone(activity["commits_30d"])
        self.assertIsNone(activity["commits_90d"])
        self.assertEqual(len(api.requests), 5)
        api.duplicate = True
        with self.assertRaisesRegex(landscape.ScanError, "pagination"):
            landscape.api_activity("example/widget", "a" * 40, landscape.timestamp(AS_OF), api)

    def test_standard_library_validator_rejects_malformed_history_and_signal_summary(self):
        fixture = json.loads((ROOT / "test" / "fixtures" / "landscape-readiness-api-v1.json").read_text())
        landscape.validate_baseline(fixture)
        fixture["repositories"][0]["ai_summary"]["content_scored_count"] = 0
        with self.assertRaisesRegex(landscape.ScanError, "Inconsistent AI summary"):
            landscape.validate_baseline(fixture)
        fixture["repositories"][0]["ai_summary"]["content_scored_count"] = 1
        fixture["repositories"][0]["activity"]["complete"] = False
        with self.assertRaises(landscape.ScanError):
            landscape.validate_baseline(fixture)
        invalid = self.root / "nonfinite.json"
        invalid.write_text('{"value":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "Non-finite"):
            landscape.read_json(invalid)

    def test_api_only_baseline_has_unknown_dependency_deltas_not_added_edges(self):
        baseline = json.loads((ROOT / "test" / "fixtures" / "landscape-v1.json").read_text())
        local = json.loads((ROOT / "test" / "fixtures" / "landscape-readiness-local-v1.json").read_text())
        comparison = landscape.compare_baseline(local["repositories"], baseline, local["edges"])
        self.assertFalse(comparison["edges"]["complete"])
        self.assertEqual(comparison["edges"]["added"], [])
        self.assertEqual(comparison["edges"]["removed"], [])
        self.assertIn("not analyzed", comparison["edges"]["unknown_reason"])
        baseline["provenance"]["analysis"] = "full"
        # A real full baseline is required, not a relabeled AI-only snapshot.
        with self.assertRaises(landscape.ScanError):
            landscape.compare_baseline(local["repositories"], baseline, local["edges"])

    def test_invalid_cached_signal_and_extra_fields_fail_closed(self):
        self.create_clone()
        self.scan()
        original = self.output.read_bytes()
        cache = json.loads(self.cache.read_text())
        record = next(iter(cache["entries"].values()))
        record["ai_files"][0]["content_signal"]["score"] = 99
        self.cache.write_text(json.dumps(cache), encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "cue score"):
            self.scan()
        self.assertEqual(self.output.read_bytes(), original)
        record["ai_files"][0]["content_signal"]["score"] = 0
        record["unexpected"] = True
        self.cache.write_text(json.dumps(cache), encoding="utf-8")
        with self.assertRaisesRegex(landscape.ScanError, "unexpected"):
            self.scan()
        self.assertEqual(self.output.read_bytes(), original)

    def test_matching_dependencies_do_not_duplicate_graph_edges(self):
        edges = landscape.match_edges([
            {"full_name": "example/widget", "produces": [
                {"name": "@example/widget", "kind": "npm-package", "source": "package.json"}
            ], "consumes": []},
            {"full_name": "example/consumer", "produces": [], "consumes": [
                {"name": "@example/widget", "kind": "npm-dependency", "source": "package.json"},
                {"name": "widget", "kind": "other-dependency", "source": "requirements.txt"},
            ]},
        ])
        self.assertEqual(len(edges), 1)
        self.assertGreaterEqual(len(edges[0]["evidence"]), 2)
        self.assertEqual(len(edges[0]["evidence"]), len({
            (item["consumer_file"], item["consumed"], item["producer_file"], item["produced"])
            for item in edges[0]["evidence"]
        }))

    def test_ai_configuration_directories_include_generic_files(self):
        self.assertEqual(landscape.kind_for(".claude/settings.json"), "tool_config")
        self.assertEqual(landscape.kind_for(".cursor/mcp.json"), "tool_config")
        self.assertEqual(landscape.kind_for(".windsurf/config.yaml"), "tool_config")
        self.assertEqual(landscape.kind_for(".cursor/rules/main.mdc"), "cursor")
        self.assertEqual(landscape.kind_for(".github/agents/helper.md"), "agent")
        self.assertIsNone(landscape.kind_for("src/config.json"))

    def test_generic_build_and_ci_evidence_is_in_full_inventory(self):
        checkout = self.create_clone()
        (checkout / "CMakeLists.txt").write_text(
            "add_library(widget_core src/code.py)\nfind_package(ZLIB REQUIRED)\n", encoding="utf-8")
        (checkout / "conanfile.txt").write_text("[requires]\nzlib/1.3\n", encoding="utf-8")
        (checkout / "Android.bp").write_text(
            'cc_library {\n  name: "widget_module",\n  shared_libs: ["base_module"],\n}\n',
            encoding="utf-8")
        (checkout / "Jenkinsfile").write_text(
            'archiveArtifacts "build/widget.zip"\nbuild(job: "upstream")\n',
            encoding="utf-8")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-q", "-m", "Add generic build evidence", date=UPDATE)
        self.git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        record = self.scan()["repositories"][0]
        self.validate_schema()
        self.assertIn(("widget_core", "cmake-target"), {
            (item["name"], item["kind"]) for item in record["produces"]
        })
        self.assertIn(("widget_module", "android-module"), {
            (item["name"], item["kind"]) for item in record["produces"]
        })
        self.assertIn(("build/widget.zip", "ci-archived-artifact"), {
            (item["name"], item["kind"]) for item in record["produces"]
        })
        self.assertIn(("ZLIB", "cmake-dependency"), {
            (item["name"], item["kind"]) for item in record["consumes"]
        })


if __name__ == "__main__":
    unittest.main()
