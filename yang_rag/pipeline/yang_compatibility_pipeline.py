#!/usr/bin/env python3
"""
Self-Improving YANG Compatibility Pipeline
-------------------------------------------
Orchestrates the complete workflow:
1. Compare two YANG files using yang_comparator
2. Parse enriched_report.txt for unmarked statements
3. Generate rules using DSPy (with semantic search)
4. If semantic search confidence is low: Interactive user selection
   - Show top 10 distinct similar keywords
   - User selects the most appropriate one
   - Use selected keyword with DSPy to generate rule
5. Validate and optionally add rules to XML
6. Log all attempts for future optimization

This creates a feedback loop for continuous improvement.
"""

import os
import sys
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

# Add parent to path if needed (e.g. running from source)
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from yang_rag.dspy.pipeline import generate_rule_for_unknown_keyword, RuleGenerationResult
from yang_rag.dspy.xml_rule_updater import XMLRuleUpdater
from yang_rag.rag.yang_rag_adapter import identify_keyword
from yang_rag.comparator import yang_comparator, filter_report, check_compatibility  # Import comparator modules
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Confirm, Prompt, IntPrompt
from rich import box
import dspy

console = Console()

# Paths
# PACKAGE_ROOT for locating package resources (points to 'yang_rag' directory)
PACKAGE_ROOT = Path(__file__).parent.parent
# OUTPUT_DIR relative to current working directory
OUTPUT_DIR = Path("output")

COMPARATOR_DIR = PACKAGE_ROOT / "comparator"
COMPATIBILITY_XML = COMPARATOR_DIR / "compatibility_rules.xml"
TRAINING_LOG = PACKAGE_ROOT / "data" / "training_data.json"

ENRICHED_REPORT = OUTPUT_DIR / "enriched_report.txt"
# LLM verification outputs (modify enriched_report.txt in place, no separate _llm file)
LLM_VERIFICATION_JSON = OUTPUT_DIR / "llm_verification_report.json"
LLM_VERIFICATION_TXT = OUTPUT_DIR / "llm_verification_report.txt"


@dataclass
class UnmarkedStatement:
    """Represents a statement without compatibility annotation."""
    line_number: int
    raw_line: str
    subject_kind: Optional[str]  # 'keyword' or 'field'
    name: Optional[str]
    action: Optional[str]  # 'added', 'changed', 'deleted'
    is_constraint: bool
    parent_action: Optional[str]
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    surrounding_context: List[str] = None
    yang_context: Optional[str] = None  # Extracted from original YANG file


@dataclass
class RuleGenerationAttempt:
    """Log entry for training data."""
    timestamp: str
    unmarked_statement: Dict[str, Any]
    method: str  # 'dspy' or 'llm:<model_name>' or 'failed'
    generated_xml: Optional[str]
    validation_passed: bool
    validation_errors: List[str]
    user_approved: bool
    added_to_xml: bool
    notes: str = ""


