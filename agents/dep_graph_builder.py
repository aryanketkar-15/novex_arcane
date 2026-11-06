"""
dep_graph_builder.py
====================
Prompt 3.1 — Build a single-depth dependency graph for all .py files in a repo.

Usage:
    from agents.dep_graph_builder import build_dep_graph
    graph = build_dep_graph('/path/to/repo')
"""

import os
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
_parser = Parser(PY_LANGUAGE)


def build_dep_graph(repo_path: str) -> dict:
    """
    Walk all .py files in repo_path (single-depth imports only).

    Returns:
        {
            'auth.py': {
                'functions':   ['validate_user', 'logout'],
                'imports':     ['api', 'middleware'],
                'imported_by': ['api.py', 'middleware.py']
            },
            ...
        }
    """
    graph = {}

    # ── Pass 1: parse every .py file for functions + imports ─────────────────
    for dirpath, _, filenames in os.walk(repo_path):
        for fname in filenames:
            if not fname.endswith('.py'):
                continue

            full_path = os.path.join(dirpath, fname)
            rel_path  = os.path.relpath(full_path, repo_path).replace('\\', '/')

            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                    src = f.read()
            except OSError:
                continue

            tree = _parser.parse(src.encode('utf-8'))
            functions, imports = _extract_info(tree.root_node)

            graph[rel_path] = {
                'functions':   functions,
                'imports':     imports,
                'imported_by': []
            }

    # ── Pass 2: invert imports → populate imported_by ─────────────────────────
    for file_path, info in graph.items():
        module_name = _path_to_module(file_path)   # e.g. 'agents/auth.py' → 'auth'

        for other_path, other_info in graph.items():
            if other_path == file_path:
                continue
            if module_name in other_info['imports']:
                graph[file_path]['imported_by'].append(other_path)

    return graph


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_info(root_node) -> tuple[list, list]:
    """Return (function_names, imported_module_names) from a parsed tree."""
    functions = []
    imports   = []

    def traverse(node):
        # Function definitions
        if node.type == 'function_definition':
            for child in node.children:
                if child.type == 'identifier':
                    functions.append(child.text.decode('utf-8'))
                    break

        # import X  or  from X import Y
        elif node.type == 'import_statement':
            for child in node.children:
                if child.type in ('dotted_name', 'identifier'):
                    imports.append(child.text.decode('utf-8').strip())

        elif node.type == 'import_from_statement':
            # first dotted_name / relative_import is the module
            for child in node.children:
                if child.type in ('dotted_name', 'relative_import', 'identifier'):
                    module = child.text.decode('utf-8').strip().lstrip('.')
                    if module:
                        imports.append(module)
                    break

        for child in node.children:
            traverse(child)

    traverse(root_node)
    return functions, list(set(imports))


def _path_to_module(rel_path: str) -> str:
    """Convert 'agents/auth.py' → 'auth'  (basename without extension)."""
    return os.path.splitext(os.path.basename(rel_path))[0]
