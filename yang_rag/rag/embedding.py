#!/usr/bin/env python3
"""
Embedding Module for YANG-RAG
------------------------------
Provides hybrid embedding support:
1. SentenceTransformers (if available)
2. Fallback to TF-IDF with sklearn

Uses embedding_template field for vectorization.
"""

import numpy as np
from typing import List, Union, Optional
import os
from pathlib import Path


class EmbeddingModel:
    """Hybrid embedding model with fallback."""
    
    def __init__(self, model_name: str = 'all-MiniLM-L6-v2', use_gpu: bool = False):
        self.model_name = model_name
        # Allow env-based override and auto-detect
        env_use_gpu = os.getenv('RAG_USE_GPU') or os.getenv('USE_GPU') or ''
        auto_cuda = False
        try:
            import torch  # type: ignore
            auto_cuda = torch.cuda.is_available()
        except Exception:
            auto_cuda = False
        if isinstance(use_gpu, bool):
            requested_gpu = use_gpu
        else:
            requested_gpu = False
        if env_use_gpu.lower() in ('1', 'true', 'yes', 'cuda'):
            requested_gpu = True
        self.use_gpu = bool(requested_gpu and auto_cuda)
        self.model = None
        self.model_type = None
        
        # Try SentenceTransformers first
        try:
            from sentence_transformers import SentenceTransformer
            try:
                self.model = SentenceTransformer(model_name)
                if self.use_gpu:
                    try:
                        self.model = self.model.to('cuda')
                        device = 'cuda'
                    except Exception:
                        device = 'cpu'
                        self.use_gpu = False
                else:
                    device = 'cpu'
                self.model_type = 'sentence-transformer'
                print(f"✓ Loaded SentenceTransformer: {model_name} ({device})")
            except Exception as e:
                # If model download/load fails (e.g., no internet), fall back gracefully
                print(f"⚠ Failed to load SentenceTransformer '{model_name}' ({e}). Falling back to TF-IDF.")
                self._init_tfidf()
        except ImportError:
            print("⚠ sentence-transformers not available, using TF-IDF fallback")
            self._init_tfidf()
    
    def _init_tfidf(self, dim: int = 384):
        """Initialize TF-IDF vectorizer as fallback.
        Args:
            dim: Target dimensionality to match saved embeddings.
        """
        from sklearn.feature_extraction.text import TfidfVectorizer
        
        self.model = TfidfVectorizer(
            max_features=dim,  # Match typical embedding dimension or saved index
            ngram_range=(1, 3),
            min_df=2,
            max_df=0.8,
            sublinear_tf=True
        )
        self.model_type = 'tfidf'
        print(f"✓ Initialized TF-IDF vectorizer ({dim}-dim)")
    
    def fit(self, texts: List[str]):
        """Fit model on corpus (only needed for TF-IDF)."""
        if self.model_type == 'tfidf':
            print(f"Fitting TF-IDF on {len(texts)} documents...")
            self.model.fit(texts)
            print(f"✓ TF-IDF vocabulary size: {len(self.model.vocabulary_)}")
    
    def encode(self, texts: Union[str, List[str]], 
               batch_size: int = 32,
               show_progress: bool = True) -> np.ndarray:
        """
        Encode texts to embeddings.
        
        Returns:
            np.ndarray of shape (n_texts, embedding_dim)
        """
        if isinstance(texts, str):
            texts = [texts]
        
        if self.model_type == 'sentence-transformer':
            embeddings = self.model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=show_progress,
                convert_to_numpy=True
            )
        else:  # tfidf
            embeddings = self.model.transform(texts).toarray()
        
        return embeddings
    
    def get_dimension(self) -> int:
        """Get embedding dimension."""
        if self.model_type == 'sentence-transformer':
            return self.model.get_sentence_embedding_dimension()
        else:
            return self.model.max_features
    
    def save(self, path: Path):
        """Save model to disk."""
        path.mkdir(parents=True, exist_ok=True)
        
        if self.model_type == 'tfidf':
            import joblib
            joblib.dump(self.model, path / 'tfidf_model.pkl')
            print(f"✓ Saved TF-IDF model to {path}")
        else:
            # Persist SentenceTransformer config and weights into the folder
            self.model.save(str(path))
            print(f"✓ Saved SentenceTransformer to {path}")
    
    def load(self, path: Path, expected_dim: Optional[int] = None):
        """Load model from disk."""
        if (path / 'tfidf_model.pkl').exists():
            import joblib
            self.model = joblib.load(path / 'tfidf_model.pkl')
            self.model_type = 'tfidf'
            print(f"✓ Loaded TF-IDF model from {path}")
        elif path.exists():
            try:
                from sentence_transformers import SentenceTransformer
                self.model = SentenceTransformer(str(path))
                self.model_type = 'sentence-transformer'
                # Move to GPU if requested and available
                if self.use_gpu:
                    try:
                        self.model = self.model.to('cuda')
                        print(f"✓ Loaded SentenceTransformer from {path} (cuda)")
                    except Exception:
                        print(f"✓ Loaded SentenceTransformer from {path} (cpu) — CUDA unavailable")
                        self.use_gpu = False
                else:
                    print(f"✓ Loaded SentenceTransformer from {path} (cpu)")
            except (ImportError, Exception) as e:
                # If model directory exists but can't load, fall back to TF-IDF or default model
                print(f"⚠ Failed to load model from {path}: {e}")
                print("⚠ Falling back to TF-IDF or default model")
                self._init_tfidf(dim=int(expected_dim) if expected_dim else 384)
        else:
            # Model directory doesn't exist - initialize default model
            try:
                from sentence_transformers import SentenceTransformer
                self.model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
                self.model_type = 'sentence-transformer'
                # Move to GPU if requested and available
                if self.use_gpu:
                    try:
                        self.model = self.model.to('cuda')
                        print(f"✓ Initialized default SentenceTransformer (cuda) — model dir not found")
                    except Exception:
                        print(f"✓ Initialized default SentenceTransformer (cpu) — model dir not found, CUDA unavailable")
                        self.use_gpu = False
                else:
                    print(f"✓ Initialized default SentenceTransformer (cpu) — model dir not found")
            except ImportError:
                # As a last resort, initialize TF-IDF
                print("⚠ sentence-transformers not available at query time. Falling back to TF-IDF; results may differ from indexed model.")
                self._init_tfidf(dim=int(expected_dim) if expected_dim else 384)


