"""Bounded diagnostics for hosted Python repairs. Never execute repository code."""
from collections import deque
from itertools import islice
from pathlib import PurePosixPath
import re

import requests
from github import Github

TEST_ID = re.compile(r"([\w./-]+\.py(?:::[\w.-]+)+(?:\[[^\]\n]+\])?)")
PY_PATH = re.compile(r'(?<![\w/])([\w./-]+\.py)(?=[:",\s])')
SIGNAL = re.compile(r"Traceback|AssertionError|\bFAILED\b|\bERROR\b|\.py[:\x22,]|^\s*(?:E\s+|>\s|assert\s)|\w+Error:")


def job_diagnostics(repo, job_id, token):
    url = f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs"
    hits, tail = deque(maxlen=96), deque(maxlen=12)
    with requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True,
                      timeout=(5, 12)) as response:
        response.raise_for_status()
        pending = b""
        size = 0
        for chunk in response.iter_content(16384):
            size += len(chunk)
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()[-2048:]
            for raw in lines:
                line = raw[:2048].decode("utf-8", "replace")
                line = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s*", "", line)
                tail.append(line)
                if SIGNAL.search(line):
                    hits.append(line)
            if size >= 16 * 1024 * 1024:
                break
        if pending:
            tail.append(pending.decode("utf-8", "replace"))
    return "\n".join(dict.fromkeys([*hits, *tail]))[-16000:]


def source_path(name):
    path = PurePosixPath(name)
    return (name.endswith(".py") and not path.is_absolute() and ".." not in path.parts
            and not path.name.startswith("test_") and "tests" not in path.parts
            and not path.name.endswith("_test.py") and not name.startswith("."))


def historical_context(repo_name, sha, log, token):
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
        raise ValueError("A GitHub commit SHA is required to diagnose a hosted repair.")
    repo = Github(token, timeout=10, retry=0).get_repo(repo_name)
    commit = repo.get_commit(sha)
    files = list(islice(commit.files, 60))
    changes = {f.filename: (f.patch or "")[:1800] for f in files
               if f.status != "removed" and source_path(f.filename)}
    checkout = f"/home/runner/work/{repo_name.split('/')[-1]}/{repo_name.split('/')[-1]}/"
    log = log.replace(checkout, "")
    traces = list(dict.fromkeys(p for p in PY_PATH.findall(log) if source_path(p)))
    tests = list(dict.fromkeys(test for line in log.splitlines() if re.search(r"\b(?:FAILED|ERROR)\b", line)
                               for test in TEST_ID.findall(line)))
    candidates = list(dict.fromkeys([*traces[:6], *changes]))[:12]
    test_paths = [t.split("::")[0] for t in tests]
    for path in candidates:
        test_paths.append(f"tests/test_{PurePosixPath(path).stem}.py")
    test_paths.extend(f.filename for f in files if f.filename.startswith("tests/") and f.filename.endswith(".py") and f.status != "removed")
    evidence = {}
    for path in islice(dict.fromkeys(test_paths), 5):
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            continue
        try:
            content = repo.get_contents(path, ref=sha)
            if content.size <= 128000:
                evidence[path] = content.decoded_content.decode()[:6000]
        except Exception:
            continue
    changes = {path: changes[path] for path in candidates if path in changes}
    return {"changes": changes, "traces": traces[:6], "tests": evidence,
            "failed_tests": tests[:8], "candidates": candidates}
