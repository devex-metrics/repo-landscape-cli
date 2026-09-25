#!/usr/bin/env python3
"""Portable GitHub repository AI-file inventory and offline report."""

import argparse
import base64
import binascii
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


SCHEMA_VERSION = 1
VERSION = json.loads((Path(__file__).resolve().parent.parent / "package.json").read_text(
    encoding="utf-8"
))["version"]
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin", ".c": "C", ".h": "C/C++",
    ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#",
    ".go": "Go", ".rs": "Rust", ".groovy": "Groovy", ".sh": "Shell",
    ".ps1": "PowerShell", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
}
MANIFESTS = {
    "pyproject.toml", "requirements.txt", "package.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "go.mod", "cargo.toml", "dockerfile",
    "conanfile.py", "conanfile.txt", "conanfile.yaml", "conanfile.yml",
    "cmakelists.txt", "android.bp",
}


class ScanError(Exception):
    pass


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScanError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except json.JSONDecodeError as error:
        raise ScanError(f"Invalid JSON in {path}: {error}") from error


def require_fields(value, required, allowed, location):
    if not isinstance(value, dict):
        raise ScanError(f"{location} must be an object")
    missing = required - value.keys()
    extra = value.keys() - allowed
    if missing or extra:
        raise ScanError(f"{location}: missing {sorted(missing)}, unexpected {sorted(extra)}")


def validate_name(name, location):
    if not isinstance(name, str) or not REPO_NAME.fullmatch(name):
        raise ScanError(f"{location} must be an owner/repo name")
    return name


def config_from(path):
    config = read_json(path)
    require_fields(config, {"schema_version", "repositories"}, {
        "schema_version", "repositories", "discovery", "stale_after_days"
    }, "Config")
    if type(config["schema_version"]) is not int or config["schema_version"] != SCHEMA_VERSION:
        raise ScanError("Config schema_version must be 1")
    explicit = config["repositories"]
    if not isinstance(explicit, list):
        raise ScanError("Config repositories must be an array")
    for name in explicit:
        validate_name(name, "Config repository")
    if len({name.casefold() for name in explicit}) != len(explicit):
        raise ScanError("Config repositories must be unique ignoring case")
    discovery = config.get("discovery", [])
    if not isinstance(discovery, list):
        raise ScanError("Config discovery must be an array")
    for entry in discovery:
        require_fields(entry, {"organization", "reviewed_repositories"}, {
            "organization", "reviewed_repositories"
        }, "Discovery entry")
        org = entry["organization"]
        if not isinstance(org, str) or not REPO_NAME.fullmatch(f"{org}/repo"):
            raise ScanError("Discovery organization must be a GitHub organization name")
        reviewed = entry["reviewed_repositories"]
        if not isinstance(reviewed, list) or not reviewed:
            raise ScanError("Discovery reviewed_repositories must be a nonempty array")
        for name in reviewed:
            validate_name(name, "Reviewed repository")
            if name.split("/")[0].casefold() != org.casefold():
                raise ScanError(f"Reviewed repository {name} is not in {org}")
    stale_after = config.get("stale_after_days", 90)
    if type(stale_after) is not int or stale_after < 1:
        raise ScanError("Config stale_after_days must be a positive integer")
    if not explicit and not discovery:
        raise ScanError("Select at least one explicit or reviewed repository")
    return explicit, discovery, stale_after


