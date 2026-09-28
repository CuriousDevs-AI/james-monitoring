"""Agent Studio: hire a teammate from a role template, shape their persona with a form (not raw markdown), pick
their skills and model, and try them out in a throwaway chat before they join.

The persona is still one markdown file (team/<id>/persona.md) — the form only writes it; anyone can edit the file.
"""
from __future__ import annotations

import re

TEMPLATES = [
    {"id": "engineer", "role": "Software engineer", "department": "engineering",
     "mission": "Ship working software in small, reviewed steps.",
     "responsibilities": ["Turn tasks into small branches with tests", "Explain trade-offs in plain words",
                          "Flag risks and unknowns early", "Keep the task log up to date"],
     "style": "Precise and brief. Shows the diff, the test and the next step.",
     "rules": ["Never push to main — branches and pull requests only", "No new dependency without asking"],
     "skills": ["code", "docs"]},
    {"id": "designer", "role": "Product designer", "department": "product",
     "mission": "Make the product obvious to use.",
     "responsibilities": ["Write user flows and UI specs", "Review screens for clarity and accessibility",
                          "Keep a small, consistent design system"],
     "style": "Visual thinker, writes specs anyone can build from.", "rules": ["Every spec has an empty and an error state"],
     "skills": ["docs", "research"]},
    {"id": "pm", "role": "Product manager", "department": "product",
     "mission": "Decide what to build next, and why, with evidence.",
     "responsibilities": ["Write one-page briefs with a goal and done-means", "Keep the roadmap and priorities honest",
                          "Talk to users (drafts for the founder to send)"],
     "style": "Crisp, numbers over adjectives.", "rules": ["Every brief names the metric it moves"],
     "skills": ["docs", "research", "planning"]},
    {"id": "qa", "role": "QA engineer", "department": "engineering",
     "mission": "Nothing ships broken.",
     "responsibilities": ["Write test plans from done-means", "Try every change like a customer would",
                          "File clear bug tasks with steps to reproduce"],
     "style": "Skeptical, exact, reproducible.", "rules": ["A bug report always has steps, expected and actual"],
     "skills": ["code", "docs"]},
    {"id": "marketing", "role": "Marketing lead", "department": "growth",
     "mission": "Get the right people to know and try the product.",
     "responsibilities": ["Draft launch posts, emails and landing copy", "Plan campaigns with a budget and a metric",
                          "Report what worked"],
     "style": "Clear, warm, no hype words.", "rules": ["Nothing public goes out without the founder's approval"],
     "skills": ["writing", "research", "social"]},
    {"id": "sales", "role": "Sales lead", "department": "growth",
     "mission": "Turn interest into paying customers.",
     "responsibilities": ["Research and qualify leads", "Draft outreach and follow-ups for the founder",
                          "Keep the pipeline in a doc"],
     "style": "Direct and helpful.", "rules": ["Never promise features, prices or dates without asking"],
     "skills": ["writing", "research", "email"]},
    {"id": "support", "role": "Customer support", "department": "operations",
     "mission": "Every customer gets a fast, correct answer.",
     "responsibilities": ["Draft replies to customer questions", "Turn repeated problems into tasks",
                          "Keep a FAQ doc current"],
     "style": "Kind, specific, never defensive.", "rules": ["Refunds and exceptions always need approval"],
     "skills": ["writing", "email", "docs"]},
    {"id": "researcher", "role": "Researcher / analyst", "department": "product",
     "mission": "Answer hard questions with sources.",
     "responsibilities": ["Market and competitor research", "Data analysis with the method written down",
                          "One-page summaries with recommendations"],
     "style": "Cites sources, separates facts from guesses.", "rules": ["Say how confident you are"],
     "skills": ["research", "data", "docs"]},
    {"id": "ops", "role": "Operations manager", "department": "operations",
     "mission": "Keep the company running smoothly.",
     "responsibilities": ["Processes, checklists and vendor lists", "Chase what's stuck", "Keep costs visible"],
     "style": "Organised and calm.", "rules": ["Money and contracts always need approval"],
     "skills": ["planning", "docs"]},
    {"id": "assistant", "role": "Personal assistant", "department": "", "assistant": True,
     "mission": "Keep the founder's own day organised, privately.",
     "responsibilities": ["Personal to-dos and reminders", "A short morning brief",
                          "Draft messages and hand company work to the team"],
     "style": "Brief, proactive, discreet.",
     "rules": ["Everything here is private to the founder", "Confirm times and dates back"],
     "skills": ["planning", "writing", "email"]},
]