class YangCompatibilityPipeline:
    """Orchestrates the self-improving compatibility checking pipeline."""
    
    def __init__(self, auto_approve: bool = False, confidence_threshold: float = 70.0, 
                 interactive: bool = True, search_mode: str = "auto",
                 enable_llm_verification: bool = False, llm_model: str = "gpt-4o-mini"):
        """
        Args:
            auto_approve: If True, automatically approve valid rules (skip user prompt)
            confidence_threshold: Minimum confidence for automatic DSPy rule generation (default: 70%)
            interactive: If True, prompt user to select keyword when confidence is low (default: True)
            search_mode: RAG search mode (default: "auto")
                - "auto": Auto-detect best database (recommended)
                - "improved": Force improved_extractor database
                - "hybrid": Pyang hybrid mode
                - "semantic": Pyang semantic mode 
                - "structural": Pyang structural mode
            enable_llm_verification: If True, run optional LLM verification for assistance="true" rules
            llm_model: LLM model identifier to use for verification
        """
        self.auto_approve = auto_approve
        self.confidence_threshold = confidence_threshold
        self.interactive = interactive
        self.search_mode = search_mode
        self.enable_llm_verification = enable_llm_verification
        self.llm_model = llm_model
        self.training_log: List[RuleGenerationAttempt] = []
        
        # Store YANG file paths for context extraction
        self.yang_file_1_path: Optional[Path] = None
        self.yang_file_2_path: Optional[Path] = None
        self._load_training_log()

    def run_llm_verification(self, enriched_report_path: Path) -> Optional[Dict[str, Any]]:
        """Run LLM verification on the enriched comparator report and write outputs.
        
        Creates two artifacts when changes requiring LLM assistance are found:
        - enriched_report_llm.txt: original report annotated with LLM verification notes
        - llm_verification_report.json and .txt: structured and human-readable summaries
        """
        try:
            if not self.enable_llm_verification:
                return None
            if not enriched_report_path.exists():
                console.print(f"[yellow]LLM verification skipped: report not found at {enriched_report_path}[/yellow]")
                return None

            # Lazy imports to avoid hard dependency when LLM is disabled
            from yang_comparator.check_compatibility import parse_rules
            from yang_rag.dspy.llm_verification import batch_verify_changes
            # Using helper functions from the hook module for extraction/injection
            from yang_rag.dspy.llm_verification_hook import _extract_changes_from_report, _inject_verification_results

            # Read report
            with open(enriched_report_path, 'r', encoding='utf-8') as f:
                enriched_lines = f.readlines()

            # Load rules
            rules = parse_rules(str(COMPATIBILITY_XML))

            # Extract changes that correspond to assistance="true" rules
            changes = _extract_changes_from_report(enriched_lines, rules)

            if not changes:
                console.print("[dim]No changes flagged for LLM verification (assistance=\"true\")[/dim]")
                return {"verified": 0, "changes": []}

            # Run verification in batch
            verifications = batch_verify_changes(changes, rules, llm_model=self.llm_model)

            # Inject verification results into the original report (in-place modification)
            enhanced_lines = _inject_verification_results(enriched_lines, verifications)

            # Ensure output dir
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            # Write back to the original enriched report (not a separate _llm file)
            with open(ENRICHED_REPORT, 'w', encoding='utf-8') as f:
                f.writelines(enhanced_lines)

            # Prepare JSON summary
            summary = {
                "model": self.llm_model,
                "verified": len(verifications),
                "timestamp": datetime.now().isoformat(),
                "items": verifications,
            }
            with open(LLM_VERIFICATION_JSON, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2)

            # Write compact text summary for quick viewing
            def _one_line(v: Dict[str, Any]) -> str:
                ch = v.get('change', {})
                vr = v.get('verification', {})
                return (
                    f"- {ch.get('rule_id','?')}::{ch.get('keyword_or_constraint','?')} "
                    f"[{ch.get('action','?')}] → {vr.get('verification_result','?')} "
                    f"({vr.get('confidence',0.0):.0%}) as {vr.get('verified_compatibility','?')}"
                )

            with open(LLM_VERIFICATION_TXT, 'w', encoding='utf-8') as f:
                f.write(f"LLM Verification Report (model: {self.llm_model})\n")
                f.write(f"Verified changes: {len(verifications)}\n\n")
                for v in verifications:
                    f.write(_one_line(v) + "\n")

            console.print(f"[green]✓ LLM verification complete. Updated: {ENRICHED_REPORT.name}, Summary: {LLM_VERIFICATION_JSON.name}[/green]")

            # After verification, regenerate grouped outputs using the updated enriched report
            try:
                regen = self._regenerate_grouped_outputs()
                if regen:
                    console.print("[green]✓ Regenerated final_report.json using LLM-verified report[/green]")
            except Exception as _e:
                console.print(f"[yellow]⚠ Could not regenerate grouped outputs: {_e}[/yellow]")

            return {"verified": len(verifications), "changes": verifications}
        except Exception as e:
            console.print(f"[yellow]⚠ LLM verification encountered an error: {e}[/yellow]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            return None

    def _regenerate_grouped_outputs(self) -> bool:
        """Regenerate compatible/non-compatible lists and final_report.json from enriched_report.txt.

        This makes final_report.json reflect any LLM updates when --llm-verify is enabled.
        """
        if not ENRICHED_REPORT.exists():
            return False
        env = os.environ.copy()
        env['YANG_ENRICHED_FILE'] = str(ENRICHED_REPORT)
        # Run the list generators with the potentially LLM-updated enriched report
        cmds = [
            [sys.executable, str(COMPARATOR_DIR / 'generate_compatibility_list.py')],
            [sys.executable, str(COMPARATOR_DIR / 'generate_non_compatibility_list.py')],
            [sys.executable, str(COMPARATOR_DIR / 'group_by_path.py'), '--out', str(OUTPUT_DIR / 'final_report.json')]
        ]
        for cmd in cmds:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if result.returncode != 0:
                console.print(f"[yellow]⚠ Command failed: {' '.join(cmd)}\n{result.stderr}[/yellow]")
                return False
        return True
    
    def _load_training_log(self):
        """Load existing training data."""
        if TRAINING_LOG.exists():
            try:
                with open(TRAINING_LOG, 'r') as f:
                    data = json.load(f)
                    self.training_log = [RuleGenerationAttempt(**entry) for entry in data]
                console.print(f"[dim]Loaded {len(self.training_log)} training examples[/dim]")
            except Exception as e:
                console.print(f"[yellow]Warning: Could not load training log: {e}[/yellow]")
    
    def _save_training_log(self):
        """Save training data for future optimization."""
        TRAINING_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TRAINING_LOG, 'w') as f:
            json.dump([asdict(attempt) for attempt in self.training_log], f, indent=2)
        console.print(f"[dim]Saved {len(self.training_log)} training examples[/dim]")
    
    def run_comparator(self, yang_file_1: str, yang_file_2: str, 
                       module_path_1: str, module_path_2: str) -> bool:
        """
        Run yang_comparator.sh to generate enriched_report.txt.
        
        Returns:
            True if successful, False otherwise
        """
        console.print("\n[bold cyan]Step 1/5: Running YANG Comparator[/bold cyan]")
        
        # Convert to Path objects
        yang_file_1_path = Path(yang_file_1).resolve()
        yang_file_2_path = Path(yang_file_2).resolve()
        module_path_1_abs = str(Path(module_path_1).resolve())
        module_path_2_abs = str(Path(module_path_2).resolve())
        
        # Store FULL file paths for YANG context extraction
        # If the files don't exist at the given path, try within the module directories
        if not yang_file_1_path.exists():
            yang_file_1_path = Path(module_path_1) / Path(yang_file_1).name
        if not yang_file_2_path.exists():
            yang_file_2_path = Path(module_path_2) / Path(yang_file_2).name
            
        self.yang_file_1_path = yang_file_1_path.resolve()
        self.yang_file_2_path = yang_file_2_path.resolve()
        console.print(f"[dim]  └─ Stored YANG paths for context extraction:[/dim]")
        console.print(f"[dim]     Old: {self.yang_file_1_path} (exists: {self.yang_file_1_path.exists()})[/dim]")
        console.print(f"[dim]     New: {self.yang_file_2_path} (exists: {self.yang_file_2_path.exists()})[/dim]")
        
        # Extract just the filenames - comparator looks for files inside module dirs
        yang_file_1_name = yang_file_1_path.name
        yang_file_2_name = yang_file_2_path.name
        
        try:
            console.print(f"[dim]Running comparator logic directly[/dim]")
            
            # Ensure output directory exists (relative to CWD)
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            
            report_file = str(OUTPUT_DIR / "report.txt")
            
            # Run the comparator
            # Note: main() might rely on being connected to stdout? It uses print().
            yang_comparator.main(
                yang_file_1_name,
                yang_file_2_name,
                module_path_1_abs,
                module_path_2_abs,
                output_file=report_file
            )
            
            # Run filter_report
            filtered_report_file = str(OUTPUT_DIR / "filtered_report.txt")
            filter_report.filter_and_write_report(report_file, filtered_report_file)
            
            # Run check_compatibility (enrichment)
            check_compatibility.enrich_report(
                filtered_report_file,
                str(COMPATIBILITY_XML),
                str(ENRICHED_REPORT),
                old_yang_file=str(self.yang_file_1_path),
                new_yang_file=str(self.yang_file_2_path),
                search_dirs=[module_path_1_abs, module_path_2_abs],
                llm_verify=self.enable_llm_verification,
            )
            
            # Copy to ENRICHED_REPORT (done by enrichment)
            # import shutil
            # shutil.copy(filtered_report_file, str(ENRICHED_REPORT))
            
            console.print(f"[green]✓ Comparator completed successfully[/green]")
            console.print(f"[dim]Report: {ENRICHED_REPORT}[/dim]")
            return True
            
        except SystemExit as e:
            if e.code != 0:
                console.print(f"[red]❌ Comparator exited with code {e.code}[/red]")
            return e.code == 0

        except Exception as e:
            console.print(f"[red]❌ Error running comparator: {e}[/red]")
            import traceback
            traceback.print_exc()
            return False
    
    def extract_yang_context(self, statement_name: str, action: str, 
                            yang_file_1_path: Path, yang_file_2_path: Path,
                            context_lines: int = 5, is_constraint: bool = False) -> Optional[str]:
        """
        Extract surrounding YANG context from the original source file.
        
        Args:
            statement_name: Name of the statement (e.g., 'prefix', 'namespace', 'regexp-posix')
            action: Action type ('added', 'changed', 'deleted')
            yang_file_1_path: Path to old YANG file
            yang_file_2_path: Path to new YANG file
            context_lines: Number of lines to extract before/after the statement
            is_constraint: If True, extract only the constraint line itself (not parent context)
        
        Returns:
            YANG code block with context, or None if not found
        """
        # Determine which file to read based on action
        source_file = yang_file_2_path if action in ['added', 'changed'] else yang_file_1_path
        
        if not source_file or not source_file.exists():
            return None
        
        try:
            with open(source_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Search for the statement in the file
            # Pattern: keyword_name with optional prefix (e.g., "prefix", "oc-ext:regexp-posix")
            import re
            
            best_match_idx = None
            best_match_priority = -1
            
            for i, line in enumerate(lines):
                # Skip comments and empty lines for better matching
                stripped = line.strip()
                if not stripped or stripped.startswith('//') or stripped.startswith('/*') or stripped.startswith('*'):
                    continue
                
                # Try multiple patterns to handle different cases:
                # Priority scoring: higher = better match
                # - Extension/attribute as statement (prefix:keyword) with ; gets highest priority (+15)
                # - Actual YANG statements (with ; or {) get +10 priority
                # - Prefixed statements get +3 base priority
                # - Exact matches get +2 base priority
                
                patterns = [
                    (r'[\w-]+:' + re.escape(statement_name) + r'\b', 3),  # Prefixed (e.g., oc-ext:regexp-posix, ep:endpoint)
                    (r'^\s*' + re.escape(statement_name) + r'\b', 2),     # Statement at line start (e.g., "privileges ...")
                    (r'\b' + re.escape(statement_name) + r'\b', 1),       # Anywhere in line (lowest priority)
                    (r'/' + re.escape(statement_name) + r'\b', 1)         # In path (e.g., /prefix)
                ]
                
                for pattern, base_priority in patterns:
                    if re.search(pattern, line):
                        # Boost priority if line looks like an actual YANG statement
                        priority = base_priority
                        
                        # Skip if the match is inside a quoted string
                        # This prevents matching "must" in error-message "MTU must be..."
                        match_obj = re.search(pattern, line)
                        if match_obj:
                            match_start = match_obj.start()
                            # Check if this position is inside quotes
                            before_match = line[:match_start]
                            # Count unescaped quotes before the match
                            double_quotes = before_match.count('"') - before_match.count(r'\"')
                            single_quotes = before_match.count("'") - before_match.count(r"\'")
                            if double_quotes % 2 == 1 or single_quotes % 2 == 1:
                                # Inside a quote, skip this match
                                continue
                        
                        # Check if this is an import/module/typedef NAME (not a statement)
                        # Pattern: import <name> { or typedef <name> { 
                        is_definition_name = bool(re.search(r'\b(import|module|typedef|grouping)\s+' + re.escape(statement_name) + r'\s*\{', stripped))
                        
                        if is_definition_name:
                            # This is a definition name (e.g., "import privileges {"), lower priority
                            priority += 5  # Lower priority than actual statements
                        elif base_priority == 3 and ';' in stripped:
                            # Prefixed extension with semicolon (e.g., "oc-ext:regexp-posix ...")
                            priority += 15  # Highest priority for extension statements
                        elif ';' in stripped:
                            # Statement with semicolon (e.g., "privileges 'value';")
                            priority += 12  # High priority for complete statements
                        elif '{' in stripped:
                            # Statement with opening brace (could be container, list, etc.)
                            priority += 10  # Good indicator but lower than semicolon statements
                        
                        # Update best match if this is higher priority
                        if priority > best_match_priority:
                            best_match_idx = i
                            best_match_priority = priority
                        break  # Found a match, stop checking other patterns for this line
            
            if best_match_idx is not None:
                # For constraints/extensions: extract ONLY the specific statement (single line/semicolon-terminated)
                if is_constraint:
                    # For must/when statements with blocks, we need to handle multi-line extraction properly
                    # Example: must ". >= 256" { error-message "..."; }
                    # We need to extract until the closing brace, not just the first semicolon
                    
                    statement_lines = []
                    brace_count = 0
                    found_opening_brace = False
                    
                    for i in range(best_match_idx, min(len(lines), best_match_idx + 20)):
                        line = lines[i]
                        statement_lines.append(line)
                        
                        # Count braces to detect blocks
                        brace_count += line.count('{') - line.count('}')
                        if '{' in line:
                            found_opening_brace = True
                        
                        # For statements without blocks (simple constraints), stop at semicolon
                        if not found_opening_brace and ';' in line:
                            break
                        
                        # For statements with blocks, stop when braces are balanced
                        if found_opening_brace and brace_count == 0:
                            break

                    # For RAG query matching, extract ONLY the statement itself (no parent context)
                    # The new YANG-RAG system uses single-statement embeddings, so adding parent
                    # context (like "module example-new {") dilutes the query and causes wrong matches
                    # E.g., "module example-new { prefix ex;" would match "module" instead of "prefix"
                    
                    # Build context with just the specific statement
                    yang_context = ''.join(statement_lines).strip()
                    
                    # For constraints like must/when, try to extract just the main statement
                    # Example: "must ". >= 256" { ... }" → extract the whole thing
                    # But remove leading/trailing context from other statements
                    import re
                    
                    # Check if this is a constraint with a block (must/when with { })
                    if statement_name in ['must', 'when'] and '{' in yang_context:
                        # Extract: constraint_name "condition" { ... }
                        # We need to properly balance braces
                        # Find the start of the statement
                        start_pos = yang_context.find(statement_name)
                        if start_pos >= 0:
                            # From statement_name, count braces until balanced
                            brace_depth = 0
                            in_statement = False
                            end_pos = start_pos
                            
                            for i in range(start_pos, len(yang_context)):
                                char = yang_context[i]
                                if char == '{':
                                    brace_depth += 1
                                    in_statement = True
                                elif char == '}':
                                    brace_depth -= 1
                                    if in_statement and brace_depth == 0:
                                        end_pos = i + 1
                                        break
                            
                            if end_pos > start_pos:
                                yang_context = yang_context[start_pos:end_pos].strip()
                    else:
                        # For simple constraints, extract just the single-line statement
                        match = re.search(r'([\w-]+:)?' + re.escape(statement_name) + r'\b[^;]*;', yang_context)
                        if match:
                            yang_context = match.group(0).strip()
                    
                    console.print(f"[dim cyan]    ✓ Found constraint '{statement_name}' at line {best_match_idx + 1} in {source_file.name}[/dim cyan]")
                    console.print(f"[dim cyan]      Extracted: {yang_context[:120]}...[/dim cyan]")
                else:
                    # For regular statements: extract surrounding context
                    start_idx = max(0, best_match_idx - context_lines)
                    end_idx = min(len(lines), best_match_idx + context_lines + 1)
                    context_block = lines[start_idx:end_idx]
                    yang_context = ''.join(context_block).strip()

                    # Try to capture nearest enclosing header above for better structural inference
                    header_line = None
                    import re as _re
                    header_pattern = _re.compile(r"\b(module|submodule|leaf-list|leaf|container|list|choice|case|grouping|typedef|type)\s+([A-Za-z0-9_\-:]+)\s*\{")
                    search_start = max(0, best_match_idx - 200)
                    for j in range(best_match_idx - 1, search_start - 1, -1):
                        m = header_pattern.search(lines[j])
                        if m:
                            header_line = lines[j].rstrip("\n")
                            break

                    if header_line:
                        yang_context = (header_line + "\n" + lines[best_match_idx].rstrip("\n")).strip()
                    
                    console.print(f"[dim cyan]    ✓ Found '{statement_name}' at line {best_match_idx + 1} in {source_file.name}[/dim cyan]")
                    console.print(f"[dim cyan]      Matched line: {lines[best_match_idx].strip()[:80]}...[/dim cyan]")
                    console.print(f"[dim cyan]      Priority: {best_match_priority}, Context size: {len(yang_context)} chars[/dim cyan]")
                
                # Limit context size to avoid overwhelming the semantic search
                if len(yang_context) > 500:
                    yang_context = yang_context[:500] + '...'
                
                return yang_context
            
            return None
            
        except Exception as e:
            console.print(f"[dim yellow]⚠ Could not extract YANG context from {source_file.name}: {e}[/dim yellow]")
            return None
    
    def parse_unmarked_statements(self) -> List[UnmarkedStatement]:
        """
        Parse enriched_report.txt to find unmarked statements.
        Unmarked = lines without <...> compatibility tags.
        
        Returns:
            List of UnmarkedStatement objects
        """
        console.print("\n[bold cyan]Step 2/5: Parsing Unmarked Statements[/bold cyan]")
        
        if not ENRICHED_REPORT.exists():
            console.print(f"[red]❌ Enriched report not found: {ENRICHED_REPORT}[/red]")
            return []
        
        with open(ENRICHED_REPORT, 'r') as f:
            lines = f.readlines()
        
        unmarked = []
        
        for i, line in enumerate(lines):
            raw = line.rstrip('\n')
            
            # Check if line has compatibility annotation (including deep-analysis marker)
            if ('<backward-compatible>' in raw or 
                '<non-backward-compatible>' in raw or 
                '<needs-deep-analysis>' in raw.lower()):
                continue  # Already marked (or flagged for LLM analysis)
            
            # Parse subject and action
            from yang_comparator.check_compatibility import parse_subject_action
            from yang_comparator.constraint_change import parse_old_new_from_line
            
            subj_kind, name, action, is_constraint = parse_subject_action(raw)
            
            if not action:
                continue  # Not a relevant line
            
            # Find parent action
            parent_action = None
            if raw.startswith(' '):
                j = i - 1
                while j >= 0:
                    parent_subj, _, parent_act, _ = parse_subject_action(lines[j].rstrip())
                    if parent_subj == 'keyword':
                        parent_action = parent_act
                        break
                    j -= 1
            
            # Extract old/new values
            old_val, new_val = parse_old_new_from_line(raw)
            
            # Collect surrounding context (3 lines before and after)
            context_start = max(0, i - 3)
            context_end = min(len(lines), i + 4)
            surrounding = [lines[j].rstrip('\n') for j in range(context_start, context_end)]
            
            # Extract YANG context from original source file
            yang_ctx = None
            if name and action and self.yang_file_1_path and self.yang_file_2_path:
                # ALWAYS extract narrow (single-statement) context for RAG queries
                # The pyang index contains single statements, so our query embeddings must match
                # Multi-line context dilutes the embedding and produces poor similarity scores
                should_extract_narrow = True
                
                console.print(f"[dim]  📄 Extracting context for '{name}' (action: {action}, narrow extraction: {should_extract_narrow})...[/dim]")
                yang_ctx = self.extract_yang_context(
                    statement_name=name,
                    action=action,
                    yang_file_1_path=self.yang_file_1_path,
                    yang_file_2_path=self.yang_file_2_path,
                    context_lines=5,
                    is_constraint=should_extract_narrow  # Always use narrow extraction for RAG matching
                )
                # Debug: Show extracted context
                if yang_ctx:
                    console.print(f"[dim green]  ✓ Extracted YANG context ({len(yang_ctx)} chars)[/dim green]")
                    console.print(Panel(
                        yang_ctx[:300] + ('...' if len(yang_ctx) > 300 else ''),
                        title=f"[cyan]Context for '{name}'[/cyan]",
                        border_style="dim",
                        padding=(0, 1)
                    ))
                else:
                    console.print(f"[dim yellow]  ⚠ Could not extract YANG context for '{name}'[/dim yellow]")
            else:
                console.print(f"[dim red]  ✗ YANG paths not available: file1={self.yang_file_1_path}, file2={self.yang_file_2_path}[/dim red]")
            
            unmarked.append(UnmarkedStatement(
                line_number=i + 1,
                raw_line=raw,
                subject_kind=subj_kind,
                name=name,
                action=action,
                is_constraint=is_constraint,
                parent_action=parent_action,
                old_value=str(old_val) if old_val is not None else None,
                new_value=str(new_val) if new_val is not None else None,
                surrounding_context=surrounding,
                yang_context=yang_ctx
            ))
        
        console.print(f"[green]✓ Found {len(unmarked)} unmarked statements[/green]")
        
        # Deduplicate unmarked statements by (name, action) to avoid processing duplicates
        seen = set()
        unique_unmarked = []
        for stmt in unmarked:
            key = (stmt.name, stmt.action)
            if key not in seen:
                seen.add(key)
                unique_unmarked.append(stmt)
        
        if len(unique_unmarked) < len(unmarked):
            console.print(f"[dim]  └─ Deduplicated: {len(unmarked)} → {len(unique_unmarked)} unique statements[/dim]")
        
        # Count how many have YANG context extracted
        with_context = sum(1 for stmt in unique_unmarked if stmt.yang_context)
        if with_context > 0:
            console.print(f"[dim]  └─ Extracted YANG context from source files: {with_context}/{len(unique_unmarked)}[/dim]")
        
        if unique_unmarked:
            table = Table(title="Unmarked Statements (Unique)")
            table.add_column("Line", style="cyan")
            table.add_column("Type", style="yellow")
            table.add_column("Name", style="green")
            table.add_column("Action", style="magenta")
            
            for stmt in unique_unmarked[:20]:  # Show first 20
                table.add_row(
                    str(stmt.line_number),
                    stmt.subject_kind or "?",
                    stmt.name or "?",
                    stmt.action or "?"
                )
            
            console.print(table)
            if len(unique_unmarked) > 20:
                console.print(f"[dim]... and {len(unique_unmarked) - 20} more[/dim]")
        
        return unique_unmarked
    
    def generate_rule_with_dspy(self, statement: UnmarkedStatement, 
                                override_keyword: Optional[str] = None) -> Optional[RuleGenerationResult]:
        """
        Generate compatibility rule using DSPy.
        
        Args:
            statement: The unmarked statement to generate a rule for
            override_keyword: If provided, use this keyword instead of semantic search
        
        Returns:
            RuleGenerationResult if successful, None otherwise
        """
        if override_keyword:
            console.print(f"\n[yellow]Generating DSPy rule with user-selected keyword: {override_keyword}[/yellow]")
        else:
            console.print(f"\n[yellow]Attempting DSPy rule generation for:[/yellow]")
            console.print(f"  {statement.raw_line}")
        
        # Build YANG context - use keyword name for better semantic search
        # Prefer YANG context from source file if available, otherwise use keyword name
        yang_context = statement.yang_context if statement.yang_context else statement.name
        if not statement.yang_context:
            # Include values for context, e.g., "prefix ex;" or "description \"text\";"
            value = statement.new_value or statement.old_value
            if value:
                # Check if value needs quotes (contains spaces, special chars, or already quoted)
                if '"' in value or "'" in value or not value.replace('-', '').replace('_', '').replace('.', '').replace(':', '').replace('/', '').isalnum():
                    # Value already has quotes or needs them
                    if not (value.startswith('"') and value.endswith('"')):
                        value = f'"{value}"'
                yang_context = f"{statement.name} {value};"
            else:
                yang_context = f"{statement.name};"
        
        try:
            result = generate_rule_for_unknown_keyword(
                unknown_yang_structure=yang_context,
                min_similarity=0.5,
                override_keyword=override_keyword  # Pass user-selected keyword
            )
            
            if result and result.generated_xml:
                console.print("[green]✓ DSPy generated rule[/green]")
                return result
            else:
                console.print("[yellow]⚠ DSPy returned no rule[/yellow]")
                return None
                
        except Exception as e:
            console.print(f"[red]❌ DSPy failed: {e}[/red]")
            return None
    
    def interactive_keyword_selection(self, statement: UnmarkedStatement) -> Optional[str]:
        """
        Interactive fallback: Show user similar keywords and let them choose.
        Dynamically adjusts to the number of results returned (up to 10).
        Includes category descriptions to help users make informed decisions.
        
        Args:
            statement: The unmarked statement
        
        Returns:
            Selected keyword string, or None if user cancels
        """
        console.print(f"\n[bold yellow]⚠ Semantic search confidence below threshold ({self.confidence_threshold}%)[/bold yellow]")
        console.print("[cyan]Let's find the most similar keyword interactively...[/cyan]\n")
        
        # Build YANG context - use keyword name for better semantic search
        # Prefer YANG context from source file if available, otherwise use keyword name
        yang_context = statement.yang_context if statement.yang_context else statement.name
        if not statement.yang_context:
            # Include values for context, e.g., "prefix ex;" or "description \"text\";"
            # For improved extractor, build proper YANG statement format
            value = statement.new_value or statement.old_value
            if value:
                # Check if value needs quotes (contains spaces, special chars, or already quoted)
                if '"' in value or "'" in value or not value.replace('-', '').replace('_', '').replace('.', '').replace(':', '').replace('/', '').isalnum():
                    # Value already has quotes or needs them
                    if not (value.startswith('"') and value.endswith('"')):
                        value = f'"{value}"'
                yang_context = f"{statement.name} {value};"
            else:
                yang_context = f"{statement.name};"
        
        # Debug: Show semantic search query
        console.print("[dim]═══ Semantic Search Query ═══[/dim]")
        if statement.yang_context:
            console.print(f"[dim green]✓ Using extracted YANG context ({len(yang_context)} chars):[/dim green]")
        else:
            console.print(f"[dim yellow]⚠ No YANG context available, using keyword name only[/dim yellow]")
        
        console.print(Panel(
            yang_context[:400] + ('...' if len(yang_context) > 400 else ''),
            title="[cyan]Query sent to RAG[/cyan]",
            border_style="cyan",
            padding=(0, 1)
        ))
        
        # Category descriptions to help users understand
        CATEGORY_DESCRIPTIONS = {
            'structural': '🏗️  Defines data structure (leaf, container, list, choice)',
            'attribute': '📝 Provides metadata (description, default, config, status)',
            'constraint': '🔒 Enforces rules (must, when, pattern, length, range)',
            'N/A': '❓ Unknown or mixed category'
        }
        
        try:
            # Always get at least 5 distinct keywords for better selection
            # Determine which YANG file to use based on action
            yang_file = self.yang_file_2_path if statement.action in ['added', 'changed'] else self.yang_file_1_path
            result = identify_keyword(yang_context, n_results=5, search_mode=self.search_mode, yang_file=str(yang_file) if yang_file else None)
            
            if not result or not result.get('examples'):
                console.print("[red]❌ No similar keywords found in database[/red]")
                return None
            
            examples = result['examples']
            num_choices = len(examples)
            
            # Display results in a table (show actual number of distinct candidates found)
            table = Table(title=f"Top {len(examples)} Keyword(s): [bold magenta]{statement.name}[/bold magenta]", 
                         show_header=True, header_style="bold cyan", show_lines=False)
            table.add_column("#", style="dim", width=3, no_wrap=True)
            table.add_column("Keyword", style="magenta", width=18, no_wrap=True)
            table.add_column("Occurrences", justify="center", width=12, no_wrap=True)
            table.add_column("Sim%", justify="right", width=6, no_wrap=True)
            table.add_column("Type", style="yellow", width=12, no_wrap=True)
            table.add_column("Scores", style="dim", no_wrap=False)  # Allow wrapping for full scores
            
            # Show only actual results (no padding)
            for i, ex in enumerate(examples, 1):
                sim_color = "green" if ex['similarity'] >= 0.8 else "yellow" if ex['similarity'] >= 0.6 else "red"
                count_color = "green" if ex['occurrence_count'] >= 10 else "yellow" if ex['occurrence_count'] >= 5 else "white"
                
                # Abbreviated category display
                category = ex.get('type', 'N/A')
                if 'ATTRIBUTE, CONSTRAINT' in category:
                    category_abbr = "Attr+Cons"
                elif 'ATTRIBUTE' in category:
                    category_abbr = "Attr"
                elif 'CONSTRAINT' in category:
                    category_abbr = "Cons"
                elif 'STRUCTURAL' in category:
                    category_abbr = "Struct"
                elif 'EXTENSION' in category:
                    category_abbr = "Ext"
                else:
                    category_abbr = category[:12]
                
                # Format score breakdown - show ALL non-zero scores
                score_components = ex.get('score_components')
                if score_components and isinstance(score_components, dict):
                    # Show all non-zero scores in descending order
                    scores_dict = {
                        'st': score_components.get('structure_sim', 0),
                        'ds': score_components.get('description_sim', 0),
                        'ph': score_components.get('path_shape', 0),
                        'kw': score_components.get('keyword_overlap', 0),
                        'ct': score_components.get('category_match', 0),
                        'nb': score_components.get('neighbor_boost', 0),
                        'lx': score_components.get('lexical_overlap', 0),
                        'tk': score_components.get('token_score', score_components.get('token_match', 0))
                    }
                    # Get ALL non-zero scores sorted by value (highest first)
                    all_scores = sorted([(k, v) for k, v in scores_dict.items() if v > 0.001], key=lambda x: x[1], reverse=True)
                    score_breakdown = ' '.join([f"{k}={v:.2f}" for k, v in all_scores]) if all_scores else "all-zero"
                else:
                    score_breakdown = "..."
                
                table.add_row(
                    str(i),
                    ex['keyword'][:18],  # Truncate long keywords
                    f"[{count_color}]{ex['occurrence_count']}[/{count_color}]",
                    f"[{sim_color}]{ex['similarity']*100:.0f}[/{sim_color}]",
                    category_abbr,
                    score_breakdown
                )
            
            console.print(table)
            
            # Show scoring weights (constant across all results) - compact version
            console.print("\n[dim]Weights: st=35% ds=5% ph=10% kw=8% ct=10% nb=7% lx=5% bm25=5% tk=15%[/dim]")
            console.print("[dim]Scores: structure(st) description(ds) path(ph) keyword(kw) category(ct) neighbor(nb) lexical(lx) token(tk)[/dim]")
            
            # Show context
            console.print(f"\n[cyan]Statement context:[/cyan]")
            console.print(f"  [bold]{statement.raw_line}[/bold]")
            
            # Add similarity explanation
            best_similarity = 0.0
            if examples:
                best_similarity = examples[0]['similarity']
                best_similarity_pct = best_similarity * 100
                
                if best_similarity >= 0.8:
                    sim_advice = "[green]🎯 High similarity - likely good match[/green]"
                elif best_similarity >= 0.6:
                    sim_advice = "[yellow]🤔 Medium similarity - consider context carefully[/yellow]"
                else:
                    sim_advice = "[red]⚠️  Low similarity - may need manual research[/red]"
                console.print(f"\n{sim_advice}")
                
                # Auto-approve if similarity exceeds threshold
                if best_similarity_pct >= self.confidence_threshold:
                    selected_keyword = examples[0]['keyword']
                    selected_category = examples[0].get('type', 'N/A')
                    console.print(f"\n[bold green]✓ Auto-selected (similarity {best_similarity_pct:.1f}% >= threshold {self.confidence_threshold}%): {selected_keyword} ({selected_category})[/bold green]")
                    return selected_keyword
            
            # Dynamic prompt based on actual number of choices
            console.print("\n[bold]Please select the most similar keyword:[/bold]")
            console.print("  • Enter 0 to skip this statement")
            console.print("  • Press Ctrl+C to exit pipeline")
            
            # Create choice list dynamically
            valid_choices = [str(i) for i in range(num_choices + 1)]  # 0 to num_choices
            
            choice = IntPrompt.ask(
                "Your choice",
                choices=valid_choices,
                default="0"
            )
            
            if choice == 0:
                console.print("[yellow]⊘ Skipping this statement[/yellow]")
                return None
            
            selected_keyword = examples[choice - 1]['keyword']
            selected_category = examples[choice - 1].get('type', 'N/A')
            console.print(f"[green]✓ Selected: {selected_keyword} ({selected_category})[/green]")
            
            return selected_keyword
            
        except KeyboardInterrupt:
            console.print("\n[yellow]⊘ User cancelled[/yellow]")
            raise
        except Exception as e:
            console.print(f"[red]❌ Interactive selection failed: {e}[/red]")
            return None
    
    def validate_generated_xml(self, xml_string: str) -> Tuple[bool, List[str]]:
        """
        Validate generated XML rule(s).
        
        Handles both single <rule> and multiple <rule> elements wrapped in <rules>.
        
        Checks:
        - Well-formed XML
        - Has required fields: rule-id, keywords/attributes/constraints, compatible
        - Parseable structure
        
        Returns:
            (is_valid, list_of_errors)
        """
        errors = []
        
        try:
            # Try to parse as-is first
            try:
                root = ET.fromstring(xml_string)
            except ET.ParseError:
                # If it fails, try wrapping in <rules> container
                wrapped_xml = f"<rules>\n{xml_string}\n</rules>"
                root = ET.fromstring(wrapped_xml)
            
            # Determine if single rule or multiple rules
            if root.tag == 'rule':
                # Single rule
                rules_to_check = [root]
            elif root.tag == 'rules':
                # Multiple rules wrapped
                rules_to_check = root.findall('rule')
                if not rules_to_check:
                    errors.append("No <rule> elements found in <rules> container")
                    return (False, errors)
            else:
                errors.append(f"Root element must be <rule> or <rules>, not <{root.tag}>")
                return (False, errors)
            
            # Validate each rule
            for idx, rule in enumerate(rules_to_check, 1):
                rule_prefix = f"Rule #{idx}: " if len(rules_to_check) > 1 else ""
                
                # Check required fields
                rule_id = rule.findtext('rule-id')
                if not rule_id:
                    errors.append(f"{rule_prefix}Missing <rule-id>")
                
                # Check that it has at least ONE of: structurals, attributes, or constraints
                # Updated to use 'structurals/structural' instead of 'keywords/keyword'
                keywords = rule.findall('structurals/structural')
                attributes = rule.findall('attributes/attribute')
                constraints = rule.findall('constraints/constraint')
                
                if not (keywords or attributes or constraints):
                    errors.append(f"{rule_prefix}Missing <structurals>, <attributes>, or <constraints>")
                
                # Check actions
                actions = rule.findall('actions/action')
                if not actions:
                    errors.append(f"{rule_prefix}Missing <actions>")
                
                # Check compatible
                compatible = rule.findtext('compatible')
                if not compatible:
                    errors.append(f"{rule_prefix}Missing <compatible>")
                elif compatible not in ['backward-compatible', 'non-backward-compatible', 'conditional-backward-compatible']:
                    errors.append(f"{rule_prefix}Invalid <compatible> value: {compatible}")
            
            return (len(errors) == 0, errors)
            
        except ET.ParseError as e:
            errors.append(f"XML parsing error: {e}")
            return (False, errors)
    
    def add_rule_to_xml(self, xml_rule: str, prompt_user: bool = True) -> bool:
        """
        Add generated rule(s) to compatibility_rules.xml.
        Handles both single <rule> and multiple <rule> elements.
        
        Args:
            xml_rule: The XML string containing <rule>...</rule> or multiple rules
            prompt_user: If True, ask user for approval
        
        Returns:
            True if added, False otherwise
        """
        if prompt_user and not self.auto_approve:
            console.print("\n[bold]Generated Rule(s):[/bold]")
            console.print(Panel(xml_rule, title="XML Rule(s)", border_style="cyan"))
            
            if not Confirm.ask("Add this/these rule(s) to compatibility_rules.xml?"):
                console.print("[yellow]⊘ Rule(s) rejected by user[/yellow]")
                return False
        
        try:
            # Parse existing XML
            tree = ET.parse(COMPATIBILITY_XML)
            root = tree.getroot()
            
            # Parse new rule(s)
            try:
                new_element = ET.fromstring(xml_rule)
            except ET.ParseError:
                # Try wrapping in container
                wrapped = f"<rules>\n{xml_rule}\n</rules>"
                new_element = ET.fromstring(wrapped)
            
            # Determine if single or multiple rules
            if new_element.tag == 'rule':
                # Single rule
                rules_to_add = [new_element]
            elif new_element.tag == 'rules':
                # Multiple rules in container
                rules_to_add = new_element.findall('rule')
            else:
                console.print(f"[red]❌ Unexpected root tag: <{new_element.tag}>[/red]")
                return False
            
            # Append each rule to root
            for rule in rules_to_add:
                root.append(rule)
            
            # Write back with pretty formatting
            ET.indent(tree, space="  ")
            tree.write(COMPATIBILITY_XML, encoding='utf-8', xml_declaration=True)
            
            count = len(rules_to_add)
            plural = "s" if count > 1 else ""
            console.print(f"[green]✓ {count} rule{plural} added to {COMPATIBILITY_XML}[/green]")
            return True
            
        except Exception as e:
            console.print(f"[red]❌ Failed to add rule(s): {e}[/red]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            return False
    
    def clone_constraint_in_existing_rules(self, new_keyword: str, similar_keyword: str, 
                                           prompt_user: bool = True) -> bool:
        """
        Clone a constraint keyword into existing rules (instead of creating new rules).
        This preserves existing rules and just adds new constraint lines.
        
        Args:
            new_keyword: The new constraint keyword to add
            similar_keyword: The existing similar constraint to clone from
            prompt_user: If True, ask user for approval
            
        Returns:
            True if cloned successfully, False otherwise
        """
        try:
            # Initialize XML updater
            updater = XMLRuleUpdater(str(COMPATIBILITY_XML))
            
            # Check if new keyword already exists
            if updater.keyword_exists(new_keyword):
                console.print(f"[yellow]⚠️  Keyword '{new_keyword}' already exists in rules[/yellow]")
                return False
            
            # Find similar keyword
            matches = updater.find_similar_keyword_in_rules(similar_keyword, exact_match=True)
            
            if not matches:
                console.print(f"[yellow]⚠️  Similar keyword '{similar_keyword}' not found in any rules[/yellow]")
                return False
            
            # Show preview
            if prompt_user and not self.auto_approve:
                console.print(f"\n[bold cyan]Preview: Cloning '{similar_keyword}' → '{new_keyword}'[/bold cyan]")
                console.print(f"Found {len(matches)} occurrence(s) in rules:\n")
                
                for i, (rule, category, item) in enumerate(matches, 1):
                    rule_id = rule.find('rule-id')
                    rule_id_text = rule_id.text if rule_id is not None else "unknown"
                    
                    console.print(f"{i}. Rule: [bold]{rule_id_text}[/bold]")
                    console.print(f"   Category: <{category}>")
                    
                    # Show source constraint (with attributes if present)
                    if item.attrib:
                        attrs_str = ' '.join(f'{k}="{v}"' for k, v in item.attrib.items())
                        console.print(f"   Keep:    <{item.tag} {attrs_str}>{similar_keyword}</{item.tag}>")
                        console.print(f"   Add:     <{item.tag} {attrs_str}>{new_keyword}</{item.tag}> [dim](attributes cloned)[/dim]")
                    else:
                        console.print(f"   Keep:    <{item.tag}>{similar_keyword}</{item.tag}>")
                        console.print(f"   Add:     <{item.tag}>{new_keyword}</{item.tag}>")
                    console.print("")
                
                if not Confirm.ask(f"Clone '{similar_keyword}' to add '{new_keyword}' in {len(matches)} rule(s)?"):
                    console.print("[yellow]⊘ Clone rejected by user[/yellow]")
                    return False
            
            # Perform the clone
            count = updater.clone_and_insert_keyword(new_keyword, similar_keyword, preserve_attributes=True)
            
            if count > 0:
                # Save with backup
                updater.save(backup=True)
                console.print(f"[green]✓ Cloned '{new_keyword}' into {count} rule(s)[/green]")
                console.print(f"[dim]Backup saved: {COMPATIBILITY_XML}.bak[/dim]")
                return True
            else:
                console.print(f"[red]❌ No changes made[/red]")
                return False
                
        except Exception as e:
            console.print(f"[red]❌ Failed to clone constraint: {e}[/red]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            return False
    
    def process_unmarked_statements(self, unmarked: List[UnmarkedStatement]) -> int:
        """
        Process all unmarked statements:
        1. Try DSPy with semantic search
        2. If confidence < threshold: Interactive user selection
        3. Use selected keyword with DSPy
        4. Validate
        5. Optionally add to XML
        6. Log for training
        
        Returns:
            Number of rules successfully added
        """
        console.print(f"\n[bold cyan]Step 3/5: Generating Rules for {len(unmarked)} Statements[/bold cyan]")
        
        statements_processed = 0  # Track successfully processed statements
        
        for i, statement in enumerate(unmarked, 1):
            console.print(f"\n[bold]Processing {i}/{len(unmarked)}[/bold]")
            console.print(f"[dim]{statement.raw_line}[/dim]")
            
            generated_xml = None
            method = None
            selected_keyword = None  # Track the similar keyword for cloning
            
            # Universal cloning approach for ALL statement types (constraints, keywords, attributes)
            # This prevents creating duplicate rules with same rule-id
            statement_type = 'constraint' if statement.is_constraint else 'keyword/attribute'
            console.print(f"[cyan]ℹ️  Detected {statement_type} - will use cloning approach to update existing rules[/cyan]")
            
            # Get similar keyword via RAG (using new YANG-RAG adapter)
            from yang_rag.rag.yang_rag_adapter import identify_keyword
            yang_context = statement.yang_context if statement.yang_context else statement.name
            if not statement.yang_context:
                # Include values for context, e.g., "prefix ex;" or "regexp-posix \"^pattern$\";"
                # For improved extractor, build proper YANG statement format
                value = statement.new_value or statement.old_value
                if value:
                    # Check if value needs quotes (contains spaces, special chars, or already quoted)
                    if '"' in value or "'" in value or not value.replace('-', '').replace('_', '').replace('.', '').replace(':', '').replace('/', '').isalnum():
                        # Value already has quotes or needs them
                        if not (value.startswith('"') and value.endswith('"')):
                            value = f'"{value}"'
                    yang_context = f"{statement.name} {value};"
                else:
                    yang_context = f"{statement.name};"
            
            # Debug: Show what we're searching with
            console.print("\n[dim]═══ RAG Query for Cloning ═══[/dim]")
            if statement.yang_context:
                console.print(f"[dim green]✓ Using extracted YANG context ({len(yang_context)} chars)[/dim green]")
            else:
                console.print(f"[dim yellow]⚠ Using keyword name only: '{yang_context}'[/dim yellow]")
            
            if len(yang_context) > 50:
                console.print(Panel(
                    yang_context[:300] + ('...' if len(yang_context) > 300 else ''),
                    title="[cyan]RAG Search Query[/cyan]",
                    border_style="dim cyan",
                    padding=(0, 1)
                ))
            
            try:
                # Determine which YANG file to use based on action
                yang_file = self.yang_file_2_path if statement.action in ['added', 'changed'] else self.yang_file_1_path
                rag_result = identify_keyword(yang_context, n_results=5, search_mode=self.search_mode, yang_file=str(yang_file) if yang_file else None)
                if rag_result and rag_result.get('keyword'):
                    # Check if we should use interactive mode or auto-approve
                    if self.interactive and not self.auto_approve:
                        # Let user select from multiple similar keywords
                        selected_keyword = self.interactive_keyword_selection(statement)
                        
                        if not selected_keyword:
                            console.print("[yellow]⊘ User skipped keyword selection[/yellow]")
                            self._log_attempt(statement, None, 'user-skipped', False, ['User skipped'], False, False)
                            continue
                        
                        method = f'{statement_type}-clone-interactive'
                    else:
                        # Auto mode: use top RAG result
                        selected_keyword = rag_result['keyword']
                        similarity = rag_result.get('examples', [{}])[0].get('similarity', 0.0) if rag_result.get('examples') else 0.0
                        console.print(f"[green]✓ Found similar {statement_type}: {selected_keyword} ({similarity:.1%} similarity)[/green]")
                        method = f'{statement_type}-clone-auto'
                    
                    # Clone directly into existing rules (no new rules created!)
                    added = self.clone_constraint_in_existing_rules(
                        new_keyword=statement.name,
                        similar_keyword=selected_keyword,
                        prompt_user=not self.auto_approve
                    )
                    
                    if added:
                        console.print(f"[green]✓ Successfully cloned {statement_type} into existing rules[/green]")
                        statements_processed += 1
                        # Log the successful clone
                        self._log_attempt(statement, f"<cloned from='{selected_keyword}'/>", method, True, [], True, True)
                    else:
                        console.print(f"[yellow]⚠ Clone failed[/yellow]")
                        self._log_attempt(statement, None, f'{statement_type}-clone-failed', False, ['Clone failed'], False, False)
                else:
                    console.print(f"[yellow]⚠ No similar {statement_type} found via RAG[/yellow]")
                    self._log_attempt(statement, None, f'no-similar-{statement_type}', False, ['No RAG match'], False, False)
            except Exception as e:
                console.print(f"[red]❌ RAG lookup failed: {e}[/red]")
                self._log_attempt(statement, None, 'rag-error', False, [str(e)], False, False)
            
            # All processing complete (cloning approach handles everything)
            # No need for additional DSPy rule generation
        
        return statements_processed
    
    def _log_attempt(self, statement: UnmarkedStatement, xml: Optional[str], 
                     method: str, valid: bool, errors: List[str], 
                     approved: bool, added: bool):
        """Log generation attempt for training."""
        attempt = RuleGenerationAttempt(
            timestamp=datetime.now().isoformat(),
            unmarked_statement=asdict(statement),
            method=method,
            generated_xml=xml,
            validation_passed=valid,
            validation_errors=errors,
            user_approved=approved,
            added_to_xml=added
        )
        self.training_log.append(attempt)
    
    def run_full_pipeline(self, yang_file_1: str, yang_file_2: str,
                          module_path_1: str, module_path_2: str) -> Dict[str, Any]:
        """
        Run the complete self-improving pipeline.
        
        Returns:
            Summary dictionary with results
        """
        console.print(Panel.fit(
            "[bold cyan]🔄 YANG Compatibility Self-Improving Pipeline[/bold cyan]\n"
            "Workflow: Compare → Parse → Generate → Validate → Update → Re-run",
            border_style="cyan"
        ))
        
        # Step 1: Run comparator
        if not self.run_comparator(yang_file_1, yang_file_2, module_path_1, module_path_2):
            return {"success": False, "error": "Comparator failed"}
        
        # Optional: LLM verification directly on comparator report
        if self.enable_llm_verification:
            console.print("\n[bold cyan]Optional: Running LLM Verification on Comparator Report[/bold cyan]")
            self.run_llm_verification(ENRICHED_REPORT)
            # Regenerate lists to include LLM verification results
            console.print("[dim]Regenerating compatibility lists with LLM verification results...[/dim]")
            self._regenerate_grouped_outputs()

        # Step 2: Parse unmarked
        unmarked = self.parse_unmarked_statements()
        
        if not unmarked:
            console.print("\n[green]✅ All statements are marked! No work needed.[/green]")
            return {"success": True, "unmarked_count": 0, "rules_added": 0}
        
        # Step 3-4: Generate and add rules
        statements_processed = self.process_unmarked_statements(unmarked)
        
        # Step 5: Save training log
        console.print("\n[bold cyan]Step 4/5: Saving Training Log[/bold cyan]")
        self._save_training_log()
        
        # Step 6: Re-run comparator if statements were processed
        if statements_processed > 0:
            console.print(f"\n[bold cyan]Step 5/5: Re-running Comparator with Updated Rules[/bold cyan]")
            self.run_comparator(yang_file_1, yang_file_2, module_path_1, module_path_2)

            # Run LLM verification again on the updated report if enabled
            if self.enable_llm_verification:
                console.print("\n[bold cyan]Optional: Re-running LLM Verification on Updated Report[/bold cyan]")
                self.run_llm_verification(ENRICHED_REPORT)
                # Regenerate lists to include LLM verification results
                console.print("[dim]Regenerating compatibility lists with LLM verification results...[/dim]")
                self._regenerate_grouped_outputs()
            
            # Check if new unmarked statements exist
            unmarked_after = self.parse_unmarked_statements()
            improvement = len(unmarked) - len(unmarked_after)
            
            console.print(f"\n[bold green]✅ Pipeline Complete![/bold green]")
            console.print(f"  • Statements processed: {statements_processed}")
            console.print(f"  • Unmarked before: {len(unmarked)}")
            console.print(f"  • Unmarked after: {len(unmarked_after)}")
            console.print(f"  • Improvement: {improvement} statements now marked")
            
            if improvement < statements_processed:
                console.print(f"[yellow]⚠️  Note: Some processed statements still unmarked (may need comparator re-run)[/yellow]")
        else:
            console.print("\n[yellow]⚠ No statements were processed[/yellow]")
        
        return {
            "success": True,
            "unmarked_count": len(unmarked),
            "statements_processed": statements_processed,
            "training_examples": len(self.training_log)
        }


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Self-Improving YANG Compatibility Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with interactive mode (default, using auto-detected database)
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2
  
  # Run with semantic search mode
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2 --search-mode semantic
  
  # Run with structural search mode (best for exact statement type matching)
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2 --search-mode structural
  
  # Run with auto-approve and hybrid search
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2 --auto-approve --search-mode hybrid
  
  # Run with higher similarity threshold (80%% - auto-select when similarity >= 80%%)
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2 --similarity-threshold 80
  
  # Run with lower similarity threshold (60%% - auto-select when similarity >= 60%%)
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2 --similarity-threshold 60
  
  # Run without interactive mode (skip low-confidence statements)
  %(prog)s old.yang new.yang /path/to/modules1 /path/to/modules2 --no-interactive
        """
    )
    parser.add_argument('yang_file_1', help='First YANG file (old version)')
    parser.add_argument('yang_file_2', help='Second YANG file (new version)')
    parser.add_argument('module_path_1', help='Module search path for first file')
    parser.add_argument('module_path_2', help='Module search path for second file')
    parser.add_argument('--auto-approve', action='store_true',
                       help='Automatically approve valid rules (no user prompt)')
    parser.add_argument('--similarity-threshold', '--confidence-threshold', type=float, default=70.0,
                       dest='confidence_threshold',
                       help='Minimum similarity percentage for automatic keyword selection (default: 70.0). '
                            'If the best match has similarity >= this threshold, it will be auto-selected without user input.')
    parser.add_argument('--no-interactive', action='store_true',
                       help='Disable interactive keyword selection (skip low-confidence statements)')
    parser.add_argument('--search-mode', type=str, default='auto',
                       choices=['auto', 'improved', 'hybrid', 'semantic', 'structural'],
                       help='RAG search mode: auto (default, auto-detect best database), '
                            'improved (force improved_extractor), hybrid (pyang hybrid), '
                            'semantic (pyang semantic), structural (pyang structural)')
    parser.add_argument('--llm-verify', action='store_true',
                       help='Enable LLM verification for rules with assistance="true" and write a separate report')
    parser.add_argument('--llm-model', type=str, default='gpt-4o-mini',
                       help='LLM model identifier to use for verification (default: gpt-4o-mini)')
    
    args = parser.parse_args()
    
    pipeline = YangCompatibilityPipeline(
        auto_approve=args.auto_approve,
        confidence_threshold=args.confidence_threshold,
        interactive=not args.no_interactive,
        search_mode=args.search_mode,
        enable_llm_verification=args.llm_verify,
        llm_model=args.llm_model
    )
    result = pipeline.run_full_pipeline(
        args.yang_file_1,
        args.yang_file_2,
        args.module_path_1,
        args.module_path_2
    )
    
    if result['success']:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
