#!/usr/bin/env python3
"""Render docs/ARCHITECTURE.md + docs/ROADMAP.md into one self-contained HTML
page, styled to fill a claude.ai Artifact (see template.html).

Python 3 stdlib only. No pip installs, no PyYAML, no markdown library.

Usage:
    python3 docs/site/build.py --arch docs/ARCHITECTURE.md \
        --roadmap docs/ROADMAP.md --out docs/site/index.html \
        [--product-name NAME] [--repo-url URL]

The build is also the format lint: it exits non-zero with a clear message
if a required frontmatter key or a required H2 section is missing, per the
format contract at DOCS-FORMAT.md.
"""

import argparse
import html
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")

ARCH_REQUIRED_KEYS = [
    "title", "product", "version", "status", "updated",
    "verified_against", "owners", "supersedes",
]
ARCH_REQUIRED_H2 = [
    "Purpose", "System diagram", "Components", "Interfaces",
    "Data & state", "Cross-product edges", "Invariants",
    "Known gaps", "Changelog",
]
ROADMAP_REQUIRED_KEYS = ["title", "product", "version", "status", "updated", "horizon"]
ROADMAP_REQUIRED_H2 = ["Now", "Next", "Later", "Not doing", "Decision log"]


class FormatError(Exception):
    pass


# --------------------------------------------------------------------------
# Frontmatter parsing (simple YAML-ish: key: value, and lists either as
# `key: [a, b]` inline or as `key:` followed by `  - item` lines)
# --------------------------------------------------------------------------

