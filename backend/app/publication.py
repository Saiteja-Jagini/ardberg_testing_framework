from pathlib import Path

from .agents import _patch_paths, _test_path
from .artifacts import run_dir
from .db import session_scope
from .events import emit, get_run, set_run
from .github import GitHubApp
from .models import Run


async def publish_generated_tests(run_id: str, context: dict,
                                  agent_results: list[dict]) -> dict:
    emit(run_id, "publication", "running", agent="executor", node="commit_tests")
    try:
        workspace = (run_dir(run_id) / "workspace").resolve()
        if not workspace.is_dir():
            raise ValueError("Disposable test checkout is unavailable")
        files = {}
        for result in agent_results:
            generated = result.get("patch") or {}
            patch = generated.get("patch", "")
            if not patch:
                continue
            declared = set(generated.get("test_files", []))
            if _patch_paths(patch) != declared:
                raise ValueError("Generated patch paths changed after validation")
            for relative in declared:
                entry = workspace / relative
                target = entry.resolve()
                if (not _test_path(relative) or workspace not in target.parents or
                        entry.is_symlink() or not target.is_file()):
                    raise ValueError(f"Generated test file cannot be published: {relative}")
                if relative in files:
                    raise ValueError(f"Generated test file overlaps another agent: {relative}")
                files[relative] = target.read_text(encoding="utf-8")
        if not files:
            emit(run_id, "publication", "passed", agent="executor", node="commit_tests",
                 success=True, detail={"committed": False, "reason": "No generated test files"})
            return {"success": True, "committed": False, "files": []}

        run = get_run(run_id)
        with session_scope() as session:
            db_run = session.get(Run, run_id)
            installation_id = db_run.installation_id
            old_check_id = db_run.check_run_id
        github = GitHubApp()
        token = await github.installation_token(installation_id)
        commit_sha = await github.commit_test_files(
            run["repository"], run["pr_number"], context["head_sha"],
            files, token, run_id,
        )
        updated_context = {**context, "published_test_sha": commit_sha,
                           "published_test_files": sorted(files)}
        set_run(run_id, head_sha=commit_sha, context=updated_context, check_run_id=None)
        if old_check_id:
            try:
                await github.complete_check(
                    run["repository"], old_check_id, token, "neutral",
                    f"Generated tests were committed at {commit_sha[:12]}. The final Ardberg review is attached to that commit.",
                )
            except Exception:
                pass
        emit(run_id, "publication", "passed", agent="executor", node="commit_tests",
             success=True, detail={"committed": True, "commit_sha": commit_sha,
                                   "files": sorted(files)})
        return {"success": True, "committed": True, "commit_sha": commit_sha,
                "files": sorted(files)}
    except Exception as exc:
        emit(run_id, "publication", "failed", agent="executor", node="commit_tests",
             success=False, detail={"error": str(exc)})
        return {"success": False, "committed": False, "error": str(exc)}