def timestamp(value):
    if not isinstance(value, str):
        raise ScanError("Expected an ISO-8601 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScanError(f"Invalid ISO-8601 timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise ScanError(f"Timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def days_between(later, earlier):
    return max(0, int((later - earlier).total_seconds() // 86400))


def kind_for(path):
    parts = path.casefold().split("/")
    name = parts[-1]
    if name == "agents.md":
        return "agents"
    if parts == [".github", "copilot-instructions.md"]:
        return "copilot_instructions"
    if parts[:2] == [".github", "instructions"] and name.endswith(".instructions.md"):
        return "copilot_instructions"
    if parts[:2] == [".github", "prompts"] and name.endswith(".prompt.md"):
        return "prompt"
    if parts[:2] == [".github", "agents"] and name.endswith(".agent.md"):
        return "agent"
    if name == "claude.md":
        return "claude"
    if name == "gemini.md":
        return "gemini"
    if parts == [".cursorrules"] or (
        parts[:2] == [".cursor", "rules"] and name.endswith(".mdc")
    ):
        return "cursor"
    if parts == [".windsurfrules"]:
        return "windsurf"
    if parts == [".mcp.json"] or parts == ["mcp.json"]:
        return "mcp"
    if parts == [".waza.yaml"]:
        return "waza"
    if parts[:2] == [".github", "skills"]:
        return "skill"
    if parts[:2] == [".github", "chatmodes"]:
        return "chatmode"
    if parts[:2] == [".github", "agents"]:
        return "agent"
    if parts[:2] == [".github", "instructions"]:
        return "copilot_instructions"
    if parts[:2] == [".github", "prompts"]:
        return "prompt"
    if parts[0] in (".claude", ".cursor", ".windsurf") and len(parts) > 1:
        return "tool_config"
    return None


class GithubApi:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def get(self, path, params=None):
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "repo-landscape/" + VERSION,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise ScanError(f"GitHub API HTTP {error.code} for {path}; verify repository access and rate limits") from error
        except URLError as error:
            raise ScanError(f"GitHub API request failed for {path}: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise ScanError(f"GitHub API returned invalid JSON for {path}") from error


def select_repositories(explicit, discovery, api):
    selected = {name.casefold(): name for name in explicit}
    details = []
    for entry in discovery:
        org = entry["organization"]
        available = set()
        page = 1
        while True:
            repos = api.get(f"/orgs/{quote(org)}/repos", {
                "type": "all", "per_page": 100, "page": page
            })
            if not isinstance(repos, list):
                raise ScanError(f"GitHub returned an invalid repository list for {org}")
            for repo in repos:
                if not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str):
                    raise ScanError(f"GitHub returned an invalid repository entry for {org}")
                available.add(repo["full_name"].casefold())
            if len(repos) < 100:
                break
            page += 1
            if page > 1000:
                raise ScanError(f"Organization listing for {org} exceeded 1000 pages")
        reviewed = entry["reviewed_repositories"]
        for name in reviewed:
            if name.casefold() not in available:
                raise ScanError(f"Reviewed repository {name} was not found in {org}; no unreviewed repositories selected")
            selected.setdefault(name.casefold(), name)
        details.append({
            "organization": org,
            "reviewed_repositories": sorted(set(reviewed), key=str.casefold),
            "discovered_count": len(available),
        })
    names = sorted(selected.values(), key=str.casefold)
    return names, {
        "mode": ("explicit+reviewed_org" if explicit else "reviewed_org") if discovery else "explicit",
        "explicit_repositories": sorted(set(explicit), key=str.casefold),
        "discovery": sorted(details, key=lambda item: item["organization"].casefold()),
        "selected_repositories": names,
    }


def read_cache(path):
    if path is None or not path.exists():
        return {"cache_version": 2, "scanner_version": VERSION, "entries": {}}
    cache = read_json(path)
    if not isinstance(cache, dict) or not isinstance(cache.get("entries"), dict):
        raise ScanError(f"Invalid cache format: {path}")
    if cache.get("cache_version") != 2 or cache.get("scanner_version") != VERSION:
        print("repo-landscape: cache format or scanner version changed; refreshing cache", file=sys.stderr)
        return {"cache_version": 2, "scanner_version": VERSION, "entries": {}}
    return cache


def scan_static(name, head_sha, api):
    root = f"/repos/{name}"
    tree = api.get(f"{root}/git/trees/{head_sha}", {"recursive": 1})
    if not isinstance(tree, dict) or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise ScanError(f"Cannot scan complete Git tree for {name} at {head_sha}")
    candidates = []
    for item in tree["tree"]:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = item.get("path")
        blob_sha = item.get("sha")
        if not isinstance(path, str) or not isinstance(blob_sha, str):
            raise ScanError(f"Invalid Git tree entry for {name}")
        kind = kind_for(path)
        if kind:
            candidates.append((path, kind, blob_sha))
    results = []
    for path, kind, blob_sha in sorted(candidates, key=lambda item: item[0]):
        blob = api.get(f"{root}/git/blobs/{quote(blob_sha)}")
        if not isinstance(blob, dict) or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise ScanError(f"Cannot read AI file {name}/{path} at {head_sha}")
        try:
            contents = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
        except binascii.Error as error:
            raise ScanError(f"Invalid blob data for {name}/{path}") from error
        history = api.get(f"{root}/commits", {"sha": head_sha, "path": path, "per_page": 1})
        if not isinstance(history, list):
            raise ScanError(f"Invalid file history for {name}/{path}")
        file = {
            "path": path,
            "kind": kind,
            "sha256": hashlib.sha256(contents).hexdigest(),
            "last_changed": None,
            "status": "unknown",
            "evidence": {"source": "github_api", "head_sha": head_sha, "blob_sha": blob_sha},
        }
        if history:
            try:
                changed = history[0]["commit"]["committer"]["date"]
                commit_sha = history[0]["sha"]
            except (KeyError, IndexError, TypeError) as error:
                raise ScanError(f"Invalid commit history entry for {name}/{path}") from error
            if not isinstance(commit_sha, str) or not SHA.fullmatch(commit_sha):
                raise ScanError(f"Invalid history commit SHA for {name}/{path}")
            file["last_changed"] = iso(timestamp(changed))
            file["status"] = "known"
            file["evidence"]["last_commit_sha"] = commit_sha
        else:
            file["unknown_reason"] = "No path commit history returned at the pinned HEAD"
        results.append(file)
    return results


def refresh_ages(files, as_of, head_date, stale_after):
    refreshed = []
    for file in files:
        current = dict(file)
        current["evidence"] = dict(file["evidence"])
        if current["status"] == "known":
            changed = timestamp(current["last_changed"])
            current["age_days"] = days_between(as_of, changed)
            current["lag_days"] = days_between(head_date, changed)
            current["stale"] = current["lag_days"] >= stale_after
        else:
            current["age_days"] = None
            current["lag_days"] = None
            current["stale"] = None
        refreshed.append(current)
    return refreshed


def validate_cached_files(files, name, head_sha, source):
    if not isinstance(files, list):
        raise ScanError(f"Invalid cached AI files for {name} at {head_sha}")
    for file in files:
        if not isinstance(file, dict) or not {
            "path", "kind", "sha256", "last_changed", "status", "evidence"
        }.issubset(file):
            raise ScanError(f"Invalid cached AI-file entry for {name} at {head_sha}")
        evidence = file["evidence"]
        if (not isinstance(file["path"], str) or not isinstance(file["kind"], str) or
                not isinstance(file["sha256"], str) or not SHA256.fullmatch(file["sha256"]) or
                not isinstance(evidence, dict) or evidence.get("source") != source or
                evidence.get("head_sha") != head_sha or
                not isinstance(evidence.get("blob_sha"), str) or
                not SHA.fullmatch(evidence["blob_sha"])):
            raise ScanError(f"Invalid cached AI-file evidence for {name} at {head_sha}")
        if file["status"] == "known":
            timestamp(file["last_changed"])
        elif file["status"] != "unknown" or not isinstance(file.get("unknown_reason"), str):
            raise ScanError(f"Invalid cached AI-file status for {name} at {head_sha}")


def scan_repo(name, as_of, stale_after, cache, api):
    root = f"/repos/{name}"
    repository = api.get(root)
    if not isinstance(repository, dict) or not isinstance(repository.get("default_branch"), str):
        raise ScanError(f"Cannot read required repository {name}")
    if repository.get("full_name", "").casefold() != name.casefold():
        raise ScanError(f"GitHub returned a different repository for {name}")
    head = api.get(f"{root}/commits/{quote(repository['default_branch'], safe='')}")
    try:
        head_sha = head["sha"]
        head_date = timestamp(head["commit"]["committer"]["date"])
    except (KeyError, TypeError) as error:
        raise ScanError(f"Invalid HEAD commit for {name}") from error
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise ScanError(f"Invalid HEAD SHA for {name}")
    if head_date > as_of:
        raise ScanError(f"Pinned HEAD for {name} is newer than --as-of")
    key = "api:" + name.casefold() + "@" + head_sha
    cached = cache["entries"].get(key)
    if cached is None:
        static = scan_static(name, head_sha, api)
        cache["entries"][key] = static
    else:
        static = cached
        validate_cached_files(static, name, head_sha, "github_api")
    files = refresh_ages(static, as_of, head_date, stale_after)
    return {
        "full_name": name,
        "head_sha": head_sha,
        "head_committed_at": iso(head_date),
        "ai_files": files,
        "ai_summary": summarize_files(files),
    }


def summarize_files(files):
    lags = [file["lag_days"] for file in files if file["lag_days"] is not None]
    unknown = sum(file["status"] == "unknown" for file in files)
    return {
        "count": len(files),
        "stale_count": sum(file["stale"] is True for file in files),
        "max_lag_days": max(lags) if lags else None,
        "unknown_count": unknown,
        "status": "partial_unknown" if unknown else "known",
    }


def git_call(repo, *args, optional=False):
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-C", str(repo), *args],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScanError(f"Cannot run Git in required checkout {repo}: {error}") from error
    if result.returncode and not optional:
        raise ScanError(f"Git {args[0]} failed for required checkout {repo} (exit {result.returncode})")
    return result.stdout if result.returncode == 0 else None


def git_text(repo, *args, optional=False):
    value = git_call(repo, *args, optional=optional)
    if value is None:
        return None
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ScanError(f"Git returned non-UTF-8 metadata for {repo}") from error


def origin_name(remote):
    match = re.fullmatch(r"(?:[^@]+@)?github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?", remote, re.I)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    parsed = urlsplit(remote)
    if parsed.hostname and parsed.hostname.casefold() == "github.com" and parsed.scheme in ("https", "ssh"):
        parts = parsed.path.strip("/").removesuffix(".git").split("/")
        if len(parts) == 2:
            return "/".join(parts)
    return None


def local_checkout(root, name):
    root = root.resolve()
    owner, repo = name.split("/")
    paths = [root / owner / repo, root / repo]
    matches = [path for path in paths if path.exists()]
    if len(matches) != 1:
        raise ScanError(f"Required clone for {name} is {'ambiguous' if matches else 'missing'} beneath {root}")
    checkout = matches[0]
    if not checkout.is_dir() or not checkout.resolve().is_relative_to(root):
        raise ScanError(f"Required clone for {name} is not a safe directory beneath {root}")
    actual_root = Path(git_text(checkout, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != checkout.resolve():
        raise ScanError(f"Required clone for {name} is not a Git checkout root")
    if git_text(checkout, "rev-parse", "--is-shallow-repository") != "false":
        raise ScanError(f"Required clone for {name} is shallow; use a full-history clone")
    remote = git_text(checkout, "remote", "get-url", "origin")
    if not remote or (origin_name(remote) or "").casefold() != name.casefold():
        raise ScanError(f"Required clone for {name} has the wrong GitHub origin")
    branch = git_text(checkout, "symbolic-ref", "--short", "HEAD", optional=True)
    if not branch:
        raise ScanError(f"Required clone for {name} must check out a tracked branch")
    tracking = git_text(checkout, "rev-parse", "--verify", f"refs/remotes/origin/{branch}",
                        optional=True)
    if not tracking:
        raise ScanError(f"Required clone for {name} has no origin/{branch} tracking ref")
    if tracking != git_text(checkout, "rev-parse", "HEAD"):
        raise ScanError(f"Required clone for {name} has a HEAD mismatch with origin/{branch}")
    return checkout


def tracked_blobs(checkout, head):
    tree = git_call(checkout, "ls-tree", "-r", "-l", "-z", head)
    files = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, raw_sha, raw_size = metadata.split()
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ScanError(f"Invalid Git tree entry in {checkout}") from error
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            continue
        if not SHA.fullmatch(raw_sha.decode("ascii")) or not raw_size.isdigit():
            raise ScanError(f"Invalid Git blob metadata in {checkout}")
        files.append((path, raw_sha.decode("ascii"), int(raw_size)))
    return sorted(files)


@contextmanager
def blob_reader(checkout):
    command = ["git", "--no-pager", "-C", str(checkout), "cat-file", "--batch"]
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            def read(blob_sha):
                try:
                    process.stdin.write(blob_sha.encode("ascii") + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if len(header) != 3 or header[0] != blob_sha.encode("ascii") or header[1] != b"blob":
                        raise ScanError(f"Cannot read pinned Git blob {blob_sha} in {checkout}")
                    size = int(header[2])
                    content = process.stdout.read(size)
                    if len(content) != size or process.stdout.read(1) != b"\n":
                        raise ScanError(f"Incomplete pinned Git blob {blob_sha} in {checkout}")
                    return content
                except (OSError, ValueError) as error:
                    raise ScanError(f"Cannot read pinned Git blob {blob_sha} in {checkout}: {error}") from error
            yield read
            process.stdin.close()
            if process.wait(timeout=120):
                raise ScanError(f"Git batch blob reader failed in {checkout}")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScanError(f"Cannot run Git batch blob reader in {checkout}: {error}") from error


def physical_lines(content):
    if not content:
        return 0
    return (content.count(b"\n") + content.count(b"\r") - content.count(b"\r\n") +
            int(content[-1:] not in (b"\r", b"\n")))


def evidence_item(items, name, kind, path, version=""):
    item = {"name": str(name)[:240], "kind": kind, "source": path, "version": str(version)[:100]}
    if item["name"] and item not in items:
        items.append(item)


def parse_manifest(path, content, produces, consumes, warnings):
    if len(content) > 2_000_000:
        warnings.append({"path": path, "reason": "Manifest exceeds 2 MB analysis limit"})
        return
    text = content.decode("utf-8", errors="replace")
    name = path.rsplit("/", 1)[-1].casefold()
    if name == "package.json":
        try:
            package = json.loads(text)
            if not isinstance(package, dict):
                raise ValueError("Expected a package object")
            evidence_item(produces, package.get("name", path), "npm-package", path,
                          package.get("version", ""))
            for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                dependencies = package.get(section, {})
                if not isinstance(dependencies, dict):
                    raise ValueError(f"Expected an object for {section}")
                for dependency, version in sorted(dependencies.items()):
                    evidence_item(consumes, dependency, "npm-dependency", path, version)
        except (json.JSONDecodeError, ValueError) as error:
            warnings.append({"path": path, "reason": f"Cannot parse package manifest: {error}"})
    elif name == "pyproject.toml":
        try:
            document = tomllib.loads(text)
            project = document.get("project", {})
            poetry = document.get("tool", {}).get("poetry", {})
            if not isinstance(project, dict):
                raise ValueError("Expected a project object")
            if not isinstance(poetry, dict):
                raise ValueError("Expected a Poetry object")
            package_name = project.get("name") or poetry.get("name")
            if package_name:
                evidence_item(produces, package_name, "python-package", path,
                              project.get("version") or poetry.get("version") or "")
            for dependency in project.get("dependencies", []):
                dependency_name = re.split(r"[<>=!~;\[\s]", str(dependency), maxsplit=1)[0]
                evidence_item(consumes, dependency_name, "python-dependency", path)
            for section in (project.get("optional-dependencies", {}) or {}).values():
                for dependency in section:
                    dependency_name = re.split(r"[<>=!~;\[\s]", str(dependency), maxsplit=1)[0]
                    evidence_item(consumes, dependency_name, "python-dependency", path)
            for dependency in (poetry.get("dependencies", {}) or {}):
                if dependency.casefold() != "python":
                    evidence_item(consumes, dependency, "python-dependency", path)
        except (tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError) as error:
            warnings.append({"path": path, "reason": f"Cannot parse Python manifest: {error}"})
    elif name.startswith("requirements") and name.endswith(".txt"):
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-", ".")):
                continue
            dependency = re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0]
            evidence_item(consumes, dependency, "python-dependency", path)
    elif name == "pom.xml":
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            warnings.append({"path": path, "reason": f"Cannot parse Maven manifest: {error}"})
            return
        def tag(element):
            return element.tag.rsplit("}", 1)[-1]
        direct = {tag(child): child.text or "" for child in root}
        evidence_item(produces, direct.get("artifactId"), "maven-artifact", path,
                      direct.get("version", ""))
        for element in root.iter():
            if tag(element) == "dependency":
                fields = {tag(child): child.text or "" for child in element}
                evidence_item(consumes, fields.get("artifactId"), "maven-dependency", path,
                              fields.get("version", ""))
    elif name in ("build.gradle", "build.gradle.kts"):
        for dependency, version in re.findall(r"""["'][\w.-]+:([\w.-]+):([^"']+)["']""", text):
            evidence_item(consumes, dependency, "gradle-dependency", path, version)
    elif name in ("conanfile.txt", "conanfile.yaml", "conanfile.yml"):
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "[")) or line.endswith(":"):
                continue
            dependency = re.split(r"[<>=!~;/\s]", line, maxsplit=1)[0]
            evidence_item(consumes, dependency, "conan-dependency", path)
    elif name == "conanfile.py":
        package = re.search(r"""(?m)^\s*name\s*=\s*["']([^"']+)""", text)
        if package:
            evidence_item(produces, package.group(1), "conan-package", path)
        for dependency in re.findall(r"""self\.(?:requires|tool_requires)\(\s*["']([^/"']+)""", text):
            evidence_item(consumes, dependency, "conan-dependency", path)
    elif name == "cmakelists.txt":
        for target in re.findall(r"(?i)(?:add_library|add_executable)\s*\(\s*([^\s)]+)", text):
            if "$" not in target:
                evidence_item(produces, target, "cmake-target", path)
        for dependency in re.findall(r"(?i)find_package\s*\(\s*([^\s)]+)", text):
            if "$" not in dependency:
                evidence_item(consumes, dependency, "cmake-dependency", path)
    elif name == "android.bp":
        for module in re.findall(r"""(?m)^\s*name:\s*["']([^"']+)""", text):
            evidence_item(produces, module, "android-module", path)
        for block in re.findall(r"(?s)(?:shared_libs|static_libs|libs):\s*\[(.*?)\]", text):
            for dependency in re.findall(r"""["']([^"']+)["']""", block):
                evidence_item(consumes, dependency, "android-module-dependency", path)
    elif name == "go.mod":
        module = re.search(r"(?m)^module\s+(\S+)", text)
        if module:
            evidence_item(produces, module.group(1), "go-module", path)
        for dependency, version in re.findall(r"(?m)^\s*([^\s()]+)\s+(v[^\s]+)", text):
            evidence_item(consumes, dependency, "go-dependency", path, version)
    elif name == "cargo.toml":
        try:
            cargo = tomllib.loads(text)
            package = cargo.get("package", {})
            if isinstance(package, dict) and package.get("name"):
                evidence_item(produces, package["name"], "cargo-crate", path,
                              package.get("version", ""))
            for dependency, version in cargo.get("dependencies", {}).items():
                evidence_item(consumes, dependency, "cargo-dependency", path,
                              version if isinstance(version, str) else "")
        except (tomllib.TOMLDecodeError, AttributeError) as error:
            warnings.append({"path": path, "reason": f"Cannot parse Cargo manifest: {error}"})
    elif name == "dockerfile" or name.endswith(".dockerfile"):
        for image in re.findall(r"(?im)^\s*FROM\s+([^\s]+)", text):
            evidence_item(consumes, image, "container-base", path)


def parse_ci(path, content, produces, consumes):
    if len(content) > 1_000_000:
        return
    text = content.decode("utf-8", errors="replace")
    if path.rsplit("/", 1)[-1].casefold().startswith("jenkinsfile"):
        for artifact in re.findall(r"""(?i)archiveArtifacts\s*(?:artifacts\s*:\s*)?["']([^"']+)""", text):
            evidence_item(produces, artifact, "ci-archived-artifact", path)
        for job in re.findall(r"""(?i)\bbuild\s*\(\s*job\s*:\s*["']([^"']+)""", text):
            evidence_item(consumes, job, "jenkins-job", path)
    if path.startswith(".github/workflows/"):
        for action in re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", text):
            if not action.startswith(("./", "docker://")):
                evidence_item(consumes, action, "github-action", path)
        for direction, group in (("upload", produces), ("download", consumes)):
            for match in re.finditer(r"actions/" + direction + r"-artifact@[^\n]*\n((?:\s+[^\n]*\n){0,12})", text):
                named = re.search(r"(?m)^\s*name:\s*([^\n#]+)", match.group(1))
                if named:
                    evidence_item(group, named.group(1).strip(" '\""), "github-artifact", path)


def ai_change(checkout, head, path, blob_sha, content):
    history = git_text(checkout, "log", "-1", "--format=%H%x09%cI", head, "--",
                       f":(literal){path}")
    file = {
        "path": path,
        "kind": kind_for(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "last_changed": None,
        "status": "unknown",
        "evidence": {"source": "local_git", "head_sha": head, "blob_sha": blob_sha},
    }
    if history:
        commit, changed = history.split("\t", 1)
        if not SHA.fullmatch(commit):
            raise ScanError(f"Invalid AI-file history commit for {path}")
        file["last_changed"] = iso(timestamp(changed))
        file["status"] = "known"
        file["evidence"]["last_commit_sha"] = commit
    else:
        file["unknown_reason"] = "No path commit history returned at the pinned HEAD"
    return file


def scan_local_static(checkout, head):
    files = tracked_blobs(checkout, head)
    languages, language_lines, extensions = Counter(), Counter(), Counter()
    ai_files, adrs, manifests, produces, consumes, warnings = [], [], [], [], [], []
    legacy_lines = 0
    summary = ""
    title = checkout.name
    readmes = [item[0] for item in files if "/" not in item[0] and item[0].casefold().startswith("readme")]
    preferred_readme = min(readmes, key=len) if readmes else None
    readme_content = b""
    with blob_reader(checkout) as read_blob:
        for path, blob_sha, size in files:
            name = path.rsplit("/", 1)[-1].casefold()
            suffix = Path(path).suffix.casefold()
            extensions[suffix or "(none)"] += 1
            ai_kind = kind_for(path)
            is_manifest = (name in MANIFESTS or
                           name.startswith("requirements") and name.endswith(".txt") or
                           name.endswith(".dockerfile"))
            is_ci = path.startswith(".github/workflows/") or name.startswith("jenkinsfile")
            read_content = ai_kind or suffix in LANGUAGES or is_manifest or is_ci or path == preferred_readme
            content = read_blob(blob_sha) if read_content else b""
            if suffix in LANGUAGES:
                language = LANGUAGES[suffix]
                languages[language] += 1
                language_lines[language] += physical_lines(content)
                if len(content) <= 500_000 and content:
                    legacy_lines += content.decode("utf-8", errors="ignore").replace(
                        "\r\n", "\n"
                    ).replace("\r", "\n").count("\n") + 1
            if ai_kind:
                ai_files.append(ai_change(checkout, head, path, blob_sha, content))
            lower_path = path.casefold()
            if suffix in (".md", ".mdx", ".rst", ".txt") and (
                re.search(r"(^|[-_/])adr([-_/.]|$)", lower_path) or
                any(part in ("decisions", "architecture-decision-records") for part in lower_path.split("/"))
            ):
                adrs.append(path)
            if is_manifest:
                manifests.append(path)
                if not any(part in ("test", "tests", "fixtures", "examples", "samples")
                           for part in lower_path.split("/")):
                    parse_manifest(path, content, produces, consumes, warnings)
            if is_ci:
                parse_ci(path, content, produces, consumes)
            if path == preferred_readme:
                readme_content = content[:2_000_000]
    if preferred_readme:
        text = readme_content.decode("utf-8", errors="replace")
        title = next((line.lstrip("# ").strip() for line in text.splitlines()
                      if line.startswith("# ")), title)[:120]
        summary = next((part.replace("\n", " ").strip() for part in re.split(r"\n\s*\n", text)
                        if len(part.strip()) > 45 and not part.lstrip().startswith(("#", "![", "<!--"))), "")[:420]
    sorted_languages = sorted(languages, key=lambda key: (-languages[key], key))
    return {
        "title": title, "summary": summary,
        "repo_type": (sorted_languages[0] + " repository") if sorted_languages else "General repository",
        "metrics": {
            "files": len(files), "bytes": sum(size for _, _, size in files),
            "source_loc": sum(language_lines.values()),
            "estimated_code_lines": legacy_lines,
        },
        "languages": [
            {"name": language, "files": languages[language], "loc": language_lines[language]}
            for language in sorted_languages
        ],
        "extensions": [
            {"name": suffix, "files": count} for suffix, count in
            sorted(extensions.items(), key=lambda pair: (-pair[1], pair[0]))[:12]
        ],
        "architecture": {"adr_count": len(adrs), "adr_files": sorted(adrs)},
        "manifests": sorted(manifests),
        "produces": sorted(produces, key=lambda item: (item["name"], item["source"])),
        "consumes": sorted(consumes, key=lambda item: (item["name"], item["source"])),
        "analysis_warnings": sorted(warnings, key=lambda item: item["path"]),
        "ai_files": sorted(ai_files, key=lambda item: item["path"]),
    }


def git_activity(checkout, head, as_of):
    since = iso(as_of.replace(hour=0, minute=0, second=0) -
                timedelta(days=89))
    until = iso(as_of)
    dates = git_text(checkout, "log", "--format=%cI", f"--since={since}",
                     f"--until={until}", head).splitlines()
    days = Counter(iso(timestamp(date))[:10] for date in dates if date)
    trend = [
        days.get((as_of.date() - timedelta(days=offset)).isoformat(), 0)
        for offset in range(89, -1, -1)
    ]
    names = git_text(checkout, "log", "--format=", "--name-only", f"--since={since}",
                     f"--until={until}", head).splitlines()
    hotspots = Counter(path for path in names if path.strip())
    authors = git_text(checkout, "log", "--format=%aE", head).splitlines()
    count = git_text(checkout, "rev-list", "--count", head)
    return {
        "branch": git_text(checkout, "symbolic-ref", "--short", "HEAD", optional=True) or "detached",
        "commit_count": int(count),
        "commits_30d": sum(trend[-30:]),
        "commits_90d": sum(trend),
        "commits_90d_trend": trend,
        "contributor_count": len({author.casefold() for author in authors if author}),
        "hotspots_90d": [
            {"path": path, "changes": changes} for path, changes in
            sorted(hotspots.items(), key=lambda item: (-item[1], item[0]))[:8]
        ],
    }


def scan_local_repo(root, name, as_of, stale_after, cache):
    checkout = local_checkout(root, name)
    head = git_text(checkout, "rev-parse", "HEAD")
    if not SHA.fullmatch(head):
        raise ScanError(f"Invalid HEAD SHA for required clone {name}")
    head_date = timestamp(git_text(checkout, "show", "-s", "--format=%cI", head))
    if head_date > as_of:
        raise ScanError(f"Pinned HEAD for {name} is newer than --as-of")
    key = "local:" + name.casefold() + "@" + head
    static = cache["entries"].get(key)
    if static is None:
        static = scan_local_static(checkout, head)
        cache["entries"][key] = static
    elif not isinstance(static, dict) or not {
        "ai_files", "metrics", "languages", "extensions", "architecture",
        "manifests", "produces", "consumes", "analysis_warnings",
        "title", "summary", "repo_type"
    }.issubset(static):
        raise ScanError(f"Invalid cached local scan for {name} at {head}")
    else:
        validate_cached_files(static["ai_files"], name, head, "local_git")
    files = refresh_ages(static["ai_files"], as_of, head_date, stale_after)
    return {
        "full_name": name, "head_sha": head, "head_committed_at": iso(head_date),
        "ai_files": files, "ai_summary": summarize_files(files),
        "git": git_activity(checkout, head, as_of),
        **{key: value for key, value in static.items() if key != "ai_files"},
    }


def match_edges(repositories):
    producers = {}
    for repo in repositories:
        names = [(repo["full_name"].split("/")[-1], "repository", "repository name")]
        names.extend((item["name"], item["kind"], item["source"]) for item in repo.get("produces", []))
        for name, kind, source in names:
            key = re.sub(r"[^a-z0-9]", "", name.split("/")[-1].casefold())
            if len(key) >= 4:
                producers.setdefault(key, []).append((repo["full_name"], kind, source, name))
    edges = {}
    for repo in repositories:
        for item in repo.get("consumes", []):
            artifact = item["name"].split("/")[-1].split("@")[0]
            key = re.sub(r"[^a-z0-9]", "", artifact.casefold())
            if len(key) < 4:
                continue
            for target, kind, source, produced in producers.get(key, []):
                if target == repo["full_name"]:
                    continue
                edge = (repo["full_name"], target)
                evidence = {
                    "consumer_file": item["source"], "consumed": item["name"],
                    "producer_file": source, "produced": produced,
                }
                matching = edges.setdefault(edge, {
                    "source": repo["full_name"], "target": target,
                    "kind": "artifact dependency", "confidence": "medium",
                    "evidence": [],
                })
                if evidence not in matching["evidence"]:
                    matching["evidence"].append(evidence)
    return [
        {"source": source, "target": target, "kind": "artifact dependency",
         "confidence": edges[(source, target)]["confidence"],
         "evidence": sorted(edges[(source, target)]["evidence"],
                            key=lambda item: (item["consumer_file"], item["producer_file"]))}
        for source, target in sorted(edges)
    ]


def validate_baseline(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ScanError("Baseline must be a landscape JSON v1 object")
    timestamp(value.get("generated_at"))
    repos = value.get("repositories")
    if not isinstance(repos, list):
        raise ScanError("Baseline repositories must be an array")
    if not isinstance(value.get("edges"), list):
        raise ScanError("Baseline edges must be an array")
    names = set()
    for repo in repos:
        if not isinstance(repo, dict):
            raise ScanError("Invalid baseline repository")
        name = validate_name(repo.get("full_name"), "Baseline repository")
        if name.casefold() in names:
            raise ScanError(f"Duplicate baseline repository: {name}")
        names.add(name.casefold())
        files = repo.get("ai_files")
        if not isinstance(files, list):
            raise ScanError(f"Invalid baseline AI files for {name}")
        paths = set()
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get("path"), str) or not file["path"]:
                raise ScanError(f"Invalid baseline AI file for {name}")
            if file["path"] in paths:
                raise ScanError(f"Duplicate baseline AI file: {name}/{file['path']}")
            paths.add(file["path"])
            if not isinstance(file.get("sha256"), str) or not SHA256.fullmatch(file["sha256"]):
                raise ScanError(f"Invalid baseline SHA-256 for {name}/{file['path']}")
            if file.get("status") not in ("known", "unknown"):
                raise ScanError(f"Invalid baseline file status for {name}/{file['path']}")
    return {repo["full_name"].casefold(): repo for repo in repos}


def compare_baseline(current, baseline, current_edges):
    previous = validate_baseline(baseline)
    current_by_name = {repo["full_name"].casefold(): repo for repo in current}
    changes = []
    metrics = {
        "files": ("metrics", "files"),
        "bytes": ("metrics", "bytes"),
        "source_loc": ("metrics", "source_loc"),
        "commits_total": ("git", "commit_count"),
        "commits_30d": ("git", "commits_30d"),
        "commits_90d": ("git", "commits_90d"),
        "adrs": ("architecture", "adr_count"),
        "ai_files": ("ai_summary", "count"),
        "stale_ai_files": ("ai_summary", "stale_count"),
        "max_lag_days": ("ai_summary", "max_lag_days"),
    }
    for key in sorted(previous.keys() | current_by_name.keys()):
        before = previous.get(key)
        after = current_by_name.get(key)
        old_files = {file["path"]: file for file in before["ai_files"]} if before else {}
        new_files = {file["path"]: file for file in after["ai_files"]} if after else {}
        old_paths, new_paths = old_files.keys(), new_files.keys()
        added = sorted(new_paths - old_paths)
        removed = sorted(old_paths - new_paths)
        changed, unchanged, unknown = [], [], []
        for path in sorted(old_paths & new_paths):
            old, new = old_files[path], new_files[path]
            if old["status"] == "unknown" or new["status"] == "unknown":
                unknown.append(path)
            elif old["sha256"] != new["sha256"]:
                changed.append(path)
            else:
                unchanged.append(path)
        metric_changes = {}
        if before and after:
            for label, (section, field) in metrics.items():
                previous_value = before.get(section, {}).get(field)
                current_value = after.get(section, {}).get(field)
                if (type(previous_value) in (int, float) and
                        type(current_value) in (int, float) and previous_value != current_value):
                    metric_changes[label] = {
                        "baseline": previous_value, "current": current_value,
                        "delta": current_value - previous_value,
                    }
            if before.get("head_sha") != after.get("head_sha"):
                metric_changes["head_sha"] = {
                    "baseline": before.get("head_sha"), "current": after.get("head_sha"),
                }
        status = ("new" if before is None else "removed" if after is None else
                  "unknown" if unknown else
                  "changed" if added or removed or changed or metric_changes else "unchanged")
        changes.append({
            "full_name": after["full_name"] if after else before["full_name"],
            "status": status,
            "added_files": added,
            "removed_files": removed,
            "changed_files": changed,
            "unchanged_files": unchanged,
            "unknown_files": unknown,
            "metric_changes": metric_changes,
            "ai_readiness": {
                "baseline": before.get("ai_summary") if before else None,
                "current": after.get("ai_summary") if after else None,
            },
        })
    def edge_keys(values):
        keys = set()
        for edge in values:
            if not isinstance(edge, dict) or not all(isinstance(edge.get(field), str)
                                                      for field in ("source", "target", "kind")):
                raise ScanError("Invalid baseline dependency edge")
            keys.add((edge["source"], edge["target"], edge["kind"]))
        return keys
    prior_edges = edge_keys(baseline["edges"])
    new_edges = edge_keys(current_edges)
    return {
        "baseline_generated_at": iso(timestamp(baseline["generated_at"])),
        "repositories": changes,
        "summary": {
            "repositories_new": sum(row["status"] == "new" for row in changes),
            "repositories_removed": sum(row["status"] == "removed" for row in changes),
            "repositories_changed": sum(row["status"] == "changed" for row in changes),
            "repositories_unchanged": sum(row["status"] == "unchanged" for row in changes),
            "repositories_unknown": sum(row["status"] == "unknown" for row in changes),
            "edges_added": len(new_edges - prior_edges),
            "edges_removed": len(prior_edges - new_edges),
        },
        "edges": {
            "added": [dict(zip(("source", "target", "kind"), edge)) for edge in sorted(new_edges - prior_edges)],
            "removed": [dict(zip(("source", "target", "kind"), edge)) for edge in sorted(prior_edges - new_edges)],
        },
    }


def write_atomic(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".repo-landscape-", dir=path.parent,
                                         delete=False) as target:
            temporary = Path(target.name)
            target.write(contents)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def different_paths(paths):
    resolved = [path.resolve() for path in paths if path is not None]
    if len(set(resolved)) != len(resolved):
        raise ScanError("Output, cache and baseline/input paths must be distinct")


def run_scan(args, api=None):
    output = Path(args.output)
    cache_path = Path(args.cache) if args.cache else None
    baseline_path = Path(args.baseline) if args.baseline else None
    heads_path = Path(args.expected_heads) if args.expected_heads else None
    different_paths([output, cache_path, baseline_path, heads_path, Path(args.config)])
    explicit, discovery, stale_after = config_from(args.config)
    as_of = timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    as_of = as_of.replace(microsecond=0)
    api = api if api is not None else GithubApi()
    names, selection = select_repositories(explicit, discovery, api)
    cache = read_cache(cache_path)
    local_root = Path(args.repos_dir) if args.repos_dir else None
    if local_root is not None and not local_root.is_dir():
        raise ScanError(f"Local repositories directory does not exist: {local_root}")
    if heads_path is not None:
        if local_root is None:
            raise ScanError("--expected-heads requires --repos-dir")
        heads = read_json(heads_path)
        if not isinstance(heads, dict) or set(heads) != set(names) or not all(
            isinstance(value, str) and SHA.fullmatch(value) for value in heads.values()
        ):
            raise ScanError("Expected heads must map each selected owner/repo to a 40-character HEAD SHA")
        for name in names:
            actual = git_text(local_checkout(local_root, name), "rev-parse", "HEAD")
            if heads[name] != actual:
                raise ScanError(f"Required clone for {name} has a HEAD mismatch with expected heads")
    if local_root is not None:
        repositories = [
            scan_local_repo(local_root, name, as_of, stale_after, cache) for name in names
        ]
    else:
        repositories = [scan_repo(name, as_of, stale_after, cache, api) for name in names]
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(as_of),
        "scanner_version": VERSION,
        "provenance": {
            "source": "local_git" if local_root is not None else "github_api",
            "snapshot": "pinned_head",
            "analysis": "full" if local_root is not None else "ai_only",
            "stale_after_days": stale_after,
        },
        "selection": selection,
        "repositories": repositories,
        "edges": match_edges(repositories),
    }
    if baseline_path is not None:
        result["comparison"] = compare_baseline(repositories, read_json(baseline_path), result["edges"])
    if cache_path is not None:
        write_atomic(cache_path, json.dumps(cache, indent=2, ensure_ascii=True) + "\n")
    write_atomic(output, json.dumps(result, indent=2, ensure_ascii=True) + "\n")
    return result


def run_report(args):
    source, output = Path(args.input), Path(args.output)
    different_paths([source, output])
    data = read_json(source)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ScanError("Report input must be a landscape JSON v1 object")
    validate_baseline(data)
    template = (Path(__file__).resolve().parent.parent / "assets" / "report.html").read_text(
        encoding="utf-8"
    )
    if template.count("__LANDSCAPE_DATA__") != 1:
        raise ScanError("Bundled report template is missing its data slot")
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace("&", "\\u0026")
    write_atomic(output, template.replace("__LANDSCAPE_DATA__", payload))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="repo-landscape", description=__doc__)
    parser.add_argument("--version", action="version", version=f"repo-landscape {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Scan selected GitHub repositories")
    scan.add_argument("--config", required=True, help="Explicit JSON configuration")
    scan.add_argument("--output", required=True, help="Landscape JSON output")
    scan.add_argument("--baseline", help="Read-only previous landscape JSON v1")
    scan.add_argument("--cache", help="Optional local cache path")
    scan.add_argument("--as-of", help="UTC ISO-8601 time for deterministic age calculation")
    scan.add_argument("--repos-dir", help="Local full-history Git checkouts under owner/repo or repo")
    scan.add_argument("--expected-heads", help="JSON mapping selected owner/repo names to expected local HEAD SHAs")
    report = commands.add_parser("report", help="Render an offline HTML report")
    report.add_argument("--input", required=True, help="Landscape JSON v1 input")
    report.add_argument("--output", required=True, help="HTML output")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            run_scan(args)
        else:
            run_report(args)
    except (ScanError, OSError, UnicodeError) as error:
        print(f"repo-landscape: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
