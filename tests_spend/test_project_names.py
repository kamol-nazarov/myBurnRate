"""A session's project is the repository it ran in, worktrees included."""

from __future__ import annotations

from pathlib import Path

from spend_app.adapters import local_common
from spend_app.adapters.local_common import folder_name, project_name


def test_worktree_layouts_count_toward_their_repository() -> None:
    assert project_name(r"C:\Users\me\.t3\worktrees\elderscrolls\t3-1c51a20c\spike") == "elderscrolls"
    assert project_name("/home/me/.t3/worktrees/app/t3-0f00/src") == "app"
    assert project_name(r"C:\Dev\shop\.claude\worktrees\magical-engelbart-1") == "shop"
    assert project_name("/Users/me/orca/workspaces/ai-pulse/audit-web") == "ai-pulse"


def test_paths_from_another_computer_keep_their_last_folder() -> None:
    assert project_name(r"C:\Dev\ExampleProject") == "ExampleProject"
    assert project_name("/home/dev/work/api/") == "api"
    assert project_name("") is None and project_name(None) is None
    assert folder_name("C:\\") is None


def test_a_folder_on_this_computer_counts_toward_its_git_repository(tmp_path: Path) -> None:
    local_common._REPO_ROOTS.clear()
    repo = tmp_path / "billing-service"
    (repo / ".git").mkdir(parents=True)
    (repo / "services" / "api").mkdir(parents=True)
    assert project_name(str(repo / "services" / "api")) == "billing-service"
    # A linked worktree points back at its main repository.
    worktree = tmp_path / "wt" / "feature-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {repo.as_posix()}/.git/worktrees/feature-x\n", encoding="utf-8")
    assert project_name(str(worktree)) == "billing-service"
    # Outside any repository the folder name stands.
    plain = tmp_path / "notes"
    plain.mkdir()
    assert project_name(str(plain)) == "notes"


def test_the_hud_lists_todays_top_repositories_by_value() -> None:
    from decimal import Decimal

    from spend_app.hud_data import repo_breakdown

    def event(project, usd, tokens):
        return {"project": project, "spend": None if usd is None else Decimal(str(usd)),
                "input_tokens": tokens, "output_tokens": 0}

    events = [event("api", 3.5, 1000), event("web", 1.25, 400), event("api", 0.5, 100),
              event(None, 2, 50), event("docs", None, 70)]
    for name in ("a", "b", "c", "d"):
        events.append(event(name, 0.1, 10))
    out = repo_breakdown(events, limit=3)
    assert out["period"] == "today" and out["totalUsd"] == 7.65
    assert [(row["project"], row["usd"], row["tokens"]) for row in out["rows"]] == [
        ("api", 4.0, 1100), ("web", 1.25, 400), ("a", 0.1, 10)]
    assert round(out["rows"][0]["sharePct"], 1) == 52.3
    # Smaller repositories and usage with no project are summed as "other".
    assert out["other"] == {"usd": 2.3, "tokens": 150}