def split_frontmatter(text):
    """Return (frontmatter_text, body_text). Frontmatter is delimited by
    a leading '---' line and a closing '---' line."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FormatError("missing frontmatter: file must start with a '---' line")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise FormatError("missing closing '---' for frontmatter")
    fm_text = "\n".join(lines[1:end])
    body_text = "\n".join(lines[end + 1:])
    return fm_text, body_text


def parse_frontmatter(fm_text):
    data = {}
    lines = fm_text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest == "" or rest == "|":
            # Possibly a block list on following indented "- " lines
            items = []
            j = i + 1
            while j < n:
                nxt = lines[j]
                if re.match(r"^\s*-\s*", nxt) and nxt.strip() != "-":
                    items.append(re.sub(r"^\s*-\s*", "", nxt).strip().strip('"\''))
                    j += 1
                elif nxt.strip() == "":
                    j += 1
                    break
                else:
                    break
            if items:
                data[key] = items
                i = j
                continue
            else:
                data[key] = ""
                i += 1
                continue
        elif rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            if inner == "":
                data[key] = []
            else:
                data[key] = [p.strip().strip('"\'') for p in inner.split(",")]
            i += 1
            continue
        else:
            data[key] = rest.strip('"\'')
            i += 1
            continue
    return data


def require_keys(data, required, doc_name):
    # Presence is what matters, not truthiness: `supersedes: []` is a valid,
    # deliberate "nothing superseded" and must not be flagged as missing.
    missing = [k for k in required if k not in data or data[k] is None]
    if missing:
        raise FormatError(
            "%s frontmatter missing required key(s): %s"
            % (doc_name, ", ".join(missing))
        )


# --------------------------------------------------------------------------
# Markdown -> HTML (stdlib only)
# --------------------------------------------------------------------------

def esc(text):
    return html.escape(text, quote=False)


INLINE_CODE_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")


def render_inline(text):
    """Render inline markdown: code spans, links, bold, italic. Escapes
    plain text nodes; code/link contents are escaped appropriately."""
    # Protect inline code spans first so markup inside isn't reinterpreted.
    placeholders = []

    def stash_code(m):
        placeholders.append("<code>%s</code>" % esc(m.group(1)))
        return "\x00%d\x00" % (len(placeholders) - 1)

    tmp = INLINE_CODE_RE.sub(stash_code, text)

    def stash_link(m):
        label = render_bold_italic(esc_but_keep_placeholders(m.group(1)))
        url = html.escape(m.group(2), quote=True)
        placeholders.append('<a href="%s" target="_blank" rel="noopener">%s</a>' % (url, label))
        return "\x00%d\x00" % (len(placeholders) - 1)

    def esc_but_keep_placeholders(s):
        return esc(s)

    tmp = LINK_RE.sub(stash_link, tmp)

    # Now escape remaining plain text, but placeholders (\x00N\x00) must survive.
    parts = re.split(r"(\x00\d+\x00)", tmp)
    out = []
    for part in parts:
        if re.match(r"^\x00\d+\x00$", part):
            idx = int(part.strip("\x00"))
            out.append(placeholders[idx])
        else:
            out.append(render_bold_italic(esc(part)))
    return "".join(out)


def render_bold_italic(escaped_text):
    t = BOLD_RE.sub(lambda m: "<strong>%s</strong>" % (m.group(1) or m.group(2)), escaped_text)
    t = ITALIC_RE.sub(lambda m: "<em>%s</em>" % (m.group(1) or m.group(2)), t)
    return t


def parse_table(lines, start):
    """lines[start] is the header row, lines[start+1] the alignment row.
    Returns (html, next_index)."""
    header_cells = [c.strip() for c in lines[start].strip().strip("|").split("|")]
    align_cells = [c.strip() for c in lines[start + 1].strip().strip("|").split("|")]
    aligns = []
    for c in align_cells:
        if re.match(r"^:-+:$", c):
            aligns.append("center")
        elif re.match(r"^-+:$", c):
            aligns.append("right")
        elif re.match(r"^:-+-*$", c) or re.match(r"^:-+$", c):
            aligns.append("left")
        else:
            aligns.append(None)
    i = start + 2
    rows = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        rows.append(cells)
        i += 1

    def align_class(idx):
        if idx < len(aligns) and aligns[idx] in ("center", "right"):
            return ' class="align-%s"' % aligns[idx]
        return ""

    out = ['<div class="scroll-wrap"><table><thead><tr>']
    for idx, cell in enumerate(header_cells):
        out.append("<th%s>%s</th>" % (align_class(idx), render_inline(cell)))
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for idx, cell in enumerate(row):
            out.append("<td%s>%s</td>" % (align_class(idx), render_inline(cell)))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out), i


TASK_RE = re.compile(r"^(\s*)-\s+\[( |x|X)\]\s+(.*)$")
UL_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
OL_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")


def list_indent(line):
    return len(line) - len(line.lstrip(" "))


def parse_list_block(lines, start):
    """Parses a contiguous (possibly nested) list block starting at
    lines[start]. Returns (html, next_index)."""
    base_indent = list_indent(lines[start])
    is_task = bool(TASK_RE.match(lines[start]))
    is_ordered = bool(OL_RE.match(lines[start])) and not is_task
    tag = "ol" if is_ordered else "ul"
    cls = ' class="task-list"' if is_task else ""
    items = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            # blank line: peek ahead, if next non-blank is still part of
            # this list at same/deeper indent, continue; else stop.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and list_indent(lines[j]) >= base_indent and (
                UL_RE.match(lines[j]) or OL_RE.match(lines[j]) or TASK_RE.match(lines[j])
            ):
                i = j
                continue
            else:
                break
        indent = list_indent(line)
        if indent < base_indent:
            break
        tm = TASK_RE.match(line)
        um = UL_RE.match(line) if not tm else None
        om = OL_RE.match(line) if not tm and not um else None
        if indent == base_indent and (tm or um or om):
            if tm:
                checked = tm.group(2).lower() == "x"
                content = tm.group(3)
                checkbox = '<input type="checkbox" disabled%s>' % (" checked" if checked else "")
                item_html = checkbox + "<span>%s</span>" % render_inline(content)
            else:
                content = (um or om).group(2)
                item_html = render_inline(content)
            # look ahead for a nested list
            i += 1
            nested = ""
            while i < len(lines):
                if not lines[i].strip():
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and list_indent(lines[j]) > base_indent and (
                        UL_RE.match(lines[j]) or OL_RE.match(lines[j]) or TASK_RE.match(lines[j])
                    ):
                        i = j
                        continue
                    else:
                        break
                if list_indent(lines[i]) > base_indent and (
                    UL_RE.match(lines[i]) or OL_RE.match(lines[i]) or TASK_RE.match(lines[i])
                ):
                    nested, i = parse_list_block(lines, i)
                    break
                else:
                    break
            items.append("<li>%s%s</li>" % (item_html, nested))
        else:
            break
    return "<%s%s>%s</%s>" % (tag, cls, "".join(items), tag), i


def render_markdown(md_text):
    lines = md_text.split("\n")
    out = []
    i = 0
    n = len(lines)
    section_open = False
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        fence_m = re.match(r"^```(\S*)\s*$", stripped)
        if fence_m:
            lang = fence_m.group(1)
            body_lines = []
            i += 1
            while i < n and lines[i].strip() != "```":
                body_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            code_text = "\n".join(body_lines)
            if lang == "mermaid":
                out.append('<pre class="mermaid">%s</pre>' % esc(code_text))
            else:
                tag = '<span class="lang-tag">%s</span>' % esc(lang) if lang else ""
                out.append(
                    '<div class="codeblock-wrap">%s<pre><code>%s</code></pre></div>'
                    % (tag, esc(code_text))
                )
            continue

        # Headings
        h_m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if h_m:
            level = len(h_m.group(1))
            text = h_m.group(2).strip()
            slug = slugify(text)
            if level == 2:
                if section_open:
                    out.append("</div>")
                out.append('<div class="doc-section" id="%s">' % slug)
                section_open = True
                out.append("<h2>%s</h2>" % render_inline(text))
            else:
                tag = "h%d" % min(level, 4) if level != 2 else "h2"
                out.append('<%s id="%s">%s</%s>' % (tag, slug, render_inline(text), tag))
            i += 1
            continue

        # Table
        if "|" in stripped and i + 1 < n and re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", lines[i + 1]):
            table_html, i = parse_table(lines, i)
            out.append(table_html)
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner_html = render_markdown("\n".join(quote_lines))
            out.append("<blockquote>%s</blockquote>" % inner_html)
            continue

        # List (task, unordered, ordered)
        if TASK_RE.match(line) or UL_RE.match(line) or OL_RE.match(line):
            list_html, i = parse_list_block(lines, i)
            out.append(list_html)
            continue

        # Paragraph: gather contiguous non-blank, non-special lines
        para_lines = [stripped]
        i += 1
        while i < n and lines[i].strip() and not (
            re.match(r"^#{1,6}\s+", lines[i].strip())
            or re.match(r"^```", lines[i].strip())
            or TASK_RE.match(lines[i]) or UL_RE.match(lines[i]) or OL_RE.match(lines[i])
            or lines[i].strip().startswith(">")
        ):
            para_lines.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % render_inline(" ".join(para_lines)))

    if section_open:
        out.append("</div>")
    return "\n".join(out)


def slugify(text):
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s\-&]", "", s)
    s = re.sub(r"[\s&]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "section"


def extract_h2_titles(md_body):
    titles = []
    for line in md_body.split("\n"):
        m = re.match(r"^##\s+(.*)$", line.strip())
        if m:
            titles.append(m.group(1).strip())
    return titles


def check_required_h2(titles, required, doc_name):
    missing = []
    for req in required:
        found = any(t == req or t.startswith(req) for t in titles)
        if not found:
            missing.append(req)
    if missing:
        raise FormatError(
            "%s missing required section(s) (## H2): %s"
            % (doc_name, ", ".join(missing))
        )


def build_nav_items(titles, active_prefix_id_set=None):
    items = []
    for t in titles:
        slug = slugify(t)
        items.append('        <li><a href="#%s">%s</a></li>' % (slug, esc(t)))
    return "\n".join(items)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, help="path to docs/ARCHITECTURE.md")
    parser.add_argument("--roadmap", required=True, help="path to docs/ROADMAP.md")
    parser.add_argument("--out", required=True, help="output HTML path")
    parser.add_argument("--product-name", default=None)
    parser.add_argument("--repo-url", default=None)
    args = parser.parse_args()

    try:
        arch_html_doc = build_doc(args.arch, "docs/ARCHITECTURE.md", ARCH_REQUIRED_KEYS, ARCH_REQUIRED_H2)
        roadmap_html_doc = build_doc(args.roadmap, "docs/ROADMAP.md", ROADMAP_REQUIRED_KEYS, ROADMAP_REQUIRED_H2)
    except FormatError as e:
        sys.stderr.write("build.py: format lint failed: %s\n" % e)
        return 1
    except (IOError, OSError) as e:
        sys.stderr.write("build.py: %s\n" % e)
        return 1

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    arch_fm = arch_html_doc["frontmatter"]
    roadmap_fm = roadmap_html_doc["frontmatter"]

    product_name = args.product_name or arch_fm.get("product") or roadmap_fm.get("product") or "Product"
    sha = arch_fm.get("verified_against", "unknown")
    repo_url = args.repo_url

    if repo_url:
        sha_badge = '<span class="badge"><a href="%s/commit/%s" target="_blank" rel="noopener">%s</a></span>' % (
            html.escape(repo_url.rstrip("/"), quote=True), esc(sha), esc(sha)
        )
    else:
        sha_badge = '<span class="badge">%s</span>' % esc(sha)

    rebuild_cmd = (
        "python3 docs/site/build.py --arch docs/ARCHITECTURE.md "
        "--roadmap docs/ROADMAP.md --out docs/site/index.html"
    )
    if args.product_name:
        rebuild_cmd += ' --product-name "%s"' % args.product_name
    if args.repo_url:
        rebuild_cmd += ' --repo-url "%s"' % args.repo_url

    replacements = {
        "__PRODUCT_NAME__": esc(product_name),
        "__VERSION__": esc(arch_fm.get("version", "")),
        "__STATUS__": esc(arch_fm.get("status", "")),
        "__UPDATED__": esc(arch_fm.get("updated", "")),
        "__SHA_BADGE__": sha_badge,
        "__SHA__": esc(sha),
        "__REBUILD_CMD__": esc(rebuild_cmd),
        "__ARCH_NAV_ITEMS__": build_nav_items(arch_html_doc["h2_titles"]),
        "__ROADMAP_NAV_ITEMS__": build_nav_items(roadmap_html_doc["h2_titles"]),
        "__ARCH_TITLE__": esc(arch_fm.get("title", "Architecture")),
        "__ARCH_VERSION__": esc(arch_fm.get("version", "")),
        "__ARCH_STATUS__": esc(arch_fm.get("status", "")),
        "__ARCH_UPDATED__": esc(arch_fm.get("updated", "")),
        "__ARCH_BODY__": arch_html_doc["body_html"],
        "__ROADMAP_TITLE__": esc(roadmap_fm.get("title", "Roadmap")),
        "__ROADMAP_VERSION__": esc(roadmap_fm.get("version", "")),
        "__ROADMAP_STATUS__": esc(roadmap_fm.get("status", "")),
        "__ROADMAP_HORIZON__": esc(roadmap_fm.get("horizon", "")),
        "__ROADMAP_BODY__": roadmap_html_doc["body_html"],
    }

    out_html = template
    for key, val in replacements.items():
        out_html = out_html.replace(key, val)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_html)

    sys.stderr.write("build.py: wrote %s\n" % args.out)
    return 0


def build_doc(path, doc_label, required_keys, required_h2):
    if not os.path.isfile(path):
        raise FormatError("%s not found at %s" % (doc_label, path))
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    fm_text, body_text = split_frontmatter(text)
    fm = parse_frontmatter(fm_text)
    require_keys(fm, required_keys, doc_label)
    h2_titles = extract_h2_titles(body_text)
    check_required_h2(h2_titles, required_h2, doc_label)
    body_html = render_markdown(body_text)
    return {"frontmatter": fm, "h2_titles": h2_titles, "body_html": body_html}


if __name__ == "__main__":
    sys.exit(main())
