#!/usr/bin/env python3
"""
Pre-download the embedding model to avoid timeouts during indexing
"""
import os
from sentence_transformers import SentenceTransformer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

MODEL_NAME = 'all-MiniLM-L6-v2'

def download_model():
    """Download the embedding model with progress indication"""
    console.print(f"[cyan]Downloading embedding model: {MODEL_NAME}[/cyan]")
    console.print("[yellow]This is a one-time download (~91MB)[/yellow]\n")
    
    try:
        # Set environment variable to show download progress
        os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '0'
        
        with console.status("[bold green]Downloading model..."):
            model = SentenceTransformer(MODEL_NAME)
        
        console.print(f"\n[green]✓ Model downloaded successfully![/green]")
        console.print(f"[dim]Model cached at: {model._model_card_vars.get('model_name', 'default cache')}[/dim]")
        
        # Test the model
        console.print("\n[cyan]Testing model...[/cyan]")
        test_text = "leaf ipv4-address { type inet:ipv4-address; }"
        embedding = model.encode([test_text])
        console.print(f"[green]✓ Model works! Embedding dimension: {len(embedding[0])}[/green]")
        
        return True
        
    except Exception as e:
        console.print(f"\n[red]✗ Download failed: {e}[/red]")
        console.print("\n[yellow]Troubleshooting:[/yellow]")
        console.print("1. Check your internet connection")
        console.print("2. If HuggingFace is blocked, try using a VPN")
        console.print("3. Alternative: Use HuggingFace CLI:")
        console.print("   [cyan]pip install -U huggingface_hub[/cyan]")
        console.print("   [cyan]huggingface-cli login[/cyan]  (optional)")
        console.print("   [cyan]huggingface-cli download sentence-transformers/all-MiniLM-L6-v2[/cyan]")
        return False


if __name__ == "__main__":
    console.print("="*60)
    console.print("[bold]Embedding Model Pre-Download Script[/bold]")
    console.print("="*60 + "\n")
    
    success = download_model()
    
    if success:
        console.print("\n[green]✓ Ready to run indexing![/green]")
        console.print("You can now run: [cyan]python code/scripts/index_extracted_yang.py[/cyan]")
    else:
        console.print("\n[red]Please fix the issues above and try again[/red]")
