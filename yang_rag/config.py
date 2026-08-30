"""
Configuration file for RAG system
"""
import os
import sys

# Get the project root directory (two levels up from this file: yang_rag/config.py)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_data_directory() -> str:
    """
    Resolve the data/ directory in priority order:

    1. <project_root>/data/          — development / editable install (pip install -e .)
    2. <sys.prefix>/share/yang-comparator-pro/data/
                                     — wheel install (pip install *.whl)
    3. <script_dir>/data/            — fallback for unusual layouts

    Returns the first path that exists, or the project-root path as default.
    """
    candidates = [
        os.path.join(PROJECT_ROOT, "data"),
        os.path.join(sys.prefix, "share", "yang-comparator-pro", "data"),
        os.path.join(os.path.dirname(sys.executable), "..", "share",
                     "yang-comparator-pro", "data"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return os.path.abspath(path)
    # Default: project root (will be created by extractor/indexer on first run)
    return os.path.join(PROJECT_ROOT, "data")


# Directory paths
DOCUMENTS_DIRECTORY = os.path.join(PROJECT_ROOT, "documents")
DATA_DIRECTORY = _find_data_directory()
CHROMA_DB_PATH = os.path.join(DATA_DIRECTORY, "chroma_db")

# Collection name for ChromaDB
CHROMA_COLLECTION_NAME = "document_rag_collection"

# Embedding model configuration
EMBEDDING_MODEL_NAME = 'all-MiniLM-L6-v2'  # Efficient, high-quality embedding model

# Text chunking configuration
CHUNK_SIZE = 500  # Max characters per chunk
CHUNK_OVERLAP = 50  # Characters overlap between chunks

# Query configuration
DEFAULT_N_RESULTS = 5  # Default number of results to return
RAG_INITIAL_K = 3000   # Initial candidate pool size for vector search.
                       # Larger values improve recall for semantically distant queries
                       # (e.g. vendor extensions vs. standard YANG keywords) at the
                       # cost of slightly higher latency. Tune based on index size.

# Supported file extensions
SUPPORTED_EXTENSIONS = {
    'xml': ['.xml'],
    'pdf': ['.pdf'],
    'docx': ['.docx', '.doc'],
    'text': ['.txt', '.md', '.markdown'],
    'code': ['.py', '.js', '.ts', '.java', '.cpp', '.c', '.cs', '.go', '.rs', '.rb', '.php', '.swift', '.kt', '.scala']
}

# Get all supported extensions as a flat list
ALL_SUPPORTED_EXTENSIONS = []
for ext_list in SUPPORTED_EXTENSIONS.values():
    ALL_SUPPORTED_EXTENSIONS.extend(ext_list)
