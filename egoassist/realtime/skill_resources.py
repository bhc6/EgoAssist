"""Bounded, explicitly declared instruction resources for a Skill package."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


@dataclass(frozen=True)
class SkillResource:
    path: str
    content: str
    sha256: str

    def context(self) -> str:
        return (f"<skill_reference path={quoteattr(self.path)} sha256={quoteattr(self.sha256)}>\n"
                f"{escape(self.content)}\n</skill_reference>")


def load_instruction_resources(root: Path, manifest: dict) -> tuple[SkillResource, ...]:
    paths = manifest.get("instruction_resources", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("instruction_resources must be a list of relative Markdown paths")
    resources = []
    total = 0
    for relative in dict.fromkeys(paths):
        target = (root / relative).resolve()
        if Path(relative).is_absolute() or not target.is_relative_to(root.resolve()):
            raise ValueError(f"instruction resource escapes skill root: {relative}")
        if target.suffix.lower() != ".md" or not target.is_file():
            raise ValueError(f"instruction resource must be an existing Markdown file: {relative}")
        # Small policies are loaded at registration, injected only when active.
        # Do not recursively enumerate assets or silently truncate instructions.
        total += target.stat().st_size
        if total > 32768:
            raise ValueError("instruction resources exceed the 32 KiB skill budget")
        data = target.read_bytes()
        resources.append(SkillResource(relative, data.decode("utf-8"), hashlib.sha256(data).hexdigest()))
    return tuple(resources)
