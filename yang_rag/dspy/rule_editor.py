"""
rule_editor.py — Interactive inline XML rule editor
-----------------------------------------------------
Presents a proposed XML rule block in a prompt_toolkit full-screen text area
so the user can freely edit it before it is written to compatibility_rules.xml.

Usage (called from xml_rule_updater.py)::

    from yang_rag.dspy.rule_editor import edit_rule_xml

    edited_xml, accepted = edit_rule_xml(proposed_xml_str, rule_id="my-new-rule")
    if accepted:
        # parse edited_xml and insert into the tree
        ...

Key bindings inside the editor
-------------------------------
  Ctrl+S                            — Accept and close (works on all platforms)
  Alt+Enter  /  Escape then Enter   — Accept and close (Linux/macOS only;
                                      Windows Terminal intercepts Alt+Enter)
  Ctrl+C     /  Ctrl+Q              — Cancel (discard changes)
  Tab                               — Insert 4 spaces
  All standard cursor / editing keys work as expected.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Tuple

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea


# ---------------------------------------------------------------------------
# Colour / style palette
# ---------------------------------------------------------------------------

_STYLE = Style.from_dict(
    {
        # Overall background
        "background":           "bg:#1e1e2e #cdd6f4",
        # Title bar
        "title-bar":            "bg:#313244 #cba6f7 bold",
        # Status / hint bar at the bottom
        "status-bar":           "bg:#313244 #a6e3a1",
        "status-bar.key":       "bg:#313244 #f38ba8 bold",
        # The editable text area
        "editor":               "bg:#181825 #cdd6f4",
        "editor focused":       "bg:#181825 #cdd6f4",
        # Validation error banner
        "error-bar":            "bg:#f38ba8 #1e1e2e bold",
        # Frame / border
        "frame.border":         "#6c7086",
        "frame.label":          "#cba6f7 bold",
        # Line numbers (decorative — shown in the hint text)
        "line-number":          "#6c7086",
    }
)


# ---------------------------------------------------------------------------
# XML validation helper
# ---------------------------------------------------------------------------

def _validate_xml(text: str) -> Tuple[bool, str]:
    """Return (is_valid, error_message).  Wraps the snippet in a root tag so
    that a bare ``<rule>…</rule>`` fragment is accepted by the parser."""
    try:
        ET.fromstring(f"<rules>{text.strip()}</rules>")
        return True, ""
    except ET.ParseError as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def edit_rule_xml(
    proposed_xml: str,
    rule_id: str = "new-rule",
    *,
    interactive: bool = True,
) -> Tuple[str, bool]:
    """Open an inline prompt_toolkit editor pre-filled with *proposed_xml*.

    Parameters
    ----------
    proposed_xml:
        The XML text to pre-fill the editor with (one or more ``<rule>``
        elements, without the surrounding ``<rules>`` wrapper).
    rule_id:
        Human-readable label shown in the title bar.
    interactive:
        When *False* (e.g. ``--no-interactive`` mode) the function returns
        immediately with ``(proposed_xml, True)`` — no UI is shown.

    Returns
    -------
    (edited_xml, accepted)
        *edited_xml* is the (possibly modified) XML string.
        *accepted* is ``True`` if the user confirmed, ``False`` if cancelled.
    """
    if not interactive:
        return proposed_xml, True

    # ── State shared between key-binding callbacks ──────────────────────────
    result: dict = {"accepted": False, "xml": proposed_xml}
    error_message: list[str] = [""]   # mutable container so closures can write

    # ── Text area ────────────────────────────────────────────────────────────
    editor = TextArea(
        text=proposed_xml,
        multiline=True,
        scrollbar=True,
        line_numbers=True,
        style="class:editor",
        focus_on_click=True,
        wrap_lines=False,
    )

    # ── Error banner (hidden when empty) ─────────────────────────────────────
    error_control = FormattedTextControl(
        lambda: HTML(f"<error-bar>  ⚠  {error_message[0]}  </error-bar>")
        if error_message[0]
        else HTML("")
    )
    error_window = Window(
        content=error_control,
        height=Dimension(min=0, max=1),
        style="class:error-bar",
    )

    # ── Title bar ────────────────────────────────────────────────────────────
    title_control = FormattedTextControl(
        lambda: HTML(
            f"<title-bar>  ✏  Edit XML rule — <b>{rule_id}</b>"
            "  (Ctrl+S to accept · Ctrl+C to cancel)  </title-bar>"
        )
    )
    title_window = Window(
        content=title_control,
        height=1,
        style="class:title-bar",
    )

    # ── Status / hint bar ────────────────────────────────────────────────────
    hint_control = FormattedTextControl(
        lambda: HTML(
            "  <status-bar.key>Ctrl+S</status-bar.key>"
            "<status-bar> Accept  </status-bar>"
            "<status-bar.key>Ctrl+C</status-bar.key>"
            "<status-bar> Cancel  </status-bar>"
            "<status-bar.key>Tab</status-bar.key>"
            "<status-bar> Indent  </status-bar>"
            "<status-bar.key>↑↓←→</status-bar.key>"
            "<status-bar> Navigate</status-bar>"
        )
    )
    hint_window = Window(
        content=hint_control,
        height=1,
        style="class:status-bar",
    )

    # ── Layout ───────────────────────────────────────────────────────────────
    body = Frame(
        body=HSplit([editor]),
        title=f" Rule: {rule_id} ",
        style="class:frame",
    )

    root_container = HSplit(
        [
            title_window,
            body,
            error_window,
            hint_window,
        ]
    )

    layout = Layout(root_container, focused_element=editor)

    # ── Key bindings ─────────────────────────────────────────────────────────
    kb = KeyBindings()

    @kb.add("escape", "enter", eager=True)   # Alt+Enter on Linux/macOS
    @kb.add("c-s")                           # Ctrl+S on Windows (Alt+Enter intercepted by terminal)
    def _accept(event: object) -> None:
        """Validate XML and accept if valid."""
        text = editor.text
        valid, err = _validate_xml(text)
        if valid:
            error_message[0] = ""
            result["accepted"] = True
            result["xml"] = text
            event.app.exit()  # type: ignore[union-attr]
        else:
            error_message[0] = f"Invalid XML — {err}"

    @kb.add("c-c")
    @kb.add("c-q")
    def _cancel(event: object) -> None:
        """Cancel without saving."""
        result["accepted"] = False
        event.app.exit()  # type: ignore[union-attr]

    @kb.add("tab")
    def _tab(event: object) -> None:
        """Insert 4 spaces instead of a real tab."""
        editor.buffer.insert_text("    ")

    # ── Application ──────────────────────────────────────────────────────────
    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        mouse_support=True,
    )

    app.run()

    return result["xml"], result["accepted"]


# ---------------------------------------------------------------------------
# Standalone demo / smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _DEMO = """\
<rule>
    <rule-id>demo-new-rule</rule-id>
    <attributes>
        <attribute type="ulist" separator=" " set-change="narrowed">privileges</attribute>
    </attributes>
    <actions>
        <action>changed</action>
    </actions>
    <compatible>non-backward-compatible</compatible>
</rule>"""

    edited, ok = edit_rule_xml(_DEMO, rule_id="demo-new-rule")
    if ok:
        print("\n✅ Accepted XML:\n")
        print(edited)
    else:
        print("\n❌ Cancelled — no changes.")
