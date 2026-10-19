"""
Skills MCP server — exposes team domain knowledge as markdown files.

Tools:
- list_skills(): returns name + short summary of each skill
- read_skill(name): returns full markdown content of a specific skill

The server reads skill_index.json (auto-generated) on startup. If a markdown
file is added, edited, or removed, the server picks it up automatically — no
restart needed for content changes.
"""
import os
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP("skills")

SKILLS_DIR = Path(__file__).parent / "skills"


def _read_first_meaningful_line(content: str, max_len: int = 200) -> str:
    """Find the first non-empty, non-header line as a summary."""
    in_comment=False
    for line in content.split("\n"):
        line = line.strip()
        if in_comment:
            if "--->" in lime:
                in_comment=False
            continue
        if line.startswith("<!--"):
            if "-->" not in line:
                in_comment=True
        if line and not line.startswith("#") and not line.startswith("---"):
            return line[:max_len]
    return ""


def _read_title(content: str) -> str:
    """Find the first H1 line as a title, fall back to filename."""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line.lstrip("# ").strip()
    return ""


@mcp.tool()
def list_skills() -> dict:
    """
    List all available skill documents.
    
    Each skill is a markdown file containing team standards, naming
    conventions, design patterns, or troubleshooting procedures for
    a specific area of ACI configuration.
    
    ALWAYS call this at the start of any task to discover relevant
    skills. Then use read_skill() to load the full content of any
    skill that looks relevant.
    
    Returns:
        Dict with "skills" key: list of {name, title, summary}.
    """
    skills = []
    if not SKILLS_DIR.exists():
        return {"skills": [], "error": f"Skills directory not found: {SKILLS_DIR}"}
    
    for md_file in sorted(SKILLS_DIR.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            continue
        skills.append({
            "name": md_file.stem,
            "title": _read_title(content) or md_file.stem,
            "summary": _read_first_meaningful_line(content),
        })
    return {"skills": skills, "count": len(skills)}


@mcp.tool()
def read_skill(skill_name: str) -> dict:
    """
    Read the full content of a specific skill document.
    
    Call this when you need detailed guidance for a task. The skill
    content contains rules, examples, and procedures that you should
    follow when constructing configurations or troubleshooting.
    
    Args:
        skill_name: Name of the skill (without .md extension).
                   Use list_skills() to see available names.
    
    Returns:
        Dict with name and full markdown content. If skill not found,
        returns error and a list of available skills.
    """
    name = skill_name.replace(".md", "").strip()
    md_path = SKILLS_DIR / f"{name}.md"
    
    # Guard
    if md_path.resolve().parent != SKILLS_DIR.resolve():
        return {"error": f"invalid skill name: '{skill_name}"}
    
    if not md_path.exists():
        available = [p.stem for p in SKILLS_DIR.glob("*.md")]
        return {
            "error": f"Skill '{name}' not found",
            "available_skills": available,
        }
    
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"Could not read skill: {e}"}
    
    return {
        "name": name,
        "title": _read_title(content) or name,
        "content": content,
        "size_bytes": len(content.encode("utf-8")),
    }


if __name__ == "__main__":
    mcp.run()
