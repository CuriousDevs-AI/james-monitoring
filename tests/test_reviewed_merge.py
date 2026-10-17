"""Only the reviewed code may merge; all destinations here are synthetic."""
import json

import pytest

from james_monitoring.asks import AskError
from james_monitoring.config import parse_config
from james_monitoring.executor import Executor, ExecutorError
from james_monitoring.fileio import atomic_write
from james_monitoring.github import GitHubError, GitHubSync
from .conftest import make_raw
from .test_executor import git
from .test_execution_recovery import effect_ask


@pytest.fixture
def reviewed_repo(tmp_path):
    repo = tmp_path / "site"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Synthetic tester")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "result.txt").write_text("base")
    git(repo, "add", "result.txt")
    git(repo, "commit", "-qm", "base")
    git(repo, "switch", "-qc", "reviewed")
    (repo / "result.txt").write_text("approved")
    git(repo, "commit", "-qam", "approved")
    reviewed = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "switch", "-qc", "later")
    (repo / "result.txt").write_text("not approved")
    git(repo, "commit", "-qam", "later")
    later = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "switch", "-q", "main")
    cfg = parse_config(make_raw(tmp_path, projects={"site": {"repo": str(repo)}}), base_dir=tmp_path)
    return repo, Executor(cfg), reviewed, later


def test_local_changed_head_is_refused_without_merging(reviewed_repo):
    repo, executor, reviewed, later = reviewed_repo
    before = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/heads/reviewed", later)
    with pytest.raises(ExecutorError, match="changed after review"):
        executor.merge("site", "reviewed", reviewed)
    assert git(repo, "rev-parse", "HEAD") == before
    assert (repo / "result.txt").read_text() == "base"


def test_local_branch_race_still_merges_only_exact_reviewed_commit(reviewed_repo, monkeypatch):
    repo, executor, reviewed, later = reviewed_repo
    import james_monitoring.executor as module
    real = module._git

    def race(path, *args, **kwargs):
        if "--no-ff" in args:
            git(repo, "update-ref", "refs/heads/reviewed", later)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module, "_git", race)
    executor.merge("site", "reviewed", reviewed)
    assert (repo / "result.txt").read_text() == "approved"
    assert git(repo, "rev-parse", "reviewed").strip() == later


def test_failed_abort_never_hard_resets_unrelated_work(reviewed_repo, monkeypatch):
    repo, executor, reviewed, later = reviewed_repo
    import james_monitoring.executor as module
    real, calls = module._git, []

    def fail_merge(path, *args, **kwargs):
        calls.append(args)
        if "merge" in args:
            raise ExecutorError("synthetic merge/abort failure")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module, "_git", fail_merge)
    with pytest.raises(ExecutorError, match="No hard reset"):
        executor.merge("site", "reviewed", reviewed)
    assert not any("reset" in args for args in calls)


def fake_merge_api(cfg, *, head="a" * 40, base="main", result=None):
    sync = GitHubSync(cfg, None, None)
    calls = []

    class Gh:
        def json(self, *args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ("repo", "view"):
                return {"url": "https://github.com/example/site"}
            if args[1].endswith("/merge"):
                return result if result is not None else {"merged": True, "sha": "b" * 40}
            return {"state": "open", "draft": False, "head": {"sha": head}, "base": {"ref": base}}

    sync.gh = Gh()
    return sync, calls


def test_github_merge_sends_expected_sha_and_requires_completed_result(cfg):
    sync, calls = fake_merge_api(cfg)
    assert sync.merge_pr("synthetic-repo", "https://github.com/example/site/pull/7", "a" * 40, "main") == "b" * 40
    args, kwargs = calls[-1]
    assert args[:2] == ("api", "repos/example/site/pulls/7/merge")
    assert "PUT" in args and "--input" in args
    assert json.loads(kwargs["input"]) == {"sha": "a" * 40, "merge_method": "merge"}
    assert "--admin" not in args and "--auto" not in args and "--delete-branch" not in args


@pytest.mark.parametrize("head,base,url", [
    ("c" * 40, "main", "https://github.com/example/site/pull/7"),
    ("a" * 40, "other-base", "https://github.com/example/site/pull/7"),
    ("a" * 40, "main", "https://github.com/another/repo/pull/7"),
    ("a" * 40, "main", "https://github.com/example/site/pull/7?override=true"),
])
def test_changed_or_wrong_github_target_never_sends_merge(cfg, head, base, url):
    sync, calls = fake_merge_api(cfg, head=head, base=base)
    with pytest.raises(GitHubError):
        sync.merge_pr("synthetic-repo", url, "a" * 40, "main")
    assert not any("PUT" in args for args, kwargs in calls)


@pytest.mark.parametrize("result", [{"merged": False}, {"status": "enqueued"}, {"merged": True}])
def test_github_non_receipt_is_not_reported_as_merged(cfg, result):
    sync, calls = fake_merge_api(cfg, result=result)
    with pytest.raises(GitHubError, match="did not confirm"):
        sync.merge_pr("synthetic-repo", "https://github.com/example/site/pull/7", "a" * 40, "main")


async def test_legacy_merge_without_reviewed_head_requires_new_review(rt):
    ask = effect_ask(rt, "merge")
    del ask.doc.meta["payload"]["expected_head"]
    atomic_write(ask.path, ask.doc.render())
    with pytest.raises(AskError, match="no full reviewed commit"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)
    assert rt.asks.get(ask.id).execution == {}
