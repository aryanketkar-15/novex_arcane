import os
import re
import json
import uuid
import time
import subprocess
import logging
from typing import Dict, Any

from agents.execution_mode import get_execution_mode

logger = logging.getLogger(__name__)


def run_bisect(state: Dict[str, Any]) -> Dict[str, Any]:
    """Entry point for git bisect analysis.

    In local mode: executes git bisect inside an isolated Docker sandbox.
    In hosted mode: uses GitHub API commit history and diffs to identify
                    the suspected breaking commit without invoking Docker.
    """
    mode = state.get("execution_mode") or get_execution_mode()
    state["execution_mode"] = mode

    if mode == "hosted":
        logger.info("[Bisect] Running in hosted mode (GitHub API commit inspection, no Docker)")
        return run_hosted_commit_inspection(state)

    logger.info("[Bisect] Running in local mode (Docker sandbox git bisect)")
    return run_docker_bisect(state)


def run_hosted_commit_inspection(state: Dict[str, Any]) -> Dict[str, Any]:
    """Hosted mode alternative to Docker git bisect.

    Uses authenticated GitHub API to inspect commit history, changed files,
    and commit diffs to identify the suspected breaking commit.
    Never executes repository code or invokes Docker.
    """
    repo_full_name = state.get("repo_full_name", "")
    target_commit_sha = state.get("commit_sha", "")
    failing_file = state.get("failing_file", "")
    failing_test = state.get("failing_test", "")

    state["bisect_mode"] = "hosted_inspection"
    state["bisect_confidence"] = "uncertain"

    context = state.get("diagnostics")
    if context:
        diff = context["changes"].get(failing_file, "")
        return {**state, "bisect_intent_report": (
            f"Unverified commit inspection at {target_commit_sha}. Candidate: {failing_file}.\n{diff}"
        )}

    if not repo_full_name:
        report = (
            "[HOSTED COMMIT INSPECTION — UNVERIFIED]\n"
            "Execution Mode: Hosted (Docker bisect skipped)\n"
            "Status: Missing repository information.\n"
        )
        return {**state, "bisect_intent_report": report}

    github_pat = os.getenv("GITHUB_PAT")
    try:
        from github import Github
        g = Github(github_pat) if github_pat else Github()
        repo = g.get_repo(repo_full_name)

        commits = []
        try:
            if target_commit_sha and target_commit_sha not in ("unknown", "?"):
                paginated = repo.get_commits(sha=target_commit_sha)
            else:
                paginated = repo.get_commits()
            for i, c in enumerate(paginated):
                if i >= 10:
                    break
                commits.append(c)
        except Exception as e:
            logger.warning(f"[HostedInspection] Failed fetching commit history via GitHub API: {e}")

        suspected_commit = None
        culprit_diff = ""
        matching_reason = ""

        # Priority 1: Check if any commit modified the failing_file
        failing_basename = os.path.basename(failing_file) if failing_file else ""
        for c in commits:
            try:
                commit_files = [f.filename for f in (c.files or [])]
                if failing_file and any(f == failing_file or (failing_basename and f.endswith(failing_basename)) for f in commit_files):
                    suspected_commit = c
                    matching_reason = f"Commit modified failing file '{failing_file}'"
                    for f in (c.files or []):
                        if f.filename == failing_file or (failing_basename and f.filename.endswith(failing_basename)):
                            culprit_diff = f.patch or ""
                    break
            except Exception:
                continue

        # Priority 2: If no commit directly touched failing_file, take target_commit_sha / latest commit
        if not suspected_commit and commits:
            suspected_commit = commits[0]
            matching_reason = "Target commit associated with CI failure event"
            try:
                if suspected_commit.files:
                    culprit_diff = "\n".join(
                        f"--- a/{f.filename}\n+++ b/{f.filename}\n{f.patch or ''}"
                        for f in suspected_commit.files[:3]
                    )
            except Exception:
                pass

        if suspected_commit:
            sha = suspected_commit.sha
            author = suspected_commit.commit.author.name if suspected_commit.commit and suspected_commit.commit.author else "Unknown"
            msg = suspected_commit.commit.message.splitlines()[0] if suspected_commit.commit and suspected_commit.commit.message else "No message"
            files_changed = [f.filename for f in (suspected_commit.files or [])][:5]

            intent_report = (
                "[HOSTED COMMIT INSPECTION — UNVERIFIED]\n"
                "Execution Mode: Hosted (Docker bisect skipped)\n"
                "Method: GitHub API commit & diff analysis\n"
                f"Suspected Breaking Commit: {sha[:7]} ({sha})\n"
                f"Author: {author}\n"
                f"Message: {msg}\n"
                f"Reason: {matching_reason}\n"
                f"Files Modified: {', '.join(files_changed) or 'Unknown'}\n"
                "Note: This is a suspected commit based on static history analysis, "
                "not a verified first-bad commit from executed bisect tests.\n"
            )
            if culprit_diff:
                intent_report += f"\n--- Commit Diff Preview ---\n{culprit_diff[:1500]}\n"
        else:
            sha = target_commit_sha or "unknown"
            intent_report = (
                "[HOSTED COMMIT INSPECTION — UNVERIFIED]\n"
                "Execution Mode: Hosted (Docker bisect skipped)\n"
                f"Target Commit: {sha[:7] if len(sha) >= 7 else sha}\n"
                "Note: Real Git bisect was not performed because Docker is unavailable in this environment. "
                "Proceeding with build event target commit as suspected breaking commit.\n"
            )

        return {**state, "bisect_intent_report": intent_report}

    except Exception as exc:
        logger.error(f"[HostedInspection] Error during commit inspection: {exc}")
        fallback_report = (
            "[HOSTED COMMIT INSPECTION — UNVERIFIED]\n"
            "Execution Mode: Hosted (Docker bisect skipped)\n"
            f"Target Commit: {target_commit_sha[:7] if target_commit_sha else 'unknown'}\n"
            f"Notice: GitHub inspection encountered an issue: {exc}. "
            "Proceeding with target commit as suspected breaking commit.\n"
        )
        return {**state, "bisect_intent_report": fallback_report}


