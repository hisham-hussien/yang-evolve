#!/usr/bin/env python3
"""
Indexer for YANG-RAG
--------------------
Reads extracted JSON, generates embeddings, stores:
- embeddings.npz: NumPy compressed vectors
- metadata.jsonl: One JSON object per line
- graph.json: Graph structure (copied from extraction)

Supports category and depth filtering.
"""

import json
import numpy as np
from pathlib import Path
from typing import List, Dict, Any
from collections import defaultdict
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

# Add parent to path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from embedding import EmbeddingModel
from vector_store import FaissStore

console = Console()

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EXTRACTED_JSON = DATA_DIR / "yang_pyang_extracted.json"
GRAPH_JSON = DATA_DIR / "yang_graph.json"
INDEX_DIR = DATA_DIR / "index"


class YANGIndexer:
    """Index YANG documents with embeddings."""
    
    def __init__(self, model_name: str = 'all-MiniLM-L6-v2', use_gpu: bool = False, vector_backend: str = 'numpy'):
        self.model = EmbeddingModel(model_name=model_name, use_gpu=use_gpu)
        self.documents = []
        self.embeddings = None
        self.metadata = []
        self.stats = defaultdict(int)
        self.vector_backend = vector_backend
    
    def load_documents(self, json_path: Path = EXTRACTED_JSON):
        """Load extracted documents."""
        console.print(f"\n[cyan]Loading documents from:[/cyan]\n{json_path}")
        
        with open(json_path, 'r', encoding='utf-8') as f:
            self.documents = json.load(f)
        
        console.print(f"[green]✓ Loaded {len(self.documents)} documents[/green]")
        
        # Gather stats
        for doc in self.documents:
            cat = doc['metadata']['category']
            self.stats[cat] += 1
        
        console.print("\n[bold]Category distribution:[/bold]")
        for cat in ['STRUCTURAL', 'CONSTRAINT', 'ATTRIBUTE', 'EXTENSION', 'UNKNOWN']:
            if cat in self.stats:
                console.print(f"  {cat}: {self.stats[cat]}")
    
    def build_embeddings(self, batch_size: int = 64):
        """Build separate embeddings for structure and description."""
        console.print("\n[cyan]Building embeddings...[/cyan]")
        
        # Extract structure templates (always present)
        structure_templates = [doc['embedding_template'] for doc in self.documents]
        
        # Extract description templates (may be None)
        # Note: Description template is stored with key '0' in the extractor
        description_templates = [doc.get('0') for doc in self.documents]
        has_descriptions = [dt is not None for dt in description_templates]
        num_with_desc = sum(has_descriptions)
        
        console.print(f"  Structure templates: {len(structure_templates)}")
        console.print(f"  Description templates: {num_with_desc} ({100*num_with_desc/len(structure_templates):.1f}%)")
        
        # Fit TF-IDF if needed
        if self.model.model_type == 'tfidf':
            all_templates = structure_templates + [dt for dt in description_templates if dt]
            self.model.fit(all_templates)
        
        # Encode structure embeddings
        console.print("\n[yellow]Phase 1: Structure embeddings[/yellow]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("[cyan]Encoding structures...", total=len(structure_templates))
            
            # Batch encoding
            all_structure_embeddings = []
            for i in range(0, len(structure_templates), batch_size):
                batch = structure_templates[i:i+batch_size]
                batch_emb = self.model.encode(batch, show_progress=False)
                all_structure_embeddings.append(batch_emb)
                progress.update(task, advance=len(batch))
            
            structure_embeddings = np.vstack(all_structure_embeddings)
        
        console.print(f"[green]✓ Generated {structure_embeddings.shape[0]} structure embeddings[/green]")
        
        # Encode description embeddings (sparse - only for extensions with descriptions)
        console.print("\n[yellow]Phase 2: Description embeddings[/yellow]")
        # Create zero embeddings for docs without descriptions
        embedding_dim = structure_embeddings.shape[1]
        description_embeddings = np.zeros((len(description_templates), embedding_dim), dtype=np.float32)
        
        if num_with_desc > 0:
            # Get indices and templates for docs with descriptions
            desc_indices = [i for i, dt in enumerate(description_templates) if dt is not None]
            desc_texts = [description_templates[i] for i in desc_indices]
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]Encoding descriptions...", total=len(desc_texts))
                
                # Batch encoding
                all_desc_embeddings = []
                for i in range(0, len(desc_texts), batch_size):
                    batch = desc_texts[i:i+batch_size]
                    batch_emb = self.model.encode(batch, show_progress=False)
                    all_desc_embeddings.append(batch_emb)
                    progress.update(task, advance=len(batch))
                
                desc_emb_array = np.vstack(all_desc_embeddings)
                
                # Place embeddings at correct indices
                for i, idx in enumerate(desc_indices):
                    description_embeddings[idx] = desc_emb_array[i]
            
            console.print(f"[green]✓ Generated {len(desc_indices)} description embeddings[/green]")
        
        # Store both embeddings
        self.embeddings = structure_embeddings
        self.description_embeddings = description_embeddings
        
        console.print(f"\n[green]✓ Total embeddings created:[/green]")
        console.print(f"[dim]  Structure shape: {self.embeddings.shape}[/dim]")
        console.print(f"[dim]  Description shape: {self.description_embeddings.shape}[/dim]")
    
    def prepare_metadata(self):
        """Prepare metadata records for storage."""
        console.print("\n[cyan]Preparing metadata...[/cyan]")
        
        self.metadata = []
        for i, doc in enumerate(self.documents):
            meta = {
                'index': i,
                'node_id': doc['node_id'],
                'category': doc['metadata']['category'],
                'keywords': doc['metadata']['keywords'],
                'skeleton_path': doc['metadata']['keyword_skeleton_path'],
                'depth': doc['metadata']['depth'],
                'parent_id': doc['metadata'].get('parent_id'),
                'extension_imports': doc['metadata'].get('extension_imports', []),
                'module': doc['metadata']['module'],
                'path': doc['metadata']['path'],
                'yang_keyword': doc['metadata']['yang_keyword'],
                'snippet_masked': doc['metadata']['snippet_masked'],
                'display_text': doc['display_text'],
                # Alias for future search UX: searchable_text ~= raw/display text
                'searchable_text': doc.get('display_text', '')
            }
            self.metadata.append(meta)
        
        console.print(f"[green]✓ Prepared {len(self.metadata)} metadata records[/green]")
    
    def save_index(self, output_dir: Path = INDEX_DIR):
        """Save embeddings, metadata, and graph."""
        console.print(f"\n[cyan]Saving index to:[/cyan]\n{output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Save both structure and description embeddings
        embeddings_path = output_dir / "embeddings.npz"
        np.savez_compressed(
            embeddings_path,
            structure_embeddings=self.embeddings,
            description_embeddings=self.description_embeddings
        )
        console.print(f"[green]✓ Saved embeddings ({embeddings_path.stat().st_size / 1024 / 1024:.2f} MB)[/green]")
        console.print(f"[dim]  Structure: {self.embeddings.shape}, Description: {self.description_embeddings.shape}[/dim]")
        
        # Save metadata as JSONL
        metadata_path = output_dir / "metadata.jsonl"
        with open(metadata_path, 'w', encoding='utf-8') as f:
            for meta in self.metadata:
                f.write(json.dumps(meta, ensure_ascii=False) + '\n')
        console.print(f"[green]✓ Saved metadata ({metadata_path.stat().st_size / 1024 / 1024:.2f} MB)[/green]")
        
        # Copy graph if exists
        if GRAPH_JSON.exists():
            import shutil
            graph_dest = output_dir / "graph.json"
            shutil.copy(GRAPH_JSON, graph_dest)
            console.print(f"[green]✓ Copied graph ({graph_dest.stat().st_size / 1024 / 1024:.2f} MB)[/green]")
        
        # Save model
        model_dir = output_dir / "model"
        self.model.save(model_dir)
        
        # Save index stats
        stats_path = output_dir / "index_stats.json"
        stats = {
            'total_documents': len(self.documents),
            'embedding_dim': int(self.embeddings.shape[1]),
            'model_type': self.model.model_type,
            'model_name': self.model.model_name,
            'categories': dict(self.stats),
            'vector_backend': self.vector_backend
        }
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2)
        console.print(f"[green]✓ Saved index stats[/green]")
        
        # Build and save FAISS indices if selected (one for structure, one for description)
        if self.vector_backend.lower() == 'faiss':
            console.print("\n[cyan]Building FAISS indices...[/cyan]")
            try:
                # Structure FAISS index
                console.print("  Building structure index...")
                structure_store = FaissStore(dim=int(self.embeddings.shape[1]))
                structure_store.build(self.embeddings.astype('float32'))
                structure_store.save(output_dir, prefix='structure_')
                console.print("[green]✓ Saved structure FAISS index[/green]")
                
                # Description FAISS index
                console.print("  Building description index...")
                description_store = FaissStore(dim=int(self.description_embeddings.shape[1]))
                description_store.build(self.description_embeddings.astype('float32'))
                description_store.save(output_dir, prefix='description_')
                console.print("[green]✓ Saved description FAISS index[/green]")
            except Exception as e:
                console.print(f"[red]FAISS build failed:[/red] {e}")
                console.print("[yellow]Falling back to NumPy backend in stats.[/yellow]")
                # Update stats to reflect fallback
                stats['vector_backend'] = 'numpy'
                with open(stats_path, 'w', encoding='utf-8') as f:
                    json.dump(stats, f, indent=2)

        console.print(f"\n[bold green]✅ Index built successfully![/bold green]")
        console.print(f"\n[dim]Index location: {output_dir.absolute()}[/dim]")
    
    def build_partitioned_index(self, output_dir: Path = INDEX_DIR):
        """Build partitioned index by category for faster filtering."""
        console.print("\n[cyan]Building category-partitioned index...[/cyan]")
        
        partitions = defaultdict(lambda: {'indices': [], 'embeddings': [], 'metadata': []})
        
        for i, (doc, emb, meta) in enumerate(zip(self.documents, self.embeddings, self.metadata)):
            cat = doc['metadata']['category']
            partitions[cat]['indices'].append(i)
            partitions[cat]['embeddings'].append(emb)
            partitions[cat]['metadata'].append(meta)
        
        # Save each partition
        partition_dir = output_dir / "partitions"
        partition_dir.mkdir(parents=True, exist_ok=True)
        
        for cat, data in partitions.items():
            cat_dir = partition_dir / cat.lower()
            cat_dir.mkdir(parents=True, exist_ok=True)
            
            # Save embeddings
            emb_array = np.array(data['embeddings'])
            np.savez_compressed(cat_dir / "embeddings.npz", embeddings=emb_array)
            
            # Save metadata
            with open(cat_dir / "metadata.jsonl", 'w', encoding='utf-8') as f:
                for meta in data['metadata']:
                    f.write(json.dumps(meta, ensure_ascii=False) + '\n')
            
            console.print(f"[green]  ✓ {cat}: {len(data['indices'])} docs[/green]")
        
        console.print(f"[green]✓ Created {len(partitions)} category partitions[/green]")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Build embedding index for YANG-RAG",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--input',
        type=Path,
        default=EXTRACTED_JSON,
        help='Input JSON file with extracted documents'
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=INDEX_DIR,
        help='Output directory for index'
    )
    parser.add_argument(
        '--model',
        type=str,
        default='all-MiniLM-L6-v2',
        help='Embedding model name'
    )
    parser.add_argument(
        '--use-gpu',
        action='store_true',
        help='Use GPU (cuda) when encoding with sentence-transformers'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=64,
        help='Batch size for encoding'
    )
    parser.add_argument(
        '--vector-backend',
        type=str,
        choices=['numpy', 'faiss'],
        default='numpy',
        help='Vector backend to build (numpy in-memory or faiss)'
    )
    parser.add_argument(
        '--partitioned',
        action='store_true',
        help='Build category-partitioned index'
    )
    
    args = parser.parse_args()
    
    # Check input exists
    if not args.input.exists():
        console.print(f"[red]Error: Input file not found: {args.input}[/red]")
        console.print(f"\n[yellow]Please run the extractor first:[/yellow]")
        console.print(f"  python -m yang_rag.parsing.pyang_extractor")
        return
    
    # Build index
    console.print("[bold cyan]YANG-RAG Indexer[/bold cyan]")
    
    indexer = YANGIndexer(model_name=args.model, use_gpu=args.use_gpu, vector_backend=args.vector_backend)
    indexer.load_documents(args.input)
    indexer.build_embeddings(batch_size=args.batch_size)
    indexer.prepare_metadata()
    indexer.save_index(args.output)
    
    if args.partitioned:
        indexer.build_partitioned_index(args.output)
    
    console.print("\n[bold green]✅ Indexing Complete![/bold green]")
    console.print("\n[cyan]Next steps:[/cyan]")
    console.print(f"  1. Query: python -m yang_rag.rag.query \"<your YANG snippet>\"")
    console.print(f"  2. View index: ls {args.output}")


if __name__ == '__main__':
    main()
