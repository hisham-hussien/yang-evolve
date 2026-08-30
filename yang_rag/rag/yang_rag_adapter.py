#!/usr/bin/env python3
"""
YANG-RAG Adapter for Compatibility Pipeline
--------------------------------------------
Adapter to integrate the new YANG-RAG system with the existing
compatibility pipeline.

Maps the old interface (identify_yang_keyword) to the new query system.
"""

import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from yang_rag.rag.query import YANGQuery
from yang_rag.config import RAG_INITIAL_K
from rich.console import Console

console = Console()

# Default paths — use config.DATA_DIRECTORY which resolves correctly for both
# source installs (project_root/data/) and wheel installs
# (<sys.prefix>/share/yang-comparator-pro/data/).
try:
    from yang_rag.config import DATA_DIRECTORY as _DATA_DIR
    INDEX_DIR = Path(_DATA_DIR) / "index"
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    INDEX_DIR = PROJECT_ROOT / "data" / "index"


class YANGRAGAdapter:
    """Adapter to use new YANG-RAG system with old pipeline interface."""
    
    def __init__(self, index_dir: Path = INDEX_DIR, weights_override: Optional[Dict[str, float]] = None,
                 initial_k: int = RAG_INITIAL_K):
        """Initialize the adapter with YANG-RAG query engine.
        
        Args:
            index_dir: Path to the FAISS index directory.
            weights_override: Optional dict to override default reranking weights.
            initial_k: Initial candidate pool size for vector search (default: RAG_INITIAL_K from config).
                       Larger values improve recall for semantically distant queries
                       (e.g. vendor extensions vs. standard YANG keywords) at the
                       cost of slightly higher latency.
        """
        self.index_dir = Path(index_dir)
        self.query_engine = None
        self.weights_override = weights_override
        self.initial_k = initial_k
        
        # Lazy load the query engine
        self._initialize_engine()
    
    def _initialize_engine(self):
        """Initialize the YANG-RAG query engine."""
        if not self.index_dir.exists():
            console.print(f"[yellow]⚠ Index directory not found: {self.index_dir}[/yellow]")
            console.print("[yellow]  Please run: python yang_rag/parsing/pyang_extractor.py && python yang_rag/rag/indexer.py[/yellow]")
            return False
        
        try:
            self.query_engine = YANGQuery(
                index_dir=self.index_dir,
                weights_override=self.weights_override
            )
            return True
        except Exception as e:
            console.print(f"[red]❌ Failed to initialize YANG-RAG: {e}[/red]")
            return False
    
    def identify_keyword(
        self,
        yang_context: str,
        n_results: int = 10,
        min_similarity: float = 0.4,
        search_mode: str = "auto",  # Ignored - kept for compatibility
        yang_file: Optional[str] = None,  # NEW: Path to YANG file for accurate path extraction
        initial_k: Optional[int] = None,  # Override instance-level initial_k for this call
        **kwargs
    ) -> Dict[str, Any]:
        """
        Identify YANG keyword using the new RAG system.
        
        Maps old interface to new query system.
        
        Args:
            yang_context: YANG snippet to search for
            n_results: Number of results to return (minimum 5 distinct keywords will be returned)
            min_similarity: Minimum similarity threshold (0.0-1.0)
            search_mode: Ignored (kept for backward compatibility)
            yang_file: Optional path to YANG file containing the snippet (for accurate skeleton path)
            **kwargs: Additional arguments (ignored)
        
        Returns:
            Dictionary with:
                - keyword: Top matching keyword
                - similarity: Similarity score
                - category: YANG category
                - examples: List of result dictionaries with similarity scores
                - analysis_size: Total number of documents searched
        """
        if not self.query_engine:
            console.print("[red]❌ YANG-RAG not initialized[/red]")
            return {
                'keyword': None,
                'similarity': 0.0,
                'category': 'unknown',
                'examples': [],
                'analysis_size': 0
            }
        
        try:
            # Query the new system
            # Request MORE results initially (100+) to get diverse keywords
            # The query system returns best matching documents, which might all have the same keyword
            # We need a larger pool to find different keywords with varying scores
            # Always ensure we get at least enough results to show 5 distinct keywords
            min_distinct_keywords = 5
            requested_keywords = max(n_results, min_distinct_keywords)
            
            # Note: For GPU acceleration, install faiss-gpu and rebuild index with FAISS backend
            # Current NumPy backend is reasonably fast for this index size (163K documents)
            _initial_k = initial_k if initial_k is not None else self.initial_k
            results = self.query_engine.query(
                query_text=yang_context,
                top_k=_initial_k,  # Return all candidates so occurrence counts reflect full pool
                initial_k=_initial_k,  # Configurable candidate pool for better diversity
                show_scores=True,  # Include score components for detailed breakdown
                yang_file=yang_file,  # Pass YANG file for accurate skeleton path extraction
                max_keyword_occurrences=_initial_k
            )
            
            # Filter by minimum similarity
            filtered_results = [r for r in results if r['score'] >= min_similarity]
            
            if not filtered_results:
                return {
                    'keyword': None,
                    'similarity': 0.0,
                    'category': 'unknown',
                    'examples': [],
                    'analysis_size': len(results)
                }
            
            # Group results by keyword to show distinct keywords (not duplicate documents)
            # For each keyword, keep the highest scoring example
            keyword_best = {}  # keyword -> best result dict
            keyword_counts = {}  # keyword -> occurrence count
            
            for r in filtered_results:
                keyword = r['yang_keyword']
                category = r['category']
                score = r['score']
                
                # Count total occurrences of this keyword
                keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1
                
                # Keep the best (highest scoring) example for each keyword
                if keyword not in keyword_best or score > keyword_best[keyword]['similarity']:
                    keyword_best[keyword] = {
                        'keyword': keyword,
                        'similarity': score,
                        'type': category,
                        'occurrence_count': 0,  # Will be updated below
                        'module': r['module'],
                        'path': r['skeleton_path'],
                        'snippet': r['snippet'],
                        'display_text': r.get('display_text', ''),
                        'score_components': r.get('score_components')
                    }
            
            # Build unique examples list sorted by similarity score (highest first)
            examples = []
            for keyword in sorted(keyword_best.keys(), key=lambda k: keyword_best[k]['similarity'], reverse=True):
                ex = keyword_best[keyword]
                ex['occurrence_count'] = keyword_counts[keyword]
                examples.append(ex)
            
            # Ensure we have at least min_distinct_keywords (5) distinct keywords in results
            # If we have fewer, it means either:
            # 1. Very few matches above min_similarity threshold
            # 2. The query is very specific and only matches one keyword
            min_distinct_keywords = 5
            if len(examples) < min_distinct_keywords:
                console.print(f"[dim yellow]⚠ Only {len(examples)} distinct keyword(s) found above threshold {min_similarity:.1%}. Relaxing to 10.0%...[/dim yellow]")
                # Try lowering threshold to get more diverse keywords
                all_results_no_filter = [r for r in results if r['score'] >= 0.1]  # Lower threshold
                # Count distinct keywords in relaxed results
                relaxed_keywords = set(r['yang_keyword'] for r in all_results_no_filter)
                console.print(f"[dim yellow]  → Found {len(relaxed_keywords)} distinct keywords with relaxed threshold[/dim yellow]")
                
                if len(relaxed_keywords) > len(examples):
                    # Reprocess with lower threshold to get more keywords
                    keyword_best_relaxed = {}
                    keyword_counts_relaxed = {}
                    
                    for r in all_results_no_filter:
                        keyword = r['yang_keyword']
                        score = r['score']
                        keyword_counts_relaxed[keyword] = keyword_counts_relaxed.get(keyword, 0) + 1
                        
                        if keyword not in keyword_best_relaxed or score > keyword_best_relaxed[keyword]['similarity']:
                            keyword_best_relaxed[keyword] = {
                                'keyword': keyword,
                                'similarity': score,
                                'type': r['category'],
                                'occurrence_count': 0,
                                'module': r['module'],
                                'path': r['skeleton_path'],
                                'snippet': r['snippet'],
                                'display_text': r.get('display_text', ''),
                                'score_components': r.get('score_components')
                            }
                    
                    # Rebuild examples with relaxed threshold
                    examples = []
                    for keyword in sorted(keyword_best_relaxed.keys(), key=lambda k: keyword_best_relaxed[k]['similarity'], reverse=True):
                        ex = keyword_best_relaxed[keyword]
                        ex['occurrence_count'] = keyword_counts_relaxed[keyword]
                        examples.append(ex)
            
            # Get top keyword
            top_result = filtered_results[0]
            
            return {
                'keyword': top_result['yang_keyword'],
                'similarity': top_result['score'],
                'category': top_result['category'],
                'examples': examples[:max(n_results, min_distinct_keywords)],  # Return at least 5, or n_results if larger
                'analysis_size': len(self.query_engine.metadata)
            }
            
        except Exception as e:
            console.print(f"[red]❌ Query failed: {e}[/red]")
            import traceback
            traceback.print_exc()
            return {
                'keyword': None,
                'similarity': 0.0,
                'category': 'unknown',
                'examples': [],
                'analysis_size': 0
            }
    
    def query(
        self,
        query_text: str,
        top_k: int = 10,
        category_filter: Optional[str] = None,
        show_scores: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Direct query interface for advanced usage.
        
        Args:
            query_text: YANG snippet
            top_k: Number of results
            category_filter: Filter by category (STRUCTURAL, CONSTRAINT, ATTRIBUTE, EXTENSION)
            show_scores: Include score breakdowns
        
        Returns:
            List of result dictionaries
        """
        if not self.query_engine:
            console.print("[red]❌ YANG-RAG not initialized[/red]")
            return []
        
        return self.query_engine.query(
            query_text=query_text,
            top_k=top_k,
            show_scores=show_scores,
            category_filter=category_filter
        )


# Global instance for backward compatibility
_adapter_instance = None


def identify_keyword(
    yang_context: str,
    n_results: int = 10,
    min_similarity: float = 0.4,
    search_mode: str = "auto",
    yang_file: Optional[str] = None,  # NEW: Accept yang_file parameter
    **kwargs
) -> Dict[str, Any]:
    """
    Backward-compatible function for identify_yang_keyword.
    
    Uses the new YANG-RAG system under the hood.
    
    Args:
        yang_context: YANG snippet to search for
        n_results: Number of results
        min_similarity: Minimum similarity threshold
        search_mode: Ignored (kept for compatibility)
        yang_file: Optional path to YANG file containing the snippet
        **kwargs: Additional arguments
    """
    global _adapter_instance
    
    if _adapter_instance is None:
        _adapter_instance = YANGRAGAdapter()
    
    return _adapter_instance.identify_keyword(
        yang_context=yang_context,
        n_results=n_results,
        min_similarity=min_similarity,
        search_mode=search_mode,
        yang_file=yang_file,  # Pass yang_file to adapter
        **kwargs
    )


def query_yang_rag(
    query_text: str,
    top_k: int = 10,
    category_filter: Optional[str] = None,
    show_scores: bool = False,
    weights_override: Optional[Dict[str, float]] = None
) -> List[Dict[str, Any]]:
    """
    Direct query interface for YANG-RAG.
    
    Args:
        query_text: YANG snippet to search for
        top_k: Number of results to return
        category_filter: Optional category filter (STRUCTURAL, CONSTRAINT, ATTRIBUTE, EXTENSION)
        show_scores: Include detailed score breakdowns
        weights_override: Override default scoring weights
    
    Returns:
        List of result dictionaries with metadata and scores
    """
    adapter = YANGRAGAdapter(weights_override=weights_override)
    return adapter.query(
        query_text=query_text,
        top_k=top_k,
        category_filter=category_filter,
        show_scores=show_scores
    )


def display_results(result: dict, unknown_structure: str):
    """
    Display identification results in a nice format.
    
    This is a simplified version for backward compatibility.
    The new system already displays results in query.py
    
    Args:
        result: Result dictionary from identify_keyword()
        unknown_structure: The query string
    """
    from rich.panel import Panel
    from rich.table import Table
    
    # Show unknown structure
    console.print(Panel(
        unknown_structure,
        title="[yellow]🔍 Query[/yellow]",
        border_style="yellow"
    ))
    
    # Show primary result
    if result['matches']:
        primary = result['matches'][0]
        confidence_color = "green" if primary['score'] >= 0.8 else "yellow" if primary['score'] >= 0.6 else "red"
        
        console.print(Panel(
            f"[bold {confidence_color}]{primary['keyword'].upper()}[/bold {confidence_color}]\n\n"
            f"Score: {primary['score']:.1%}\n"
            f"Category: {primary['category']}\n"
            f"Signals: {primary.get('signal_count', 'N/A')}/9",
            title=f"[green]✅ Top Match[/green]",
            border_style="green"
        ))
    
    # Show top matches in table
    if result['matches']:
        console.print("\n[cyan]🎯 Top Matches:[/cyan]\n")
        
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("#", style="dim", width=3)
        table.add_column("Keyword", style="magenta", width=15)
        table.add_column("Score", justify="right", width=10)
        table.add_column("Category", style="yellow", width=15)
        table.add_column("Example", style="green", width=30)
        
        for i, match in enumerate(result['matches'][:10], 1):
            score_color = "green" if match['score'] >= 0.8 else "yellow" if match['score'] >= 0.6 else "red"
            table.add_row(
                str(i),
                match['keyword'],
                f"[{score_color}]{match['score']:.1%}[/{score_color}]",
                match['category'],
                match['name'][:30]
            )
        
        console.print(table)


if __name__ == '__main__':
    # Test the adapter
    console.print("[bold cyan]Testing YANG-RAG Adapter[/bold cyan]\n")
    
    # Test queries
    test_queries = [
        "leaf name { type string; }",
        "pattern \"[0-9]+\";",
        "must \"end-port >= start-port\";",
        "key \"name\";",
    ]
    
    for query in test_queries:
        console.print(f"\n[bold]Query:[/bold] {query}")
        result = identify_keyword(query, n_results=5)
        
        if result['keyword']:
            console.print(f"  [green]✓ Top Match:[/green] {result['keyword']} ({result['similarity']:.1%} similarity)")
            console.print(f"  [dim]Category: {result['category']}[/dim]")
            console.print(f"  [dim]Found {len(result['examples'])} examples from {result['analysis_size']} documents[/dim]")
        else:
            console.print(f"  [yellow]⚠ No matches found[/yellow]")
