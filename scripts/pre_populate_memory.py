"""
pre_populate_memory.py
======================
Pre-populate ChromaDB with 1 patch from the demo repo dry run.
Run once before Hour 20 to ensure the memory hit demo works.

Usage:
    python scripts/pre_populate_memory.py
"""

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.chroma_memory import init_memory, store_patch, query_memory

CHROMA_PATH = os.path.abspath('./chroma_data')

def main():
    print("[ARCANE] Pre-populating ChromaDB with demo dry-run patch...")
    client = init_memory(CHROMA_PATH)

    # Patch from bug-1 scenario: validate_user signature change breaking callers
    store_patch(
        client,
        error_log  = "FAILED tests/test_api.py::test_get_user - TypeError: validate_user() got an unexpected keyword argument 'username'",
        root_cause = "Function signature changed: validate_user(username, password) renamed to validate_user(user, pwd) breaking all callers in api.py and middleware.py",
        patch_diff = (
            "--- a/auth.py\n+++ b/auth.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def validate_user(user, pwd):\n"
            "+def validate_user(username, password):\n"
            "     return username == 'admin' and password == 'secret'\n"
        ),
        test_file  = "tests/test_api.py",
        commit_sha = "DRY_RUN_BUG1_PREPOPULATE"
    )

    print("[ARCANE] Patch stored. Verifying query...")
    result = query_memory(
        client,
        "FAILED tests/test_api.py - TypeError: validate_user() got unexpected keyword argument 'username'",
        threshold=0.70   # slightly lower threshold for the demo to guarantee a hit
    )

    if result:
        print(f"[ARCANE] MATCH CONFIRMED! commit_sha={result['commit_sha']}")
        print(f"         date={result['date']}")
        print(f"         test_file={result['test_file']}")
    else:
        print("[ARCANE] WARNING: No match found -- threshold may need adjusting.")

    print(f"\n[ARCANE] ChromaDB persisted at: {CHROMA_PATH}")
    print("[ARCANE] Pre-population complete. Ready for Step 10 demo memory hit!")

if __name__ == '__main__':
    main()
