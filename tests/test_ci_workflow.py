"""The workflows under `.github/`: what they may and may not do.

This repository deploys from `main`, so its CI is part of the supply chain. The
properties that matter are checked on the files themselves, so a later edit that
quietly gives a job a secret, a floating action or a self-hosted runner fails
here, in the suite everyone already runs.
"""

import re
from pathlib import Path

import pytest
import yaml

GITHUB = Path(__file__).resolve().parent.parent / ".github"
WORKFLOWS = sorted((GITHUB / "workflows").glob("*.yml"))
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 reads the key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def steps(workflow):
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def test_the_workflows_exist():
    assert {p.name for p in WORKFLOWS} == {"ci.yml", "scanner-nightly.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_workflow_can_only_read_the_repository(path):
    workflow = load(path)

    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"{name} widens the permissions"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_workflow_uses_no_secret_and_no_privileged_trigger(path):
    # Comments may say what the workflow refuses; only what it does counts.
    text = re.sub(r"(?m)#.*$", "", path.read_text(encoding="utf-8"))

    assert "secrets." not in text and "secrets[" not in text
    assert "pull_request_target" not in text
    assert "GITHUB_TOKEN" not in text and "github.token" not in text
    assert "workflow_run" not in text


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit(path):
    used = [s["uses"] for s in steps(load(path)) if "uses" in s]

    assert used
    for ref in used:
        action, _, version = ref.partition("@")
        assert FULL_SHA.match(version), f"{ref} is not pinned to a commit"
        assert not action.startswith("./"), "no local action: it would be unreviewed code in the job"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_pinned_action_says_which_release_it_is(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if "uses:" in line and "@" in line:
            assert re.search(r"@[0-9a-f]{40} # v\d+(\.\d+)*$", line.strip()), line


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_runs_on_a_github_hosted_runner_with_a_timeout(path):
    for name, job in load(path)["jobs"].items():
        assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"]), f"{name} runs on {job['runs-on']!r}"
        assert 1 <= job["timeout-minutes"] <= 15, name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_keeps_no_credential_in_the_workspace(path):
    for step in steps(load(path)):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_the_required_checks_are_the_job_names_the_readme_gives():
    ci = load(GITHUB / "workflows" / "ci.yml")

    assert ci["jobs"]["test"]["name"] == "test"
    assert ci["jobs"]["guard-scan"]["name"] == "guard-scan"
    readme = (GITHUB.parent / "README.md").read_text(encoding="utf-8")
    assert "`test`" in readme and "`guard-scan`" in readme


def test_pull_requests_are_never_filtered_by_path():
    """A required check that does not start leaves the pull request unmergeable."""
    triggers = load(GITHUB / "workflows" / "ci.yml")["on"]

    for name in ("pull_request", "push"):
        assert "paths" not in (triggers[name] or {}) and "paths-ignore" not in (triggers[name] or {})


def test_ci_runs_on_pull_requests_and_on_main():
    triggers = load(GITHUB / "workflows" / "ci.yml")["on"]

    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]


def test_the_scan_job_runs_the_script_over_the_checkout_with_the_scanners_elsewhere():
    ci = load(GITHUB / "workflows" / "ci.yml")
    commands = [s["run"] for s in ci["jobs"]["guard-scan"]["steps"] if "run" in s]

    assert any("scripts/guard_scan.py" in c and "$RUNNER_TEMP" in c for c in commands)
    assert not any("--latest" in c for c in commands), "the pinned run must not follow a branch"


def test_the_nightly_run_follows_the_newest_scanners_and_cannot_hold_a_merge():
    nightly = load(GITHUB / "workflows" / "scanner-nightly.yml")
    commands = [s["run"] for s in steps(nightly) if "run" in s]

    assert "schedule" in nightly["on"] and "pull_request" not in nightly["on"]
    assert any("--latest" in c for c in commands)


APP_REPO = "fullstackstudio-org/hermie"


def bundle_job():
    return load(GITHUB / "workflows" / "ci.yml")["jobs"]["web-bundle-verify"]


def bundle_step(job, needle):
    """The one step of the bundle job whose name contains `needle`, with its index."""
    found = [(i, s) for i, s in enumerate(job["steps"]) if needle in s.get("name", "")]
    assert len(found) == 1, needle
    return found[0]


def test_the_bundle_job_keeps_the_name_branch_protection_requires():
    job = bundle_job()

    assert job["name"] == "web-bundle-verify"
    assert not any("Placeholder" in s.get("name", "") for s in job["steps"])


def test_the_bundle_job_trusts_no_source_build_json_names_but_one():
    """build.json is written by the pull request, so its `sourceRepo` is an input.

    A bundle naming a repository of its author's choosing would pass by rebuilding
    itself; the app repository is fixed in the job and the named one compared with it.
    """
    job = bundle_job()
    _, source = bundle_step(job, "read its source")
    checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    app = next(s for s in checkouts if s["with"].get("path") == "app")

    assert job["env"]["APP_REPO"] == APP_REPO
    assert '[ "$repo" != "$APP_REPO" ]' in source["run"]
    assert app["with"]["repository"] == "${{ env.APP_REPO }}"
    assert app["with"]["ref"] == "${{ steps.source.outputs.commit }}"
    assert "token" not in app["with"], "a public repository needs no credential of ours"
    assert app["with"]["persist-credentials"] is False


def test_the_bundle_job_puts_no_value_from_build_json_into_a_script():
    """Values reach a script through `env`, never through `${{ }}` in its text."""
    for step in bundle_job()["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_bundle_job_refuses_unmerged_source_before_it_builds_anything():
    job = bundle_job()
    ancestry_at, ancestry = bundle_step(job, "not on the app's main")
    build_at, build = bundle_step(job, "Rebuild")
    node_at = next(i for i, s in enumerate(job["steps"]) if s.get("uses", "").startswith("actions/setup-node@"))

    assert "git fetch" in ancestry["run"] and "refs/heads/main" in ancestry["run"]
    assert "merge-base --is-ancestor" in ancestry["run"]
    assert ancestry["run"].index("git fetch") < ancestry["run"].index("merge-base")
    assert ancestry_at < node_at < build_at
    assert "npm ci" in build["run"] and "npm run client:build" in build["run"]


def test_the_bundle_job_builds_with_the_node_the_app_pins_and_no_cache():
    node = next(s for s in bundle_job()["steps"] if s.get("uses", "").startswith("actions/setup-node@"))

    assert node["with"]["node-version-file"] == "app/.nvmrc"
    assert node["with"]["package-manager-cache"] is False
    assert "node-version" not in node["with"]


def test_the_bundle_job_compares_every_file_build_json_included():
    _, compare = bundle_step(bundle_job(), "Compare")

    assert "diff -r" in compare["run"] and "--no-dereference" in compare["run"]
    assert "app/native/web/dist plugin/dashboard/app" in compare["run"]
    assert "--exclude" not in compare["run"] and "-x " not in compare["run"]


def test_every_bundle_step_after_the_source_runs_only_when_there_is_a_bundle():
    steps = bundle_job()["steps"]
    source_at, _ = bundle_step(bundle_job(), "read its source")

    for step in steps[source_at + 1 :]:
        assert step["if"] == "steps.source.outputs.present == 'true'", step.get("name", step.get("uses"))


def _plugin_tree(tmp_path):
    """The two files the source step runs, in a tree of their own."""
    import shutil

    tree = tmp_path / "plugin"
    (tree / "scripts").mkdir(parents=True)
    (tree / "dashboard").mkdir()
    shutil.copy(GITHUB.parent / "web.py", tree / "web.py")
    shutil.copy(GITHUB.parent / "scripts" / "import_web_client.py", tree / "scripts" / "import_web_client.py")
    return tree


def _write_bundle(app, repo=APP_REPO, commit="ab" * 20):
    import hashlib
    import json

    files = {"index.html": b"<!doctype html>\n"}
    app.mkdir(parents=True)
    (app / "index.html").write_bytes(files["index.html"])
    manifest = {
        "v": 1,
        "name": "hermie-web-client",
        "version": "0.2.0",
        "sourceRepo": repo,
        "sourceCommit": commit,
        "files": {n: {"sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in files.items()},
        "totalBytes": sum(len(d) for d in files.values()),
    }
    (app / "build.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _run_step(script, cwd, tmp_path, **env):
    import os
    import subprocess

    output = tmp_path / "github_output"
    output.write_text("")
    environment = {**os.environ, "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(tmp_path), **env}
    done = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=cwd, env=environment, capture_output=True, text=True)
    return done.returncode, output.read_text(), done.stdout + done.stderr


def test_the_source_step_really_reads_a_bundle_and_refuses_a_foreign_or_damaged_one(tmp_path):
    import os

    job = bundle_job()
    _, source = bundle_step(job, "read its source")
    env = {"APP_REPO": job["env"]["APP_REPO"]}

    none = _plugin_tree(tmp_path / "none")
    assert _run_step(source["run"], none, tmp_path, **env)[:2] == (0, "present=false\n")

    good = _plugin_tree(tmp_path / "good")
    _write_bundle(good / "dashboard" / "app")
    code, output, _ = _run_step(source["run"], good, tmp_path, **env)
    assert (code, output) == (0, "present=true\ncommit=" + "ab" * 20 + "\n")

    foreign = _plugin_tree(tmp_path / "foreign")
    _write_bundle(foreign / "dashboard" / "app", repo="someone-else/hermie")
    code, output, log = _run_step(source["run"], foreign, tmp_path, **env)
    assert code == 1 and "present=true" not in output and "must be built from" in log

    damaged = _plugin_tree(tmp_path / "damaged")
    _write_bundle(damaged / "dashboard" / "app")
    (damaged / "dashboard" / "app" / "index.html").write_bytes(b"<!doctype html>\r")
    assert _run_step(source["run"], damaged, tmp_path, **env)[0] == 1

    dangling = _plugin_tree(tmp_path / "dangling")
    os.symlink(tmp_path / "nowhere", dangling / "dashboard" / "app")
    assert _run_step(source["run"], dangling, tmp_path, **env)[0] == 1


def test_the_ancestry_step_really_refuses_a_commit_that_is_not_on_main(tmp_path):
    """Against a local stand-in for the app repository: one commit on main, one beside it."""
    import subprocess

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=work)
    git("commit", "-q", "--allow-empty", "-m", "on main", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    merged = git("rev-parse", "HEAD", cwd=work)
    git("commit", "-q", "--allow-empty", "-m", "beside main", cwd=work)
    git("push", "-q", "origin", "HEAD:refs/heads/side", cwd=work)
    unmerged = git("rev-parse", "HEAD", cwd=work)

    _, ancestry = bundle_step(bundle_job(), "not on the app's main")

    app = tmp_path / "app"
    git("clone", "-q", str(origin), str(app), cwd=tmp_path)
    git("checkout", "-q", "--detach", merged, cwd=app)
    code, _, log = _run_step(ancestry["run"], app, tmp_path, COMMIT=merged, APP_REPO=APP_REPO)
    assert code == 0, log

    git("checkout", "-q", "--detach", unmerged, cwd=app)
    code, _, log = _run_step(ancestry["run"], app, tmp_path, COMMIT=unmerged, APP_REPO=APP_REPO)
    assert code == 1 and "not on the main branch" in log

    code, _, log = _run_step(ancestry["run"], app, tmp_path, COMMIT=merged, APP_REPO=APP_REPO)
    assert code == 1 and "not at" in log, "the checkout must be at the commit build.json names"


def test_the_readme_lists_every_job_of_ci_as_a_required_check():
    readme = (GITHUB.parent / "README.md").read_text(encoding="utf-8")
    block = readme[readme.index("### Branch protection"):]

    for job in load(GITHUB / "workflows" / "ci.yml")["jobs"].values():
        assert f'"{job["name"]}"' in block, job["name"]


def test_the_test_job_installs_pinned_packages_only():
    for line in (GITHUB / "requirements-ci.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", line), line


def test_the_pull_request_template_carries_both_checklists():
    text = (GITHUB / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")

    assert "dashboard/app" in text and "build.json" in text
    assert text.count("- [ ]") >= 8
