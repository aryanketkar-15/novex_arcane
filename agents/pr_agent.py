import os
import logging
from github import Github
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

from agents.mermaid_generator import generate_mermaid_diagram
from agents.execution_mode import get_execution_mode


def create_pr(state: dict) -> str:
    """Creates a new branch from main/base, commits the patch diff and patched file,
    and opens a PR with the full 7-section format.

    In hosted mode (or when validation was SKIPPED):
    - Opens a Draft PR (draft=True).
    - Includes a prominent unverified testing banner and review requirement.
    In local mode:
    - Opens a standard PR.
    """
    token = os.getenv("GITHUB_PAT")
    if not token:
        raise ValueError("GITHUB_PAT not found in environment variables")

    repo_full_name = state.get("repo_full_name", "")
    commit_sha = state.get("commit_sha", "unknown")
    patch_diff = state.get("patch_diff", "")
    failing_test = state.get("failing_test", "unknown_test")
    failing_file = state.get("failing_file", "unknown_file")
    root_cause = state.get("root_cause_summary", "No root cause provided.")
    regression_test = state.get("regression_test_code", "")
    confidence = state.get("confidence_score", "N/A")
    tests_passed = state.get("tests_passed", False)

    mode = state.get("execution_mode") or get_execution_mode()
    validation_status = state.get("validation_status", "PASSED" if tests_passed else "SKIPPED")
    is_draft = (mode == "hosted") or (validation_status == "SKIPPED")
    state["is_draft_pr"] = is_draft

    g = Github(token)
    repo = g.get_repo(repo_full_name)

    branch_name = f"arcane/fix-{commit_sha[:7]}"
    base_branch = (state.get("event") or {}).get("branch") or "main"
    patched_content = state.get("patched_content", "")

    if not patch_diff.strip():
        raise ValueError("Cannot open a pull request without a source patch.")
    if mode == "hosted" and validation_status != "SKIPPED":
        raise ValueError("Hosted static validation must pass before opening a draft.")
    if is_draft:
        from agents.patch_generator import source_diff
        import hashlib
        from agents.validator_agent import validate_hosted_static

        if validation_status != "SKIPPED" or not patched_content.strip():
            raise ValueError("A statically validated source change is required for a hosted draft.")
        if validate_hosted_static(dict(state))["validation_status"] != "SKIPPED":
            raise ValueError("Hosted static validation failed.")
        base_sha = repo.get_git_ref(f"heads/{base_branch}").object.sha
        historical = repo.get_contents(failing_file, ref=commit_sha)
        current = repo.get_contents(failing_file, ref=base_sha)
        if current.sha != historical.sha:
            raise ValueError("Target file changed since the failed build. Run repair against the current revision.")
        original = historical.decoded_content.decode("utf-8")
        expected = source_diff(failing_file, original, patched_content)
        if not expected or expected.rstrip("\n") != patch_diff.rstrip("\n"):
            raise ValueError("Patch does not match the historical source and proposed file content.")

        # Build the commit before exposing the repair branch.
        from github import InputGitTreeElement, GithubException
        parent = repo.get_git_commit(base_sha)
        directory = parent.tree
        for part in failing_file.split("/")[:-1]:
            entry = next(item for item in directory.tree if item.path == part and item.type == "tree")
            directory = repo.get_git_tree(entry.sha)
        target = next(item for item in directory.tree if item.path == failing_file.split("/")[-1])
        if target.mode not in ("100644", "100755") or target.sha != historical.sha:
            raise ValueError("Target must be the same regular source file at the branch tip.")
        edit = InputGitTreeElement(failing_file, target.mode, "blob", content=patched_content)
        tree = repo.create_git_tree([edit], parent.tree)
        digest = hashlib.sha256((base_sha + tree.sha).encode()).hexdigest()[:12]
        branch_name += "-" + digest
        commit = repo.create_git_commit(f"fix: repair {failing_file}", tree, [parent])
        try:
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=commit.sha)
        except GithubException as exc:
            if exc.status != 422:
                raise
            existing = repo.get_git_commit(repo.get_git_ref(f"heads/{branch_name}").object.sha)
            if existing.tree.sha != tree.sha or [p.sha for p in existing.parents] != [base_sha]:
                raise
            for pull in repo.get_pulls(state="open", base=base_branch, head=f"{repo_full_name.split('/')[0]}:{branch_name}"):
                if pull.draft and pull.html_url:
                    return pull.html_url
        files_description = f"- `{failing_file}`"
    else:
        # 1. Create a new branch from base
        base_ref = repo.get_git_ref(f"heads/{base_branch}")
        try:
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.object.sha)
        except Exception as e:
            logger.warning(f"Branch creation exception (might already exist): {e}")

        # 2. Commit the patch_diff content as a .diff artifact on this branch
        file_path = f"arcane_patch_{commit_sha[:7]}.diff"
        commit_msg = f"fix: ARCANE auto-repair code patch for {failing_test}"

        try:
            contents = repo.get_contents(file_path, ref=branch_name)
            repo.update_file(contents.path, commit_msg, patch_diff, contents.sha, branch=branch_name)
        except Exception:
            repo.create_file(file_path, commit_msg, patch_diff, branch=branch_name)

        # 2b. Commit the actual fix to the failing file so the PR really changes the code
        fix_committed = False
        if patched_content and failing_file and failing_file not in ("N/A", "unknown_file", "unknown"):
            try:
                try:
                    existing = repo.get_contents(failing_file, ref=branch_name)
                    repo.update_file(existing.path, f"fix: ARCANE repair for {failing_test}", patched_content, existing.sha, branch=branch_name)
                except Exception:
                    repo.create_file(failing_file, f"fix: ARCANE repair for {failing_test}", patched_content, branch=branch_name)
                fix_committed = True
            except Exception as e:
                logger.warning(f"Could not commit the patched file {failing_file}: {e}")

        files_description = f"- `{file_path}`" + (f"\n- `{failing_file}`" if fix_committed else "")

    # Generate mermaid diagram
    logger.info("Generating Mermaid diagram...")
    try:
        mermaid_diagram = generate_mermaid_diagram(patch_diff, root_cause)
    except Exception as m_err:
        logger.warning(f"Mermaid generation fallback: {m_err}")
        mermaid_diagram = "```mermaid\ngraph LR\n  Fail[CI Failure] --> Fix[Proposed Patch] --> Review[Human Review]\n```"

    # Title & Banner
    if is_draft:
        mermaid_diagram = "```mermaid\nflowchart LR\n    CI[Failed workflow] --> P[Proposed source patch] --> S[Static checks] --> D[Draft PR for review]\n```"
        pr_title = f"[DRAFT] fix: ARCANE auto-repair proposal for {failing_test}"
        validation_banner = (
            "> ⚠️ **UNVERIFIED DRAFT PULL REQUEST**\n"
            "> \n"
            "> **Docker Sandbox Execution:** SKIPPED (Hosted cloud environment without Docker daemon).\n"
            "> **Test Verification:** NOT VERIFIED (No test suite was executed in an isolated container).\n"
            "> **Static Checks:** PASSED (Python AST syntax and unified diff structure verified).\n"
            "> **Action Required:** Human review and local/CI test verification required before merging.\n\n"
        )
    else:
        pr_title = f"fix: ARCANE auto-repair for {failing_test}"
        validation_banner = ""

    memory_hit_str = ""
    memory_hit = state.get("memory_hit")
    if memory_hit and isinstance(memory_hit, dict):
        mem_date = memory_hit.get("date", "unknown date")
        mem_file = memory_hit.get("test_file", "unknown file")
        memory_hit_str = f"> **🧠 Pattern matched from memory** — {mem_date} — `{mem_file}`\n\n"

    if is_draft:
        confidence_str = f"{confidence}% (🟡 Hosted Proposal — Tests Not Executed — Draft PR)"
    elif isinstance(confidence, (int, float)) and confidence >= 85:
        confidence_str = f"{confidence}% (🟢 Auto-Approve Threshold Met)"
    elif isinstance(confidence, (int, float)) and confidence >= 60:
        confidence_str = f"{confidence}% (🟡 Manual Review Required)"
    elif isinstance(confidence, (int, float)):
        confidence_str = f"{confidence}% (🔴 Escalated)"
    else:
        confidence_str = str(confidence)

    test_verif_label = state.get("test_verification", "NOT_VERIFIED" if is_draft else "VERIFIED")
    val_status_label = state.get("validation_status", "SKIPPED" if is_draft else ("PASSED" if tests_passed else "FAILED"))
    summary_text = state.get("validator_summary", "Tests executed in Docker sandbox." if not is_draft else "Docker sandbox skipped in hosted environment.")

    pr_body = f"""## ARCANE Autonomous Repair

{validation_banner}{memory_hit_str}### 1. What Broke
**Failing Test:** `{failing_test}`

### 2. Root Cause
{root_cause}

### 3. Fix
```diff
{patch_diff}
```

### 4. Execution Flow
{mermaid_diagram}

### 5. Test
**Regression Test Code Generated:**
```python
{regression_test or "# No tests generated"}
```

### 6. Confidence & Validation
- **Execution Mode:** `{mode.upper()}`
- **Validation Status:** `{val_status_label}`
- **Test Verification:** `{test_verif_label}`
- **Confidence Score:** {confidence_str}
- **Validator Summary:** {summary_text}

### 7. Files Changed
{files_description}
"""

    # 3. Opens a PR from arcane/fix-{sha} to base
    logger.info(f"[PR] Creating PR (draft={is_draft}) on {repo_full_name}")
    pr = repo.create_pull(
        title=pr_title,
        body=pr_body,
        head=branch_name,
        base=base_branch,
        draft=is_draft,
    )

    if not pr or not pr.html_url or (is_draft and not pr.draft):
        raise RuntimeError("PR creation failed without returning a valid PR object.")

    logger.info(f"PR generated: {pr.html_url}")
    return pr.html_url


def get_file_content(repo_full_name, file_path, branch='main'):
    """Reads a file from the repo using PyGitHub."""
    token = os.getenv("GITHUB_PAT")
    if not token:
        raise ValueError("GITHUB_PAT not found in environment variables")

    g = Github(token)
    repo = g.get_repo(repo_full_name)

    contents = repo.get_contents(file_path, ref=branch)
    if isinstance(contents, list) or contents.type != "file" or contents.size > 256_000:
        raise ValueError(f"Unsupported file type or size for {file_path}")
    return contents.decoded_content.decode('utf-8')
