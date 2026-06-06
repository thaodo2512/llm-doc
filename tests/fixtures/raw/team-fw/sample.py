"""Sample source file exercised by the tree-sitter parser (M4)."""


def render_widget(widget):
    """Render a widget to HTML."""
    return f"<div>{widget.name}</div>"


class Widget:
    """A UI widget with a name and a retry_backoff."""

    def __init__(self, name, retry_backoff=0.5):
        self.name = name
        self.retry_backoff = retry_backoff