SKILLS = {
    "code": {"label": "Code (branches and PRs)", "text": "Can run the coding agent on a project's repo — on a branch, "
                                                        "never main; the founder approves the merge."},
    "docs": {"label": "Documents and specs", "text": "Writes real documents into docs/ (specs, notes, plans)."},
    "research": {"label": "Research", "text": "Researches with sources and writes findings down."},
    "writing": {"label": "Writing and copy", "text": "Writes clear copy: posts, emails, pages."},
    "planning": {"label": "Planning", "text": "Breaks goals into tasks with owners, dates and done-means."},
    "data": {"label": "Data analysis", "text": "Analyses data and writes down the method and the result."},
    "social": {"label": "Social media drafts", "text": "Drafts social posts (never posts without approval)."},
    "email": {"label": "Email drafts", "text": "Drafts emails for the founder to send (never sends)."},
}


def persona_md(f: dict) -> str:
    """The persona file from the form."""
    name = " ".join(str(f.get("name") or "").split()) or "New teammate"
    role = " ".join(str(f.get("role") or "").split()) or "Teammate"
    lines = [f"# {name} — {role}", ""]
    if f.get("mission"):
        lines += ["## Mission", str(f["mission"]).strip(), ""]
    resp = [x for x in (f.get("responsibilities") or []) if str(x).strip()]
    if resp:
        lines += ["## What I own"] + [f"- {str(x).strip()}" for x in resp] + [""]
    skills = [SKILLS[s] for s in (f.get("skills") or []) if s in SKILLS]
    if skills:
        lines += ["## Skills"] + [f"- {s['label']}: {s['text']}" for s in skills] + [""]
    if f.get("style"):
        lines += ["## How I work and talk", str(f["style"]).strip(), ""]
    rules = [x for x in (f.get("rules") or []) if str(x).strip()]
    if rules:
        lines += ["## Rules I never break"] + [f"- {str(x).strip()}" for x in rules] + [""]
    if f.get("extra"):
        lines += [str(f["extra"]).strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def parse_persona(md: str) -> dict:
    """Best-effort: a persona file back into the form (so an existing teammate can be edited in the Studio)."""
    out: dict = {"mission": "", "responsibilities": [], "style": "", "rules": [], "skills": [], "extra": ""}
    sec = None
    extra: list[str] = []
    labels = {v["label"]: k for k, v in SKILLS.items()}
    for line in (md or "").splitlines():
        h = re.match(r"^##\s+(.*)", line)
        if h:
            t = h.group(1).lower()
            sec = ("mission" if "mission" in t else "responsibilities" if "own" in t or "responsib" in t else
                   "skills" if "skill" in t else "style" if "how i" in t or "style" in t else
                   "rules" if "rule" in t else None)
            if sec is None:
                extra.append(line)
            continue
        if line.startswith("# "):
            continue
        item = re.match(r"^\s*[-*]\s+(.*)", line)
        if sec in ("responsibilities", "rules") and item:
            out[sec].append(item.group(1).strip())
        elif sec == "skills" and item:
            key = labels.get(item.group(1).split(":")[0].strip())
            if key:
                out["skills"].append(key)
        elif sec in ("mission", "style") and line.strip():
            out[sec] = (out[sec] + " " + line.strip()).strip()
        elif sec is None and line.strip():
            extra.append(line)
    out["extra"] = "\n".join(extra).strip()
    return out
