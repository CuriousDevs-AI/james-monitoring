"""Markdown documents with YAML front matter and `## Section` bodies."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

_FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


@dataclass
class MDoc:
    meta: dict = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)   # ordered: heading -> body
    preamble: str = ""

    @classmethod
    def parse(cls, text: str) -> "MDoc":
        m = _FM.match(text)
        if not m:
            return cls(meta={}, sections={}, preamble=text)
        meta = yaml.safe_load(m.group(1)) or {}
        body = m.group(2)
        sections: dict[str, str] = {}
        preamble_lines: list[str] = []
        current: str | None = None
        buf: list[str] = []
        for line in body.splitlines():
            if line.startswith("## "):
                if current is not None:
                    sections[current] = "\n".join(buf).strip("\n")
                current, buf = line[3:].strip(), []
            elif current is None:
                preamble_lines.append(line)
            else:
                buf.append(line)
        if current is not None:
            sections[current] = "\n".join(buf).strip("\n")
        return cls(meta=meta, sections=sections, preamble="\n".join(preamble_lines).strip("\n"))

    def render(self) -> str:
        fm = yaml.safe_dump(self.meta, sort_keys=False, allow_unicode=True, default_flow_style=None).strip()
        out = [f"---\n{fm}\n---"]
        if self.preamble:
            out.append(self.preamble)
        for h, body in self.sections.items():
            out.append(f"## {h}\n{body}".rstrip())
        return "\n\n".join(out) + "\n"

    def add_line(self, section: str, line: str, newest_first: bool = False) -> None:
        cur = self.sections.get(section, "").strip("\n")
        lines = [l for l in cur.splitlines() if l.strip()]
        if newest_first:
            lines.insert(0, line)
        else:
            lines.append(line)
        self.sections[section] = "\n".join(lines)
