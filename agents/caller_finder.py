"""
caller_finder.py
================
Prompt 3.2 — Given a changed function name, return all files that call it.
"""


def find_callers(changed_function: str, dep_graph: dict) -> list[str]:
    """
    Search the dep_graph for files that import the module containing changed_function.

    Args:
        changed_function: name of the function that was changed (e.g. 'validate_user')
        dep_graph:        output of build_dep_graph()

    Returns:
        List of file paths (relative) that import the module owning changed_function.
    """
    # Find which file owns the changed function
    owner_file = None
    for file_path, info in dep_graph.items():
        if changed_function in info.get('functions', []):
            owner_file = file_path
            break

    if owner_file is None:
        return []

    # Return the files that import the owner
    return list(dep_graph[owner_file].get('imported_by', []))
