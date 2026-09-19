#!/usr/bin/env python3
"""Combine a tree of Markdown units into a single, organized EPUB.

Reads the tree produced by ``download_course.py`` (optionally cleaned by
``strip_for_audio.py``) and writes an EPUB 3 file whose table of contents
mirrors the course: learning path -> module -> unit.

Each learning path and module also gets its own short landing page in the
reading order, so a text-to-speech app such as Speechify announces where it
is instead of running the units together.

Ordering comes from ``manifest.json`` when it is present; otherwise the
numeric filename prefixes are used.

Examples:
    python build_epub.py content/az-104-audio
    python build_epub.py content/az-104-audio -o az-104.epub --author "Microsoft Learn"
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


try:
    import markdown as markdown_lib
    from ebooklib import epub
except ImportError as exc:  # pragma: no cover
    sys.exit("Missing dependency: %s. Run: pip install -r requirements.txt" % exc)


def long_path(path: Path) -> Path:
    """Windows refuses paths over 260 characters without this prefix."""
    if os.name != "nt":
        return path
    full = os.path.abspath(str(path))
    if full.startswith("\\\\?\\"):
        return Path(full)
    if full.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + full[2:])
    return Path("\\\\?\\" + full)


NUM_PREFIX_RE = re.compile(r"^\d+[-_.]\s*")
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

CSS = """
body { font-family: Georgia, serif; line-height: 1.5; margin: 1em; }
h1 { font-size: 1.5em; margin: 0 0 .6em; }
h2 { font-size: 1.2em; margin: 1.2em 0 .4em; }
h3 { font-size: 1.05em; margin: 1em 0 .3em; }
p, li { margin: 0 0 .6em; }
blockquote { margin: .8em 0; padding-left: .8em; border-left: 3px solid #999; }
.part { text-align: center; margin-top: 25%; }
.part .kind { font-size: .8em; letter-spacing: .12em;
              text-transform: uppercase; color: #666; }
""".strip()


# --------------------------------------------------------------------------
# reading the Markdown tree
# --------------------------------------------------------------------------


def slugify(text: str, maxlen: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return (text[:maxlen].rstrip("-") or "item")


def prettify_dirname(name: str) -> str:
    """``03-configure-user-accounts`` -> ``Configure user accounts``."""
    name = NUM_PREFIX_RE.sub("", name).replace("-", " ").replace("_", " ").strip()
    return name[:1].upper() + name[1:] if name else name


def parse_front_matter(text: str) -> "tuple[dict, str]":
    """Extract the simple ``key: "value"`` front matter written by script 1."""
    meta: dict = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    block = text[3:end]
    line_end = text.find("\n", end + 1)
    body = text[line_end + 1:] if line_end != -1 else ""
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] == '"' and value[-1:] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        meta[key.strip()] = value
    return meta, body


@dataclass
class UnitDoc:
    title: str
    body_md: str


@dataclass
class ModuleDoc:
    title: str
    units: "list[UnitDoc]" = field(default_factory=list)


@dataclass
class PathDoc:
    title: str
    modules: "list[ModuleDoc]" = field(default_factory=list)


@dataclass
class Book:
    title: str
    paths: "list[PathDoc]" = field(default_factory=list)


def read_unit(path: Path, fallback_title: str = "") -> UnitDoc:
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    title = meta.get("title") or fallback_title or prettify_dirname(path.stem)
    # The body repeats the title as an H1; the chapter template adds its own.
    body = re.sub(r"^\s*#\s+.*\n", "", body, count=1)
    return UnitDoc(title=title, body_md=body.strip())


def build_from_manifest(root: Path, manifest: dict) -> Book:
    book = Book(title=manifest.get("course", {}).get("title") or prettify_dirname(root.name))
    for lp in manifest.get("learning_paths", []):
        path_doc = PathDoc(title=lp.get("title") or "Learning path")
        for mod in lp.get("modules", []):
            mod_doc = ModuleDoc(title=mod.get("title") or "Module")
            for unit in mod.get("units", []):
                rel = unit.get("path")
                if not rel:
                    continue
                src = root / rel
                if not src.exists():
                    print("  ! missing %s (listed in manifest)" % rel, file=sys.stderr)
                    continue
                mod_doc.units.append(read_unit(src, unit.get("title", "")))
            if mod_doc.units:
                path_doc.modules.append(mod_doc)
        if path_doc.modules:
            book.paths.append(path_doc)
    return book


def build_from_tree(root: Path) -> Book:
    """Infer the hierarchy from directory depth when there is no manifest."""
    book = Book(title=prettify_dirname(root.name))
    files = sorted(p for p in root.rglob("*.md") if p.is_file())
    groups: "dict[tuple, list[Path]]" = {}
    for path in files:
        groups.setdefault(path.parent.relative_to(root).parts, []).append(path)

    default_path = PathDoc(title=book.title)
    for parts in sorted(groups):
        members = sorted(groups[parts])
        units = [read_unit(p) for p in members]
        first_meta, _ = parse_front_matter(members[0].read_text(encoding="utf-8"))

        if len(parts) >= 2:  # <learning path>/<module>/unit.md
            lp_title = first_meta.get("learning_path") or prettify_dirname(parts[-2])
            mod_title = first_meta.get("module") or prettify_dirname(parts[-1])
            if not book.paths or book.paths[-1].title != lp_title:
                book.paths.append(PathDoc(title=lp_title))
            book.paths[-1].modules.append(ModuleDoc(title=mod_title, units=units))
        elif len(parts) == 1:  # <module>/unit.md
            mod_title = first_meta.get("module") or prettify_dirname(parts[-1])
            default_path.modules.append(ModuleDoc(title=mod_title, units=units))
        else:  # unit.md at the root
            mod_title = first_meta.get("module") or book.title
            default_path.modules.append(ModuleDoc(title=mod_title, units=units))

    if default_path.modules:
        if len(default_path.modules) == 1 and not book.paths:
            default_path.title = default_path.modules[0].title
        book.paths.insert(0, default_path)
    return book


# --------------------------------------------------------------------------
# writing the EPUB
# --------------------------------------------------------------------------


def md_to_html(text: str, drop_images: bool) -> str:
    html = markdown_lib.markdown(
        text, extensions=["extra", "sane_lists"], output_format="xhtml")
    if drop_images:
        html = IMG_TAG_RE.sub("", html)
    return html


def make_chapter(book: epub.EpubBook, uid: str, file_name: str, title: str,
                 html: str, css: epub.EpubItem) -> epub.EpubHtml:
    item = epub.EpubHtml(uid=uid, file_name=file_name, title=title, lang=book.language)
    item.content = "<h1>%s</h1>\n%s" % (html_lib.escape(title), html)
    item.add_item(css)
    book.add_item(item)
    return item


def part_page(kind: str) -> str:
    """Body of a learning-path or module landing page (make_chapter adds the H1)."""
    return '<div class="part"><p class="kind">%s</p></div>' % html_lib.escape(kind)


def build_epub(doc: Book, out_path: Path, author: str, language: str,
               identifier: str, drop_images: bool) -> "tuple[int, int]":
    book = epub.EpubBook()
    book.set_identifier(identifier)
    book.set_title(doc.title)
    book.set_language(language)
    book.add_author(author)

    css = epub.EpubItem(uid="style", file_name="style/main.css",
                        media_type="text/css", content=CSS)
    book.add_item(css)

    spine: "list" = ["nav"]
    toc: "list" = []
    unit_count = 0

    for pi, path_doc in enumerate(doc.paths, start=1):
        single_path = len(doc.paths) == 1 and path_doc.title == doc.title
        path_children: "list" = []

        if not single_path:
            item = make_chapter(
                book, "p%02d" % pi, "text/p%02d-%s.xhtml" % (pi, slugify(path_doc.title)),
                path_doc.title, part_page("Learning path"), css)
            spine.append(item)
            path_section = epub.Section(path_doc.title, href=item.file_name)
        else:
            path_section = None

        for mi, mod in enumerate(path_doc.modules, start=1):
            item = make_chapter(
                book, "p%02dm%02d" % (pi, mi),
                "text/p%02dm%02d-%s.xhtml" % (pi, mi, slugify(mod.title)),
                mod.title, part_page("Module"), css)
            spine.append(item)
            mod_children = []

            for ui, unit in enumerate(mod.units, start=1):
                chapter = make_chapter(
                    book, "p%02dm%02du%02d" % (pi, mi, ui),
                    "text/p%02dm%02du%02d-%s.xhtml" % (pi, mi, ui, slugify(unit.title)),
                    unit.title, md_to_html(unit.body_md, drop_images), css)
                spine.append(chapter)
                mod_children.append(chapter)
                unit_count += 1

            path_children.append((epub.Section(mod.title, href=item.file_name),
                                  mod_children))

        if path_section is not None:
            toc.append((path_section, path_children))
        else:
            toc.extend(path_children)

    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    long_path(out_path).parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(long_path(out_path)), book)
    return len(doc.paths), unit_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Combine a Markdown course tree into one organized EPUB.")
    parser.add_argument("src", type=Path, help="Root of the Markdown tree")
    parser.add_argument("-o", "--out", type=Path,
                        help="Output .epub path (default: <src>.epub)")
    parser.add_argument("--title", help="Override the book title")
    parser.add_argument("--author", default="Microsoft Learn", help="Book author")
    parser.add_argument("--language", default="en", help="Book language (default: en)")
    parser.add_argument("--keep-images", action="store_true",
                        help="Keep <img> tags (they point at remote URLs and will "
                             "not render offline)")
    args = parser.parse_args()

    src = args.src.resolve()
    if not src.is_dir():
        print("Not a directory: %s" % src, file=sys.stderr)
        return 2

    scan_root = long_path(src)
    manifest_path = scan_root / "manifest.json"
    if manifest_path.exists():
        doc = build_from_manifest(scan_root,
                                  json.loads(manifest_path.read_text(encoding="utf-8")))
        source = "manifest.json"
    else:
        doc = build_from_tree(scan_root)
        source = "directory layout"

    if args.title:
        doc.title = args.title
    if not doc.paths:
        print("No Markdown units found under %s" % src, file=sys.stderr)
        return 2

    out = (args.out or src.parent / (src.name + ".epub")).resolve()
    identifier = "urn:uuid:" + hashlib.sha1(
        ("microsoft-learn-to-audio:" + doc.title).encode("utf-8")).hexdigest()

    paths, units = build_epub(doc, out, args.author, args.language, identifier,
                              drop_images=not args.keep_images)
    modules = sum(len(p.modules) for p in doc.paths)
    print("Built %s" % out)
    print("  ordered by: %s" % source)
    print("  %s - %d learning path(s), %d module(s), %d unit(s)"
          % (doc.title, paths, modules, units))
    return 0


if __name__ == "__main__":
    sys.exit(main())
