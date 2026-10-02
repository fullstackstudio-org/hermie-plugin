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


def test_the_bundle_placeholder_has_its_final_name_and_says_in_its_step_that_it_is_one():
    job = load(GITHUB / "workflows" / "ci.yml")["jobs"]["web-bundle-verify"]

    assert job["name"] == "web-bundle-verify"
    named = [s["name"] for s in job["steps"] if "run" in s]
    assert any("Placeholder" in n and "not yet active" in n for n in named)


def test_the_bundle_placeholder_refuses_a_bundle_even_as_a_dangling_symlink():
    job = load(GITHUB / "workflows" / "ci.yml")["jobs"]["web-bundle-verify"]
    script = " ".join(s.get("run", "") for s in job["steps"])

    assert "[ -e dashboard/app ] || [ -L dashboard/app ]" in script and "exit 1" in script


def test_the_bundle_placeholder_really_fails_on_a_bundle_and_on_a_dangling_symlink(tmp_path):
    import os
    import subprocess

    script = next(s["run"] for s in load(GITHUB / "workflows" / "ci.yml")["jobs"]["web-bundle-verify"]["steps"] if "run" in s)

    def run(tree):
        return subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=tree, capture_output=True, text=True).returncode

    clean = tmp_path / "clean"
    clean.mkdir()
    assert run(clean) == 0

    folder = tmp_path / "folder"
    (folder / "dashboard" / "app").mkdir(parents=True)
    assert run(folder) == 1

    dangling = tmp_path / "dangling"
    (dangling / "dashboard").mkdir(parents=True)
    os.symlink(tmp_path / "nowhere", dangling / "dashboard" / "app")
    assert run(dangling) == 1


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
