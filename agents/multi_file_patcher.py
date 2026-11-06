"""
multi_file_patcher.py
=====================
Prompt 3.2 — For each caller file, generate a patch updating calls to changed_function.
"""

import difflib
import os
from agents.llm_client import call_llm


def generate_caller_patches(callers: list[str], original_signature: str,
                             new_signature: str, repo_path: str = '') -> list[dict]:
    """
    For each caller file, ask the LLM to update all calls from original_signature
    to new_signature and return unified diffs.

    Args:
        callers:            list of relative file paths (from find_callers)
        original_signature: e.g. 'validate_user(username, password)'
        new_signature:      e.g. 'validate_user(user, pwd)'
        repo_path:          absolute base path to resolve relative caller paths

    Returns:
        List of { 'file': path, 'patch': unified_diff_string }
    """
    results = []

    for rel_path in callers:
        full_path = os.path.join(repo_path, rel_path) if repo_path else rel_path

        try:
            with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                original_content = f.read()
        except OSError:
            continue

        prompt = (
            f"Update all calls to `{original_signature}` to use `{new_signature}` "
            f"in the following Python file. Return ONLY the complete updated file "
            f"content with no explanation, no markdown fences.\n\n"
            f"```python\n{original_content}\n```"
        )

        updated_content = call_llm(prompt)

        # Strip any accidental markdown fences from LLM output
        updated_content = (
            updated_content
            .replace('```python', '')
            .replace('```', '')
            .strip()
        )

        # Compute unified diff
        diff_lines = list(difflib.unified_diff(
            original_content.splitlines(keepends=True),
            updated_content.splitlines(keepends=True),
            fromfile=f'a/{rel_path}',
            tofile=f'b/{rel_path}'
        ))
        patch_str = ''.join(diff_lines)

        results.append({
            'file':  rel_path,
            'patch': patch_str
        })

    return results
