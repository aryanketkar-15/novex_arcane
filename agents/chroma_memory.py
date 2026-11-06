"""
chroma_memory.py
================
Prompt 3.3 — ChromaDB Patch Memory Layer

ARCANE learns from every successful patch. Similar failures get solved in seconds.

Usage:
    from agents.chroma_memory import init_memory, store_patch, query_memory

    client = init_memory('./chroma_data')
    store_patch(client, error_log, root_cause, patch_diff, test_file, commit_sha)
    result = query_memory(client, new_error_log)   # dict | None
"""

import os
import datetime
import chromadb
from chromadb.utils import embedding_functions


_COLLECTION_NAME = 'arcane_patches'
_EMBED_MODEL     = 'all-MiniLM-L6-v2'


def init_memory(persist_path: str) -> chromadb.ClientAPI:
    """
    Creates or loads a persistent ChromaDB collection named 'arcane_patches'.

    Args:
        persist_path: directory where ChromaDB stores its data (MUST be absolute)

    Returns:
        chromadb.ClientAPI with collection pre-loaded
    """
    abs_path = os.path.abspath(persist_path)
    os.makedirs(abs_path, exist_ok=True)

    client = chromadb.PersistentClient(path=abs_path)

    # Attach embedding function so queries/adds use the same model
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=_EMBED_MODEL
    )

    # get_or_create is idempotent — safe to call on every startup
    client._arcane_collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef
    )

    return client


def _get_collection(client: chromadb.ClientAPI):
    """Retrieve or lazily attach the collection to the client."""
    if hasattr(client, '_arcane_collection'):
        return client._arcane_collection

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=_EMBED_MODEL
    )
    col = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef
    )
    client._arcane_collection = col
    return col


def store_patch(client: chromadb.ClientAPI,
                error_log: str,
                root_cause: str,
                patch_diff: str,
                test_file: str,
                commit_sha: str) -> None:
    """
    Embed and store a successful patch in ChromaDB.

    The embedding input is: '{error_log} || {root_cause}'
    Metadata stored: patch_diff, test_file, commit_sha, date
    """
    col = _get_collection(client)

    document = f"{error_log} || {root_cause}"
    doc_id   = f"{commit_sha}_{hash(document) & 0xFFFFFFFF}"

    col.add(
        documents=[document],
        metadatas=[{
            'patch_diff': patch_diff,
            'test_file':  test_file,
            'commit_sha': commit_sha,
            'date':       datetime.datetime.utcnow().isoformat(),
            'error_log':  error_log,
            'root_cause': root_cause,
        }],
        ids=[doc_id]
    )


def query_memory(client: chromadb.ClientAPI,
                 error_log: str,
                 threshold: float = 0.85) -> dict | None:
    """
    Query ChromaDB for a patch that matches the given error_log.

    ChromaDB returns L2 distances — lower = more similar.
    We convert to a similarity score: similarity = 1 / (1 + distance)
    Returns the metadata dict if similarity >= threshold, else None.

    Args:
        client:     ChromaDB client from init_memory()
        error_log:  new error log to match against
        threshold:  minimum similarity (0.0–1.0) to consider a match

    Returns:
        metadata dict (with patch_diff, test_file, commit_sha, date) or None
    """
    col = _get_collection(client)

    try:
        results = col.query(
            query_texts=[error_log],
            n_results=1,
            include=['metadatas', 'distances']
        )
    except Exception as e:
        # Collection empty or other ChromaDB error — return None gracefully
        print(f"[chroma_memory] query failed: {e}")
        return None

    if not results or not results['distances'] or not results['distances'][0]:
        return None

    distance   = results['distances'][0][0]
    similarity = 1.0 / (1.0 + distance)   # convert L2 distance → similarity

    if similarity >= threshold:
        return results['metadatas'][0][0]

    return None
