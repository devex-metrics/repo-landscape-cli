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
        class FakeApi:
            truncated = False
            def get(self, path, params=None):
                if path == "/repos/example/widget":
                    return {"full_name": "example/widget", "default_branch": "main"}
                if path.endswith("/commits/main"):
                    return {"sha": "a" * 40, "commit": {"committer": {"date": UPDATE}}}
                if "/git/trees/" in path:
                    return {"truncated": self.truncated, "tree": [
                        {"type": "blob", "path": "AGENTS.md", "sha": "b" * 40}
                    ]}
                if "/git/blobs/" in path:
                    return {"encoding": "base64", "content": base64.b64encode(b"synthetic").decode()}
                if path.endswith("/commits"):
                    return []
                if path == "/orgs/example/repos":
                    return [{"full_name": "example/widget"}, {"full_name": "example/unreviewed"}]
                raise AssertionError(path)
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
        api.truncated = True
        with self.assertRaisesRegex(landscape.ScanError, "complete Git tree"):
            landscape.scan_repo("example/widget", landscape.timestamp(AS_OF), 10,
                                {"entries": {}}, api)

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
