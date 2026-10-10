"""Pure text formatting shared by local and remote command presentation."""

from __future__ import annotations

import json


def format_text_output(data) -> str:
    """Format the compact CLI text representation, including its final newline."""
    if isinstance(data, dict):
        lines = [
            f"{key}: {json.dumps(value, indent=2, ensure_ascii=False, default=str)}"
            if isinstance(value, (dict, list)) else f"{key}: {value}"
            for key, value in data.items()
        ]
    elif isinstance(data, list):
        lines = [json.dumps(item, ensure_ascii=False, default=str)
                 if isinstance(item, dict) else str(item) for item in data]
    else:
        lines = [str(data)]
    return "\n".join(lines) + ("\n" if lines else "")