def run_docker_bisect(state: Dict[str, Any]) -> Dict[str, Any]:
    """Automates the process of finding the commit that introduced a failure using git bisect.

    Runs entirely inside an isolated Docker sandbox.
    """
    repo_full_name = state.get("repo_full_name", "")
    target_commit_sha = state.get("commit_sha", "")

    if not repo_full_name or not target_commit_sha:
        logger.error("[Bisect] Missing repo_full_name or commit_sha in state.")
        return {**state, "bisect_intent_report": "Error: Missing repo info for bisect."}

    github_pat = os.getenv("GITHUB_PAT")
    if not github_pat:
        logger.warning("[Bisect] GITHUB_PAT not set. Cloning might fail if the repo is private.")
        repo_url = f"https://github.com/{repo_full_name}.git"
    else:
        repo_url = f"https://x-access-token:{github_pat}@github.com/{repo_full_name}.git"

    container_name = f"arcane-bisect-{uuid.uuid4().hex[:8]}"
    logger.info(f"[Bisect] Booting sandbox container: {container_name}")

    # 1. Boot the container
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", container_name, "--storage-opt", "size=2G", "arcane-sandbox", "sleep", "infinity"],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"[Bisect] Failed to boot container: {e.stderr}")
        return {**state, "bisect_intent_report": "Error: Failed to boot sandbox."}

    def exec_cmd(cmd_list, timeout=60):
        """Helper to run a command inside the sandbox."""
        return subprocess.run(
            ["docker", "exec", "-i", container_name] + cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    try:
        exec_cmd(["git", "config", "--global", "user.email", "arcane@bot.ai"])
        exec_cmd(["git", "config", "--global", "user.name", "ARCANE Bot"])

        logger.info(f"[Bisect] Cloning repository {repo_full_name} (depth=20)...")
        clone_res = exec_cmd(["git", "clone", "--depth=20", repo_url, "."])
        if clone_res.returncode != 0:
            raise RuntimeError(f"Clone failed: {clone_res.stderr}")

        checkout_res = exec_cmd(["git", "checkout", target_commit_sha])
        if checkout_res.returncode != 0:
            raise RuntimeError(f"Failed to checkout target commit {target_commit_sha}: {checkout_res.stderr}")

        exec_cmd(["git", "bisect", "start"])
        exec_cmd(["git", "bisect", "bad"])

        known_good_commit = None
        cat_res = exec_cmd(["cat", ".arcane_config.json"])
        if cat_res.returncode == 0:
            try:
                config = json.loads(cat_res.stdout)
                known_good_commit = config.get("known_good_commit")
            except Exception as e:
                logger.warning(f"Failed to parse .arcane_config.json: {e}")

        if not known_good_commit:
            logger.info("[Bisect] .arcane_config.json missing or invalid. Falling back to HEAD~3 as known good commit.")
            head_3_res = exec_cmd(["git", "rev-parse", "HEAD~3"])
            if head_3_res.returncode == 0:
                known_good_commit = head_3_res.stdout.strip()
            else:
                raise RuntimeError("Failed to resolve HEAD~3 for bisect fallback.")

        logger.info(f"[Bisect] Found known good commit: {known_good_commit[:7]}")

        check_commit_res = exec_cmd(["git", "rev-parse", "--verify", f"{known_good_commit}^{{commit}}"])
        if check_commit_res.returncode != 0:
            logger.info(f"[Bisect] Commit {known_good_commit[:7]} not in shallow history. Fetching --unshallow...")
            unshallow_res = exec_cmd(["git", "fetch", "--unshallow"])
            if unshallow_res.returncode != 0:
                logger.warning(f"[Bisect] Unshallow fetch failed: {unshallow_res.stderr}")

        logger.info(f"[Bisect] Marking {known_good_commit[:7]} as good.")
        good_res = exec_cmd(["git", "bisect", "good", known_good_commit])
        if good_res.returncode != 0:
            raise RuntimeError(f"Failed to mark {known_good_commit} as good: {good_res.stderr}")

        MAX_ITERATIONS = 8
        bad_commit_found = None

        for i in range(MAX_ITERATIONS):
            logger.info(f"[Bisect] Iteration {i+1}/{MAX_ITERATIONS}...")
            pytest_res = exec_cmd(["env", "PYTHONPATH=.", "pytest", "-v"])

            if pytest_res.returncode == 0:
                logger.info("[Bisect] Tests passed. Marking current commit as GOOD.")
                step_res = exec_cmd(["git", "bisect", "good"])
            else:
                logger.info("[Bisect] Tests failed. Marking current commit as BAD.")
                step_res = exec_cmd(["git", "bisect", "bad"])

            bisect_output = step_res.stdout + step_res.stderr
            if "is the first bad commit" in bisect_output:
                match = re.search(r"([0-9a-f]{40})\s+is the first bad commit", bisect_output)
                if match:
                    bad_commit_found = match.group(1)
                else:
                    lines = bisect_output.strip().splitlines()
                    for line in lines:
                        if "is the first bad commit" in line:
                            bad_commit_found = line.split()[0]
                            break
                break

        state["bisect_mode"] = "docker_bisect"
        if bad_commit_found:
            logger.info(f"[Bisect] Found first bad commit: {bad_commit_found[:7]}")
            state["bisect_confidence"] = "verified"
            log_res = exec_cmd(["git", "log", "-1", "--format=%H%n%an%n%s%n%b", bad_commit_found])
            log_parts = log_res.stdout.strip().split("\n", 3)
            commit_hash = log_parts[0] if len(log_parts) > 0 else bad_commit_found
            author = log_parts[1] if len(log_parts) > 1 else "Unknown"
            subject = log_parts[2] if len(log_parts) > 2 else "No Subject"
            body = log_parts[3] if len(log_parts) > 3 else ""

            show_res = exec_cmd(["git", "show", bad_commit_found])
            diff_output = show_res.stdout.strip()

            intent_report = (
                f"Bisect identified commit {commit_hash[:7]} as the first bad commit.\n"
                f"Author: {author}\n"
                f"Message: {subject}\n"
                f"Body: {body}\n\n"
                f"--- Diff ---\n{diff_output}\n"
            )
        else:
            logger.warning("[Bisect] Max iterations reached without finding the exact bad commit.")
            state["bisect_confidence"] = "low"
            head_res = exec_cmd(["git", "rev-parse", "HEAD"])
            best_guess = head_res.stdout.strip()

            show_res = exec_cmd(["git", "show", best_guess])
            diff_output = show_res.stdout.strip()

            intent_report = (
                f"[LOW CONFIDENCE] Bisect exhausted {MAX_ITERATIONS} iterations. "
                f"Best guess for bad commit is {best_guess[:7]}.\n\n"
                f"--- Diff ---\n{diff_output}\n"
            )

        return {**state, "bisect_intent_report": intent_report}

    except Exception as e:
        logger.error(f"[Bisect] Sandbox error: {e}")
        return {**state, "bisect_intent_report": f"Error during bisect: {e}"}

    finally:
        logger.info(f"[Bisect] Cleaning up container {container_name}")
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