def compute_similarity(query_vec: np.ndarray, 
                      doc_vecs: np.ndarray,
                      metric: str = 'cosine') -> np.ndarray:
    """
    Compute similarity between query and documents.
    
    Args:
        query_vec: (embedding_dim,)
        doc_vecs: (n_docs, embedding_dim)
        metric: 'cosine' or 'dot'
    
    Returns:
        similarities: (n_docs,)
    """
    if metric == 'cosine':
        from sklearn.metrics.pairwise import cosine_similarity
        query_vec = query_vec.reshape(1, -1)
        similarities = cosine_similarity(query_vec, doc_vecs)[0]
    else:  # dot product
        similarities = np.dot(doc_vecs, query_vec)
    
    return similarities


if __name__ == '__main__':
    # Test the embedding model
    print("Testing Embedding Model\n" + "="*50)
    
    # Sample texts
    texts = [
        "module/container/list/leaf/type | category: CONSTRAINT | keywords: pattern,type",
        "module/container/leaf | category: STRUCTURAL | keywords: leaf,container",
        "module/typedef/type | category: STRUCTURAL | keywords: typedef,type"
    ]
    
    # Initialize model
    model = EmbeddingModel()
    
    # Fit if TF-IDF
    if model.model_type == 'tfidf':
        model.fit(texts)
    
    # Encode
    embeddings = model.encode(texts, show_progress=False)
    
    print(f"\nEmbedding shape: {embeddings.shape}")
    print(f"Embedding dimension: {model.get_dimension()}")
    
    # Test similarity
    query = texts[0]
    query_vec = model.encode(query, show_progress=False)
    sims = compute_similarity(query_vec, embeddings)
    
    print(f"\nSimilarities to query:")
    for i, sim in enumerate(sims):
        print(f"  Doc {i}: {sim:.4f}")
    
    print("\n✓ Embedding model test passed!")
