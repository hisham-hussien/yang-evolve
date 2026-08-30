"""
XML Rule Updater for compatibility_rules.xml
---------------------------------------------
Updates the compatibility_rules.xml file by cloning similar constraint/keyword lines
and inserting new ones underneath, based on semantic search results.

Interactive editing
-------------------
When ``interactive=True`` (the default), the proposed cloned rule XML is
presented in a full-screen prompt_toolkit editor before being written to disk.
The user can freely modify any field — rule-id, compatible, set-change, actions,
etc. — and the edited XML is validated before acceptance.  Pass
``interactive=False`` to skip the editor (e.g. in ``--no-interactive`` mode).
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from rich.console import Console
from xml.dom import minidom

console = Console()

# Lazy import so the module loads even when prompt_toolkit is absent
def _get_editor():
    try:
        from yang_rag.dspy.rule_editor import edit_rule_xml
        return edit_rule_xml
    except ImportError:
        return None


class XMLRuleUpdater:
    """Handles reading and updating compatibility_rules.xml file."""
    
    def __init__(self, xml_path: str):
        """Initialize with path to compatibility_rules.xml.
        
        Args:
            xml_path: Path to compatibility_rules.xml file
        """
        self.xml_path = Path(xml_path)
        if not self.xml_path.exists():
            raise FileNotFoundError(f"XML file not found: {xml_path}")
        
        self.tree = ET.parse(self.xml_path)
        self.root = self.tree.getroot()
    
    def find_similar_keyword_in_rules(self, similar_keyword: str, exact_match: bool = True) -> List[Tuple[ET.Element, str, ET.Element]]:
        """Find all occurrences of similar keyword in rules.
        
        Args:
            similar_keyword: The keyword to search for (e.g., 'pattern')
            exact_match: If True, only match exact text. If False, match substrings.
            
        Returns:
            List of tuples: (rule_element, category_type, keyword_element)
            where category_type is 'keywords', 'constraints', or 'attributes'
        """
        matches = []
        
        for rule in self.root.findall('rule'):
            # Check each category
            for category in ['keywords', 'constraints', 'attributes']:
                category_elem = rule.find(category)
                if category_elem is not None:
                    for item in category_elem:
                        # Extract tag name (keyword, constraint, or attribute)
                        tag = item.tag
                        text = (item.text or "").strip()
                        
                        # Check if similar keyword matches
                        if exact_match:
                            if text.lower() == similar_keyword.lower():
                                matches.append((rule, category, item))
                        else:
                            if similar_keyword.lower() in text.lower():
                                matches.append((rule, category, item))
        
        return matches
    
    def clone_and_insert_keyword(
        self, 
        new_keyword: str, 
        similar_keyword: str,
        preserve_attributes: bool = True,
        exact_match: bool = True
    ) -> int:
        """Clone lines with similar_keyword and insert new_keyword underneath.
        
        For each occurrence of similar_keyword in the XML, this will:
        1. Find the exact element (e.g., <constraint>pattern</constraint>)
        2. Check if new_keyword already exists in that rule
        3. If not, clone it with the new keyword and insert immediately after
        
        Args:
            new_keyword: The new keyword to add (e.g., 'posix-pattern')
            similar_keyword: The similar keyword to clone from (e.g., 'pattern')
            preserve_attributes: If True, copy XML attributes from similar element
            exact_match: If True, only match exact text (default). If False, match substrings.
            
        Returns:
            Number of insertions made
        """
        matches = self.find_similar_keyword_in_rules(similar_keyword, exact_match=exact_match)
        
        if not matches:
            console.print(f"[yellow]No rules found containing '{similar_keyword}'[/yellow]")
            return 0
        
        insertions = 0
        
        for rule, category, item in matches:
            # Get the parent element (keywords/constraints/attributes container)
            category_elem = rule.find(category)
            if category_elem is None:
                continue
            
            # Check if new_keyword already exists in this specific rule's category
            keyword_already_exists = False
            for existing_item in category_elem:
                if existing_item.text and existing_item.text.strip().lower() == new_keyword.lower():
                    keyword_already_exists = True
                    break
            
            if keyword_already_exists:
                rule_id = rule.find('rule-id')
                rule_id_text = rule_id.text if rule_id is not None else "unknown"
                console.print(f"[dim]⊘ Skipped '{new_keyword}' in rule {rule_id_text} (already exists)[/dim]")
                continue
            
            # Find the index of the current item
            items = list(category_elem)
            try:
                index = items.index(item)
            except ValueError:
                continue
            
            # Create new element with same tag but new text
            new_elem = ET.Element(item.tag)
            new_elem.text = new_keyword
            
            # Preserve attributes if requested
            if preserve_attributes:
                for attr_name, attr_value in item.attrib.items():
                    new_elem.set(attr_name, attr_value)
            
            # Insert after the current item
            category_elem.insert(index + 1, new_elem)
            insertions += 1
            
            rule_id = rule.find('rule-id')
            rule_id_text = rule_id.text if rule_id is not None else "unknown"
            console.print(f"[green]✓[/green] Added '{new_keyword}' after '{similar_keyword}' in rule: {rule_id_text}")
        
        return insertions
    
    def preview_changes(self, similar_keyword: str, new_keyword: str, exact_match: bool = True) -> str:
        """Preview what changes would be made without saving.
        
        Args:
            similar_keyword: The keyword to search for
            new_keyword: The new keyword that would be added
            exact_match: If True, only match exact text (default). If False, match substrings.
            
        Returns:
            Formatted string showing what would change
        """
        matches = self.find_similar_keyword_in_rules(similar_keyword, exact_match=exact_match)
        
        if not matches:
            return f"No occurrences of '{similar_keyword}' found in rules."
        
        preview_lines = []
        preview_lines.append(f"\n[bold cyan]Preview: Adding '{new_keyword}' based on '{similar_keyword}'[/bold cyan]\n")
        preview_lines.append(f"Found {len(matches)} occurrence(s):\n")
        
        for i, (rule, category, item) in enumerate(matches, 1):
            rule_id = rule.find('rule-id')
            rule_id_text = rule_id.text if rule_id is not None else "unknown"
            
            preview_lines.append(f"{i}. Rule: [bold]{rule_id_text}[/bold]")
            preview_lines.append(f"   Category: <{category}>")
            preview_lines.append(f"   Current: <{item.tag}>{item.text}</{item.tag}>")
            
            # Show attributes if any
            if item.attrib:
                attrs = ' '.join(f'{k}="{v}"' for k, v in item.attrib.items())
                preview_lines.append(f"   Attributes: {attrs}")
            
            preview_lines.append(f"   Will add: <{item.tag}>{new_keyword}</{item.tag}>")
            preview_lines.append("")
        
        return "\n".join(preview_lines)
    
    def save(self, backup: bool = True) -> bool:
        """Save the modified XML back to file.
        
        Args:
            backup: If True, create a backup file before saving (only if no
                    backup already exists — preserves the original pre-Phase-2
                    state across re-runs).
            
        Returns:
            True if save successful, False otherwise
        """
        try:
            # Create backup only if requested AND no backup exists yet.
            # This ensures the .bak always reflects the file state *before*
            # Phase 2 first ran, not the state after a previous re-run.
            if backup:
                backup_path = self.xml_path.with_suffix('.xml.bak')
                import shutil
                if not backup_path.exists():
                    shutil.copy2(self.xml_path, backup_path)
                    console.print(f"[dim]Backup created: {backup_path}[/dim]")
                else:
                    console.print(f"[dim]Backup already exists (skipped): {backup_path}[/dim]")
            
            # Format XML nicely with proper indentation
            xml_str = ET.tostring(self.root, encoding='unicode')
            dom = minidom.parseString(xml_str)
            pretty_xml = dom.toprettyxml(indent="    ")
            
            # Remove extra blank lines
            lines = [line for line in pretty_xml.split('\n') if line.strip()]
            pretty_xml = '\n'.join(lines)
            
            # Write to file
            with open(self.xml_path, 'w', encoding='utf-8') as f:
                f.write(pretty_xml)
            
            console.print(f"[green]✓[/green] Saved changes to: {self.xml_path}")
            return True
            
        except Exception as e:
            console.print(f"[red]❌ Failed to save XML: {e}[/red]")
            return False
    
    def get_rules_summary(self) -> Dict:
        """Get summary statistics of the rules file.
        
        Returns:
            Dictionary with counts and lists of keywords/constraints/attributes
        """
        summary = {
            'total_rules': 0,
            'keywords': set(),
            'constraints': set(),
            'attributes': set(),
            'rule_ids': []
        }
        
        for rule in self.root.findall('rule'):
            summary['total_rules'] += 1
            
            rule_id = rule.find('rule-id')
            if rule_id is not None:
                summary['rule_ids'].append(rule_id.text)
            
            # Collect unique keywords/constraints/attributes
            for category in ['keywords', 'constraints', 'attributes']:
                category_elem = rule.find(category)
                if category_elem is not None:
                    for item in category_elem:
                        if item.text:
                            summary[category].add(item.text.strip())
        
        # Convert sets to sorted lists
        summary['keywords'] = sorted(summary['keywords'])
        summary['constraints'] = sorted(summary['constraints'])
        summary['attributes'] = sorted(summary['attributes'])
        
        return summary
    
    def keyword_exists(self, keyword: str, category: Optional[str] = None) -> bool:
        """Check if a keyword already exists in the rules.
        
        Args:
            keyword: The keyword to check for
            category: Optional category to limit search ('keywords', 'constraints', 'attributes')
            
        Returns:
            True if keyword exists, False otherwise
        """
        categories = [category] if category else ['keywords', 'constraints', 'attributes']
        
        for rule in self.root.findall('rule'):
            for cat in categories:
                cat_elem = rule.find(cat)
                if cat_elem is not None:
                    for item in cat_elem:
                        # Use exact match, not substring
                        if item.text and item.text.strip().lower() == keyword.lower():
                            return True
        
        return False


def _rules_to_xml_text(rules: List[ET.Element]) -> str:
    """Serialise a list of ``<rule>`` elements to a pretty-printed XML string.

    The result is a bare fragment (no ``<rules>`` wrapper) suitable for
    display in the inline editor.
    """
    parts: List[str] = []
    for rule in rules:
        raw = ET.tostring(rule, encoding="unicode")
        dom = minidom.parseString(f"<rules>{raw}</rules>")
        # toprettyxml adds an XML declaration and a <rules> wrapper — strip both
        pretty = dom.toprettyxml(indent="    ")
        lines = pretty.splitlines()
        # Drop the <?xml …?> declaration (line 0) and the <rules>/</ rules> wrapper
        inner = [l for l in lines[2:-1] if l.strip()]
        parts.append("\n".join(inner))
    return "\n\n".join(parts)


def _xml_text_to_rules(text: str) -> List[ET.Element]:
    """Parse a bare XML fragment (one or more ``<rule>`` elements) back into
    a list of :class:`xml.etree.ElementTree.Element` objects."""
    wrapper = ET.fromstring(f"<rules>{text.strip()}</rules>")
    return list(wrapper)


def update_rules_from_rag(
    xml_path: str,
    new_keyword: str,
    similar_keyword: str,
    preview_only: bool = False,
    backup: bool = True,
    exact_match: bool = True,
    interactive: bool = True,
) -> Tuple[bool, str]:
    """Update compatibility_rules.xml based on RAG-identified similar keyword.

    This is the main entry point for updating rules. It will:

    1. Find all occurrences of *similar_keyword* in the XML.
    2. Clone those lines with *new_keyword*.
    3. **Open the proposed rule(s) in an inline prompt_toolkit editor** so the
       user can freely modify any field before the change is committed.
    4. Parse the edited XML back and replace the in-memory cloned elements.
    5. Save the file (with optional backup).

    Args:
        xml_path: Path to compatibility_rules.xml
        new_keyword: The new unknown keyword to add
        similar_keyword: The similar keyword from RAG search
        preview_only: If True, only show preview without saving
        backup: If True, create backup before saving
        exact_match: If True, only match exact text (default). If False, match substrings.
        interactive: If True (default), open the inline editor before saving.
                     Set to False to skip the editor (non-interactive / batch mode).

    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        updater = XMLRuleUpdater(xml_path)

        # Check if keyword already exists
        if updater.keyword_exists(new_keyword):
            return (False, f"Keyword '{new_keyword}' already exists in rules")

        # Show rich preview
        preview = updater.preview_changes(similar_keyword, new_keyword, exact_match=exact_match)
        console.print(preview)

        if preview_only:
            return (True, "Preview only - no changes made")

        # ── Step 1: clone the matching rules in memory ───────────────────────
        insertions = updater.clone_and_insert_keyword(
            new_keyword, similar_keyword, exact_match=exact_match
        )

        if insertions == 0:
            return (False, f"No insertions made - '{similar_keyword}' not found")

        # ── Step 2: collect the freshly-cloned <rule> elements ───────────────
        # The cloned elements are the ones whose category contains new_keyword.
        cloned_rules: List[ET.Element] = []
        for rule in updater.root.findall("rule"):
            for category in ["keywords", "constraints", "attributes", "structurals"]:
                cat_elem = rule.find(category)
                if cat_elem is None:
                    continue
                for item in cat_elem:
                    if (item.text or "").strip().lower() == new_keyword.lower():
                        cloned_rules.append(rule)
                        break

        # ── Step 3: open inline editor (if interactive) ──────────────────────
        if interactive and cloned_rules:
            edit_rule_xml = _get_editor()
            if edit_rule_xml is not None:
                proposed_xml = _rules_to_xml_text(cloned_rules)
                console.print(
                    "\n[bold cyan]Opening inline editor…[/bold cyan]  "
                    "[dim](Alt+Enter to accept · Ctrl+C to cancel)[/dim]\n"
                )
                edited_xml, accepted = edit_rule_xml(
                    proposed_xml,
                    rule_id=new_keyword,
                    interactive=True,
                )

                if not accepted:
                    # User cancelled — roll back the in-memory clones
                    for rule in cloned_rules:
                        updater.root.remove(rule)
                    return (False, "User cancelled — no changes written to disk")

                # Parse the edited XML and replace the cloned elements
                try:
                    edited_rules = _xml_text_to_rules(edited_xml)
                except ET.ParseError as exc:
                    for rule in cloned_rules:
                        updater.root.remove(rule)
                    return (False, f"Edited XML is invalid: {exc}")

                # Replace each cloned rule with its edited counterpart
                rules_list = list(updater.root)
                for old_rule, new_rule in zip(cloned_rules, edited_rules):
                    idx = rules_list.index(old_rule)
                    updater.root.remove(old_rule)
                    updater.root.insert(idx, new_rule)

                console.print(
                    f"[green]✓[/green] Accepted edited rule(s) for '{new_keyword}'"
                )
            else:
                console.print(
                    "[yellow]⚠ prompt_toolkit not available — skipping inline editor[/yellow]"
                )

        # ── Step 4: save ─────────────────────────────────────────────────────
        if updater.save(backup=backup):
            return (True, f"Successfully added '{new_keyword}' to {insertions} rule(s)")
        else:
            return (False, "Failed to save changes")

    except Exception as e:
        console.print(f"[red]❌ Error: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        return (False, str(e))


if __name__ == "__main__":
    # Example usage
    import sys
    
    if len(sys.argv) < 4:
        print("Usage: python xml_rule_updater.py <xml_path> <new_keyword> <similar_keyword> [--preview]")
        sys.exit(1)
    
    xml_path = sys.argv[1]
    new_keyword = sys.argv[2]
    similar_keyword = sys.argv[3]
    preview_only = "--preview" in sys.argv
    
    success, message = update_rules_from_rag(
        xml_path=xml_path,
        new_keyword=new_keyword,
        similar_keyword=similar_keyword,
        preview_only=preview_only
    )
    
    if success:
        console.print(f"\n[bold green]✓ Success:[/bold green] {message}")
    else:
        console.print(f"\n[bold red]✗ Failed:[/bold red] {message}")
        sys.exit(1)
