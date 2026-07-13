from __future__ import annotations

from skills import resolve_data_path
from skills.tool_decorator import tool


@tool(
    description="Read a local UTF-8 txt or md file from the data directory.",
    parameters={
        "path": "Path relative to the configured data root.",
        "max_chars": "Maximum number of characters to return.",
    },
    returns={
        "content": {"type": "string", "description": "File content."},
        "num_chars": {"type": "integer", "description": "Returned character count."},
        "source": {"type": "string", "description": "Normalized path relative to data root."},
        "truncated": {"type": "boolean", "description": "Whether content was truncated."},
    },
)
def file_reader(path: str, max_chars: int = 2000, *, data_root: str | None = None) -> dict:
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    source, root = resolve_data_path(path, data_root)
    if source.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("file_reader only supports .txt and .md files")
    if not source.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    original = source.read_text(encoding="utf-8")
    content = original[:max_chars]
    return {
        "content": content,
        "num_chars": len(content),
        "source": source.relative_to(root).as_posix(),
        "truncated": len(original) > len(content),
    }
