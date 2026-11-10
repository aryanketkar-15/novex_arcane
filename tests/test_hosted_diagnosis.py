from unittest.mock import MagicMock

import pytest

from agents import analyst_agent, github_diagnostics as diagnostics, orchestrator, patch_generator, pr_agent
from agents.validator_agent import validate_hosted_static

SHA = "339ee46b7e115dcd6e149b8d0f6dc84989a5e4f8"
PATH = "src/auth.py"


def test_streamed_job_trace(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"2026-01-01T00:00:00Z Traceback\n", b"src/auth.py:12: AssertionError\nFAILED tests/test_auth.py::test_token"]
    get = MagicMock(return_value=response)
    monkeypatch.setattr(diagnostics.requests, "get", get)
    log = diagnostics.job_diagnostics("team/demo", 17, "user-token")
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer user-token"
    assert get.call_args.kwargs["stream"]
    assert all(line in log for line in ("Traceback", "AssertionError", "src/auth.py:12", "tests/test_auth.py::test_token"))


@pytest.mark.parametrize("log,uncertain", [("Job 17 logs unavailable", True),
    ("FAILED tests/test_auth.py::test_token\nsrc/auth.py:12: AssertionError", False)])
def test_historical_analysis_and_patch(monkeypatch, log, uncertain):
    repo = MagicMock()
    repo.get_commit.return_value.files = [MagicMock(filename=PATH, status="modified", patch="-limit=10\n+limit=40")]
    repo.get_contents.return_value = MagicMock(size=40, decoded_content=b"assert valid('long-enough-token')")
    monkeypatch.setattr(diagnostics, "Github", lambda *a, **kw: MagicMock(get_repo=lambda name: repo))
    monkeypatch.setenv("ARCANE_EXECUTION_MODE", "hosted")
    monkeypatch.setattr(analyst_agent, "call_llm", lambda **kw: '{"failing_file":"unknown"}')
    state = analyst_agent.analyze(dict(repo_full_name="team/demo", commit_sha=SHA, failure_log=log, execution_mode="hosted"))
    assert state["failing_file"] == PATH
    assert ("Uncertain" in state["root_cause_summary"]) == uncertain
    if not uncertain:
        assert state["failing_test"] == "tests/test_auth.py::test_token"
    assert all(call.kwargs["ref"] == SHA for call in repo.get_contents.call_args_list)
    source = MagicMock(return_value="limit=40\n")
    monkeypatch.setattr(pr_agent, "get_file_content", source)
    model = MagicMock(return_value="limit=10\n")
    monkeypatch.setattr(patch_generator, "call_llm", model)
    result = patch_generator.generate_patch(state)
    source.assert_called_once_with("team/demo", PATH, SHA)
    assert "assert valid" in model.call_args.kwargs["prompt"]
    assert "-limit=40\n+limit=10" in result["patch_diff"]
    assert validate_hosted_static(result)["test_verification"] == "NOT_VERIFIED"
    model.return_value = "limit=40\n"
    unchanged = patch_generator.generate_patch(state)
    assert not unchanged["patch_diff"]
    assert orchestrator.after_patching(unchanged) == "escalated"


def test_missing_target_stops_without_patch_attempt(monkeypatch):
    monkeypatch.setattr(diagnostics, "historical_context", lambda *args: {"candidates": []})
    result = analyst_agent.analyze(dict(repo_full_name="team/demo", commit_sha=SHA, execution_mode="hosted"))
    assert orchestrator.after_analyzing(result) == "escalated"
    assert "No Python repair target" in orchestrator.escalated_node(result)["error"]


def test_draft_rejects_changed_base_and_requires_api_confirmation(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "unit-test-token")
    monkeypatch.setenv("ARCANE_DRY_RUN", "0")
    repo = MagicMock()
    monkeypatch.setattr(pr_agent, "Github", lambda _: MagicMock(get_repo=lambda name: repo))
    state = dict(repo_full_name="team/demo", commit_sha=SHA, execution_mode="hosted",
                 validation_status="SKIPPED", failing_file=PATH, patched_content="limit=10\n",
                 patch_diff=patch_generator.source_diff(PATH, "limit=40\n", "limit=10\n"))
    old = MagicMock(sha="old", decoded_content=b"limit=40\n")
    repo.get_contents.side_effect = [old, MagicMock(sha="new")]
    with pytest.raises(ValueError, match="changed since"):
        orchestrator.creating_pr_node(state)
    repo.create_git_ref.assert_not_called()
    repo.get_contents.side_effect = None
    repo.get_contents.return_value = old
    repo.get_git_ref.return_value.object.sha = "base-sha"
    repo.get_git_commit.return_value.tree.tree = [MagicMock(path="src", type="tree")]
    repo.get_git_tree.return_value.tree = [MagicMock(path="auth.py", mode="100755", sha="old")]
    repo.create_git_tree.return_value.sha = "tree-sha"
    repo.create_pull.return_value = None
    with pytest.raises(RuntimeError, match="valid PR"):
        orchestrator.creating_pr_node(state)
    assert repo.create_pull.call_args.kwargs["draft"]
    element = repo.create_git_tree.call_args.args[0][0]
    assert element._identity["mode"] == "100755"
    assert element._identity["path"] == PATH
    assert element._identity["content"] == "limit=10\n"

def test_submitted_workflow_uses_user_token_and_keeps_sha_when_logs_expire(monkeypatch):
    import github
    import requests
    from api import routes

    repo = "team/demo"
    job = MagicMock(id=17, conclusion="failure", steps=[])
    job.name = "unit tests"
    workflow = MagicMock(head_sha=SHA, head_branch="release", conclusion="failure", head_commit=None)
    workflow.jobs.return_value = [job]
    client = MagicMock()
    client.get_repo.return_value.get_workflow_run.return_value = workflow
    monkeypatch.setattr(github, "Github", lambda **kwargs: client)
    fetch = MagicMock(side_effect=requests.HTTPError("expired"))
    monkeypatch.setattr(diagnostics, "job_diagnostics", fetch)
    enqueue = MagicMock(return_value={"id": "queued"})
    monkeypatch.setattr(routes, "enqueue_event", enqueue)
    body = routes.NewRun(repo=repo, workflowRun="https://github.com/team/demo/actions/runs/123")
    routes.create_run(body, MagicMock(), {"login": "alice", "token": "user-token"})
    client.get_repo.return_value.get_workflow_run.assert_called_once_with(123)
    fetch.assert_called_once_with(repo, 17, "user-token")
    event = enqueue.call_args.args[1]
    assert event["commit_sha"] == SHA and event["branch"] == "release"
    assert "log unavailable" in event["failure_log"] and event["owner"] == "alice"


def test_empty_patch_clears_prior_candidate_in_timeline(monkeypatch):
    from api import recorder
    monkeypatch.setattr(recorder.store, "save", lambda: None)
    run = {"patchDiff": "old diff", "steps": [{"key": "patch", "status": "running"}]}
    recorder.apply_node(run, "patching", {"patch_diff": "", "error": "Source unchanged"})
    assert run["patchDiff"] == ""
    assert run["steps"][0]["status"] == "failed"
