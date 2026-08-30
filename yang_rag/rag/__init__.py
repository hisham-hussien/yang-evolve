"""
RAG (Retrieval-Augmented Generation) Module
===========================================
Handles semantic search and retrieval of similar YANG keywords using vector embeddings.

Core Components:
- NEW: yang_rag_adapter: Adapter for new YANG-RAG system
- embedding: Hybrid embedding model (SentenceTransformer/TF-IDF)
- indexer: Dual-embedding indexer
- query: Advanced query engine with hybrid scoring
- vector_store: FAISS vector store wrapper

Legacy Components (deprecated):
- rag_system: Old RAG functionality (deprecated)
- identify_yang_keyword: Old keyword identifier (deprecated, use yang_rag_adapter)
"""

# New system - preferred
from .yang_rag_adapter import identify_keyword, query_yang_rag, display_results

# Legacy imports for backward compatibility (will be removed)
# from .rag_system import (...)  # Commented out to avoid import errors
# from .identify_yang_keyword import (...)  # Commented out to avoid import errors

__all__ = [
    # New system
    'identify_keyword',
    'query_yang_rag',
    'display_results'
]
