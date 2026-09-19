#!/usr/bin/env python3
"""Download Microsoft Learn training content as a tree of Markdown files.

Given a course, learning path or module URL, walks the Microsoft Learn
hierarchy and writes one Markdown file per unit into:

    <out>/<course>/<learning_path>/<module>/<unit>.md

Each level is named from the last segment of its Learn URL, prefixed with a
zero-padded ordinal so the reading order survives alphabetical sorting. A
``manifest.json`` is written at the root of the tree describing the
hierarchy; ``build_epub.py`` uses it.

Examples:
    python download_course.py https://learn.microsoft.com/en-us/training/courses/az-104t00/
    python download_course.py https://learn.microsoft.com/en-us/training/paths/az-104-manage-identities-governance/
    python download_course.py https://learn.microsoft.com/en-us/training/modules/configure-user-group-accounts/
    python download_course.py <course-url> --learning-path <path-url-or-uid>
    python download_course.py <course-url> --module <module-url-or-uid> --list
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    from markdownify import MarkdownConverter
except ImportError as exc:  # pragma: no cover
    sys.exit("Missing dependency: %s. Run: pip install -r requirements.txt" % exc)

SITE = "https://learn.microsoft.com"
CATALOG_URL = SITE + "/api/catalog/"
USER_AGENT = "Mozilla/5.0 (compatible; microsoft-learn-to-audio/1.0; personal offline reading)"
CATALOG_MAX_AGE = 7 * 24 * 3600  # seconds

ADMONITIONS = {"NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


MAX_NAME = 40  # course/path/module/unit names nest four deep; keep them short

UNIT_SEGMENT_RE = re.compile(r"^(\d+)[-.](.*)$")


def slugify(text: str, maxlen: int = 0) -> str:
    """Filesystem-safe, lowercase, hyphenated version of *text*."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    limit = maxlen or MAX_NAME
    if len(text) > limit:
        text = text[:limit].rstrip("-")
    return text or "untitled"


def url_segment(url: str) -> str:
    """Last path segment of a Learn URL: ``.../modules/shape-data/`` -> ``shape-data``."""
    path = urlparse(url).path.rstrip("/") if "//" in url else url.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def branch_name(order: int, url: str, title: str) -> str:
    """Directory name for a learning path or module.

    Names come from the URL rather than the title: titles slugify to
    40-character mouthfuls and four levels of them run Windows out of its
    260-character path budget. Learning-path and module segments never start
    with a number, so the ordinal goes in front to keep reading order.
    """
    return "%02d-%s" % (order, slugify(url_segment(url)) if url_segment(url)
                        else slugify(title))


def unit_name(order: int, url: str, title: str) -> str:
    """File stem for a unit: its ordinal, then its URL segment.

    Unit segments already start with a number (``1-introduction``,
    ``10-summary``), but that number is the module author's and does not
    always match the unit's position: one DP-600 module numbers its ten unit
    pages 1, 2, 3, 3a, 4, 4b, 4c, 5, 6, 7. Reusing it would file
    ``5-exercise`` ahead of ``4b-visualize`` in ``build_epub.py``'s
    manifest-less fallback, which sorts names as text. So the leading number
    is dropped in favour of the zero-padded ordinal, and the rest of the
    segment carries the name.
    """
    segment = url_segment(url)
    match = UNIT_SEGMENT_RE.match(segment)
    if match and match.group(2).strip("-."):
        segment = match.group(2)
    return "%02d-%s" % (order, slugify(segment) if segment else slugify(title))


def long_path(path: Path) -> str:
    """Windows refuses paths over 260 characters without this prefix.

    A four-level course tree under a deep output directory hits that limit
    easily, so every filesystem call goes through here.
    """
    if os.name != "nt":
        return str(path)
    full = os.path.abspath(str(path))
    if full.startswith("\\\\?\\"):
        return full
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def normalize_path(url: str) -> str:
    """Strip host, locale prefix, query and trailing slash from a Learn URL."""
    path = urlparse(url).path if "//" in url else url
    path = re.sub(r"^/[a-z]{2}(-[a-z]{2,4})?/", "/", path)
    return "/" + path.strip("/").lower()


def localize(url: str, locale: str) -> str:
    """Rewrite the locale segment of a learn.microsoft.com URL."""
    parsed = urlparse(url)
    path = re.sub(r"^/[a-z]{2}(-[a-z]{2,4})?/", "/", parsed.path)
    return "%s/%s%s" % (SITE, locale, path)


def yaml_scalar(value) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Fetcher:
    """Session with polite throttling and retry on transient failures."""

    def __init__(self, delay: float = 0.25, retries: int = 3, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _throttle(self) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.delay
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str) -> "requests.Response":
        last = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as exc:
                last = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = requests.HTTPError("HTTP %s for %s" % (resp.status_code, url))
                else:
                    raise requests.HTTPError("HTTP %s for %s" % (resp.status_code, url))
            time.sleep(1.5 * (attempt + 1))
        raise requests.HTTPError("giving up on %s: %s" % (url, last))


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


class Catalog:
    """The public Microsoft Learn catalog API, cached on disk.

    Course and learning-path pages render their children with JavaScript, so
    their HTML carries no child links. The catalog is the only reliable source
    for the course -> learning path -> module hierarchy.
    """

    def __init__(self, data: dict):
        self.data = data
        self.by_uid: dict[str, dict] = {}
        self.by_path: dict[str, dict] = {}
        for kind in ("courses", "learningPaths", "modules"):
            for raw in data.get(kind, []):
                entry = dict(raw, _kind=kind)
                self.by_uid[entry["uid"]] = entry
                if entry.get("url"):
                    self.by_path[normalize_path(entry["url"])] = entry

    @classmethod
    def load(cls, fetcher: "Fetcher", locale: str, cache_dir: Path,
             refresh: bool = False) -> "Catalog":
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / ("catalog-%s.json" % locale)
        fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < CATALOG_MAX_AGE
        if not refresh and fresh:
            try:
                return cls(json.loads(cache.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                print("Cached catalog is unreadable; downloading a fresh copy.",
                      file=sys.stderr)
        print("Downloading Microsoft Learn catalog (%s)..." % locale, file=sys.stderr)
        resp = fetcher.get("%s?locale=%s" % (CATALOG_URL, locale))
        # Parse before caching and swap the file in atomically: the catalog is
        # ~14 MB, and a half-written one would otherwise look fresh for a week
        # and break every run.
        data = resp.json()
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_text(resp.text, encoding="utf-8")
        os.replace(str(tmp), str(cache))
        return cls(data)

    def resolve(self, ref: str):
        """Resolve a URL, uid or bare slug to a catalog entry."""
        ref = (ref or "").strip()
        if ref in self.by_uid:
            return self.by_uid[ref]
        if "/" in ref:
            path = normalize_path(ref)
            if path in self.by_path:
                return self.by_path[path]
            # A unit URL: fall back to its parent module.
            parts = path.strip("/").split("/")
            if len(parts) > 3 and parts[0] == "training":
                parent = "/" + "/".join(parts[:3])
                if parent in self.by_path:
                    return self.by_path[parent]
            return None
        slug = ref.lower()
        for prefix in ("/training/courses/", "/training/paths/", "/training/modules/"):
            if prefix + slug in self.by_path:
                return self.by_path[prefix + slug]
        return self.by_uid.get("course." + slug)


# --------------------------------------------------------------------------
# tree model
# --------------------------------------------------------------------------


@dataclass
class Unit:
    uid: str
    title: str
    url: str
    order: int
    duration_minutes: "int | None" = None
    path: str = ""


@dataclass
class Module:
    uid: str
    title: str
    url: str
    order: int
    units: "list[Unit]" = field(default_factory=list)


@dataclass
class LearningPath:
    uid: str
    title: str
    url: str
    order: int
    modules: "list[Module]" = field(default_factory=list)
    synthetic: bool = False


@dataclass
class Course:
    uid: str
    title: str
    url: str
    kind: str
    learning_paths: "list[LearningPath]" = field(default_factory=list)


def build_hierarchy(catalog: Catalog, entry: dict, locale: str,
                    lp_filter, module_filter) -> Course:
    """Assemble the course/learning-path/module skeleton (units come later)."""
    kind = entry["_kind"]
    root = Course(uid=entry["uid"], title=entry["title"],
                  url=localize(entry["url"], locale), kind=kind)

    def make_module(uid: str, order: int):
        mod = catalog.by_uid.get(uid)
        if not mod:
            print("  ! module not in catalog, skipping: %s" % uid, file=sys.stderr)
            return None
        return Module(uid=mod["uid"], title=mod["title"],
                      url=localize(mod["url"], locale), order=order)

    def make_path(uid: str, order: int):
        lp = catalog.by_uid.get(uid)
        if not lp:
            print("  ! learning path not in catalog, skipping: %s" % uid, file=sys.stderr)
            return None
        node = LearningPath(uid=lp["uid"], title=lp["title"],
                            url=localize(lp["url"], locale), order=order)
        for muid in lp.get("modules", []):
            mod = make_module(muid, len(node.modules) + 1)
            if mod:
                node.modules.append(mod)
        return node

    if kind == "courses":
        standalone = LearningPath(uid=root.uid + ".modules", title="Standalone modules",
                                  url=root.url, order=0, synthetic=True)
        for item in entry.get("study_guide") or []:
            if item.get("type") == "learningPath":
                node = make_path(item["uid"], len(root.learning_paths) + 1)
                if node:
                    root.learning_paths.append(node)
            elif item.get("type") == "module":
                mod = make_module(item["uid"], len(standalone.modules) + 1)
                if mod:
                    standalone.modules.append(mod)
        if standalone.modules:
            standalone.order = len(root.learning_paths) + 1
            root.learning_paths.append(standalone)
        if not root.learning_paths:
            raise SystemExit(
                "Course %s lists no learning paths or modules in the catalog." % root.uid)
    elif kind == "learningPaths":
        node = make_path(entry["uid"], 1)
        root.learning_paths = [node] if node else []
    else:  # a single module
        lp = LearningPath(uid=root.uid, title=root.title, url=root.url, order=1,
                          synthetic=True)
        lp.modules = [Module(uid=entry["uid"], title=entry["title"],
                             url=localize(entry["url"], locale), order=1)]
        root.learning_paths = [lp]

    if lp_filter:
        wanted = catalog.resolve(lp_filter)
        key = wanted["uid"] if wanted else lp_filter
        kept = [lp for lp in root.learning_paths
                if lp.uid == key or normalize_path(lp.url) == normalize_path(key)]
        if not kept:
            raise SystemExit("--learning-path %r is not part of %r. Available:\n%s"
                             % (lp_filter, root.title,
                                "\n".join("  %s  (%s)" % (lp.title, lp.uid)
                                          for lp in root.learning_paths)))
        root.learning_paths = kept

    if module_filter:
        wanted = catalog.resolve(module_filter)
        key = wanted["uid"] if wanted else module_filter
        kept = []
        for lp in root.learning_paths:
            mods = [m for m in lp.modules
                    if m.uid == key or normalize_path(m.url) == normalize_path(key)]
            if mods:
                lp.modules = mods
                kept.append(lp)
        if not kept:
            raise SystemExit("--module %r is not part of %r. Available:\n%s"
                             % (module_filter, root.title,
                                "\n".join("  %s  (%s)" % (m.title, m.uid)
                                          for lp in root.learning_paths for m in lp.modules)))
        root.learning_paths = kept

    return root


def unit_duration_index(catalog: Catalog) -> "dict[str, int]":
    return {u["uid"]: u["duration_in_minutes"]
            for u in catalog.data.get("units", [])
            if u.get("duration_in_minutes") is not None}


def fetch_units(fetcher: Fetcher, module: Module, catalog: Catalog,
                durations: "dict[str, int]") -> None:
    """Populate ``module.units`` from the module landing page.

    The page's ``#unit-list`` carries the real unit hrefs. Unit URLs cannot be
    derived from catalog uids: roughly half of all modules number or name their
    unit pages differently from their unit uids.
    """
    soup = BeautifulSoup(fetcher.get(module.url).text, "html.parser")
    unit_list = soup.find(id="unit-list")
    units: "list[Unit]" = []
    if unit_list:
        for li in unit_list.find_all("li", recursive=False):
            anchor = li.find("a", class_="unit-title") or li.find("a", href=True)
            if not anchor or not anchor.get("href"):
                continue
            uid = li.get("data-unit-uid") or li.get("data-progress-uid") or ""
            units.append(Unit(
                uid=uid,
                title=anchor.get_text(strip=True),
                url=urljoin(module.url.rstrip("/") + "/", anchor["href"]),
                order=len(units) + 1,
                duration_minutes=durations.get(uid),
            ))
    if not units:
        # Fallback: derive URLs from catalog uids (correct for most modules).
        entry = catalog.by_uid.get(module.uid, {})
        base = module.url.rstrip("/") + "/"
        for i, uid in enumerate(entry.get("units", []), start=1):
            slug = uid[len(module.uid) + 1:].lstrip(".") if uid.startswith(module.uid) else uid
            units.append(Unit(uid=uid, title=slug.replace("-", " ").capitalize(),
                              url=urljoin(base, "%d-%s/" % (i, slug)), order=i,
                              duration_minutes=durations.get(uid)))
        if units:
            print("  ! %s: no unit list on page, guessed %d unit URLs"
                  % (module.uid, len(units)), file=sys.stderr)
    module.units = units


def safe_fetch_units(fetcher: Fetcher, module: Module, catalog: Catalog,
                     durations: "dict[str, int]") -> None:
    """``fetch_units`` that logs and moves on, so one dead module page does not
    take a 60-module course down with it."""
    try:
        fetch_units(fetcher, module, catalog, durations)
    except Exception as exc:  # noqa: BLE001 - one bad module must not stop the run
        print("  ! %s: %s" % (module.uid, exc), file=sys.stderr)


# --------------------------------------------------------------------------
# HTML -> Markdown
# --------------------------------------------------------------------------


class LearnConverter(MarkdownConverter):
    """markdownify converter that resolves relative Learn URLs to absolute."""

    def __init__(self, base_url: str, **options):
        options.setdefault("heading_style", "ATX")
        options.setdefault("bullets", "-")
        options.setdefault("code_language_callback", self._code_language)
        super().__init__(**options)
        self.base_url = base_url

    @staticmethod
    def _code_language(el) -> str:
        for cls in (el.get("class") or []):
            if cls.startswith("lang-"):
                return cls[5:]
        return ""

    def convert_img(self, el, text, *args, **kwargs):
        src = el.get("src") or el.get("data-src") or ""
        if src:
            el["src"] = urljoin(self.base_url, src)
        return super().convert_img(el, text, *args, **kwargs)

    def convert_a(self, el, text, *args, **kwargs):
        href = el.get("href")
        if href and not href.startswith(("#", "mailto:", "javascript:")):
            el["href"] = urljoin(self.base_url, href)
        return super().convert_a(el, text, *args, **kwargs)


def clean_content(soup: BeautifulSoup, container, base_url: str) -> None:
    """Normalize Learn-specific markup into plain HTML markdownify understands."""
    for el in container.select("span.visually-hidden, span.docon, button, script, style, "
                               "div.xp-tag, .is-hidden-visually"):
        el.decompose()

    # Video embeds -> a marker paragraph that strip_for_audio.py can remove.
    for wrapper in container.select("div.embeddedvideo, div.video-container"):
        iframe = wrapper.find("iframe")
        wrapper.replace_with(video_marker(soup, iframe, base_url))
    for iframe in container.find_all("iframe"):
        iframe.replace_with(video_marker(soup, iframe, base_url))

    # Note/Tip/Important/Warning boxes -> blockquotes.
    for div in container.find_all("div", class_=True):
        labels = ADMONITIONS.intersection(div.get("class") or [])
        if not labels:
            continue
        label = sorted(labels)[0].capitalize()
        quote = soup.new_tag("blockquote")
        head = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = label
        head.append(strong)
        quote.append(head)
        children = list(div.children)
        # The box's first paragraph is its own "Note"/"Tip" label; drop it.
        for i, child in enumerate(children):
            if (getattr(child, "name", None) == "p"
                    and child.get_text(strip=True).lower() == label.lower()):
                children.pop(i)
                break
        for child in children:
            quote.append(child.extract() if hasattr(child, "extract") else child)
        div.replace_with(quote)


def video_marker(soup: BeautifulSoup, iframe, base_url: str):
    """A ``[Video: url]`` paragraph standing in for an embedded player."""
    src = (iframe.get("src") or "") if iframe is not None else ""
    marker = soup.new_tag("p")
    marker.string = "[Video: %s]" % urljoin(base_url, src) if src else "[Video]"
    return marker


def quiz_to_markdown(form) -> str:
    """Render a knowledge-check form as Markdown (correct answers are server-side)."""
    lines: "list[str]" = []
    for i, question in enumerate(form.select("div.quiz-question"), start=1):
        label = question.select_one("div.field-label")
        text = " ".join(label.get_text(" ", strip=True).split()) if label else ""
        text = re.sub(r"^\d+\.\s*", "", text)
        lines.append("%d. %s" % (i, text))
        for choice in question.select("div.radio-label-text, div.checkbox-label-text"):
            answer = " ".join(choice.get_text(" ", strip=True).split())
            if answer:
                lines.append("    - " + answer)
        lines.append("")
    return "\n".join(lines).strip()


def unit_to_markdown(html: str, url: str) -> "tuple[str, str]":
    """Return ``(title, markdown_body)`` for a unit page."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(id="module-unit-title")
    title = heading.get_text(strip=True) if heading else ""

    parts: "list[str]" = []
    content = soup.find(id="module-unit-content")
    if content and content.get_text(strip=True):
        clean_content(soup, content, url)
        parts.append(LearnConverter(url).convert_soup(content).strip())

    form = soup.find(id="question-container")
    if form:
        quiz = quiz_to_markdown(form)
        if quiz:
            parts.append("## Knowledge check\n\n" + quiz)

    body = "\n\n".join(p for p in parts if p)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return title, body


def render_unit_file(unit: Unit, module: Module, lp: LearningPath, course: Course,
                     title: str, body: str) -> str:
    front = [
        ("title", title or unit.title),
        ("uid", unit.uid),
        ("url", unit.url),
        ("order", unit.order),
        ("duration_minutes", unit.duration_minutes),
        ("module", module.title),
        ("module_uid", module.uid),
        ("learning_path", lp.title),
        ("learning_path_uid", lp.uid),
        ("course", course.title),
        ("course_uid", course.uid),
    ]
    lines = ["---"]
    lines += ["%s: %s" % (k, yaml_scalar(v)) for k, v in front]
    lines += ["---", "", "# " + (title or unit.title), "", body, ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def print_tree(course: Course) -> None:
    print("%s  [%s]" % (course.title, course.kind))
    for lp in course.learning_paths:
        indent = 2
        if not (lp.synthetic and lp.title == course.title):
            print("  %02d. %s" % (lp.order, lp.title))
            indent = 6
        for mod in lp.modules:
            if not (course.kind == "modules" and mod.title == course.title):
                print("%s%02d. %s  (%d units)" % (" " * indent, mod.order, mod.title,
                                                  len(mod.units)))
            for unit in mod.units:
                print("%s%02d. %s" % (" " * (indent + 4), unit.order, unit.title))


def merge_manifest(root: Path, manifest: dict) -> dict:
    """Fold this run's manifest into the one an earlier run left behind.

    ``--learning-path``/``--module`` narrow the hierarchy, so writing the
    manifest straight out would drop everything a previous partial run
    recorded -- and build_epub.py prefers the manifest over the directory
    tree, so those units would silently vanish from the book.
    """
    path = long_path(root / "manifest.json")
    if not os.path.exists(path):
        return manifest
    try:
        with open(path, encoding="utf-8") as handle:
            old = json.load(handle)
    except (OSError, ValueError):
        return manifest
    if old.get("course", {}).get("uid") != manifest["course"]["uid"]:
        return manifest  # a different course in the same folder: don't merge
    paths = {lp["uid"]: lp for lp in old.get("learning_paths", [])}
    for lp in manifest["learning_paths"]:
        previous = paths.get(lp["uid"])
        if not previous:
            paths[lp["uid"]] = lp
            continue
        modules = {m["uid"]: m for m in previous.get("modules", [])}
        # This run re-listed the modules it touched, so its copies win.
        modules.update({m["uid"]: m for m in lp["modules"]})
        previous["modules"] = sorted(modules.values(), key=lambda m: m["order"])
    return dict(manifest,
                learning_paths=sorted(paths.values(), key=lambda lp: lp["order"]))


def download(course: Course, out_root: Path, fetcher: Fetcher, workers: int,
             force: bool) -> "tuple[Path, int, int, int]":
    # The requested entity is the tree root, so its own level is not repeated
    # inside itself: a course gets course/path/module/unit.md, a learning path
    # gets path/module/unit.md, a single module gets module/unit.md.
    keep_lp_dir = course.kind == "courses"
    keep_module_dir = course.kind in ("courses", "learningPaths")

    # The root keeps its URL segment as-is: courses have no order to record.
    root = out_root / (slugify(url_segment(course.url)) if url_segment(course.url)
                       else slugify(course.title))
    jobs = []
    for lp in course.learning_paths:
        # The synthetic "Standalone modules" path borrows the course URL, so
        # naming it from a segment would repeat the course name; use its title.
        lp_name = ("%02d-%s" % (lp.order, slugify(lp.title)) if lp.synthetic
                   else branch_name(lp.order, lp.url, lp.title))
        lp_dir = root / lp_name if keep_lp_dir else root
        for mod in lp.modules:
            mod_dir = (lp_dir / branch_name(mod.order, mod.url, mod.title)
                       if keep_module_dir else lp_dir)
            for unit in mod.units:
                dest = mod_dir / (unit_name(unit.order, unit.url, unit.title) + ".md")
                unit.path = str(dest.relative_to(root)).replace(os.sep, "/")
                jobs.append((unit, mod, lp, dest))

    counts = {"written": 0, "skipped": 0, "failed": 0}
    lock = threading.Lock()
    total = len(jobs)

    def work(job):
        unit, mod, lp, dest = job
        if os.path.exists(long_path(dest)) and not force:
            with lock:
                counts["skipped"] += 1
            return
        try:
            html = fetcher.get(unit.url).text
            title, body = unit_to_markdown(html, unit.url)
            os.makedirs(long_path(dest.parent), exist_ok=True)
            with open(long_path(dest), "w", encoding="utf-8") as handle:
                handle.write(render_unit_file(unit, mod, lp, course, title, body))
        except Exception as exc:  # noqa: BLE001 - one bad unit must not stop the run
            with lock:
                counts["failed"] += 1
            print("  ! failed %s: %s" % (unit.url, exc), file=sys.stderr)
            return
        with lock:
            counts["written"] += 1
            done = counts["written"] + counts["skipped"] + counts["failed"]
            print("  [%d/%d] %s" % (done, total, unit.path))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, jobs))

    manifest = {
        "course": {"uid": course.uid, "title": course.title, "url": course.url,
                   "kind": course.kind},
        "learning_paths": [
            {
                "uid": lp.uid, "title": lp.title, "url": lp.url, "order": lp.order,
                "synthetic": lp.synthetic,
                "modules": [
                    {
                        "uid": m.uid, "title": m.title, "url": m.url, "order": m.order,
                        "units": [
                            {"uid": u.uid, "title": u.title, "url": u.url,
                             "order": u.order, "path": u.path,
                             "duration_minutes": u.duration_minutes}
                            for u in m.units
                        ],
                    }
                    for m in lp.modules
                ],
            }
            for lp in course.learning_paths
        ],
    }
    os.makedirs(long_path(root), exist_ok=True)
    manifest = merge_manifest(root, manifest)
    with open(long_path(root / "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return root, counts["written"], counts["skipped"], counts["failed"]


def main() -> int:
    global MAX_NAME

    parser = argparse.ArgumentParser(
        description="Download a Microsoft Learn course, learning path or module as Markdown.")
    parser.add_argument("target", help="Course/learning path/module URL, uid or slug")
    parser.add_argument("-o", "--out", default=Path("content"), type=Path,
                        help="Output root directory (default: content)")
    parser.add_argument("--learning-path", metavar="REF",
                        help="Only this learning path of the course (URL, uid or slug)")
    parser.add_argument("--module", metavar="REF",
                        help="Only this module (URL, uid or slug)")
    parser.add_argument("--locale", default="en-us", help="Content locale (default: en-us)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel downloads (default: 4)")
    parser.add_argument("--delay", type=float, default=0.25,
                        help="Seconds between requests (default: 0.25)")
    parser.add_argument("--max-name", type=int, default=MAX_NAME,
                        help="Max characters per directory/file name (default: %d)" % MAX_NAME)
    parser.add_argument("--force", action="store_true", help="Re-download existing files")
    parser.add_argument("--list", action="store_true",
                        help="Print the unit tree and exit without downloading")
    parser.add_argument("--refresh-catalog", action="store_true",
                        help="Ignore the cached catalog and download a fresh copy")
    args = parser.parse_args()
    MAX_NAME = max(8, args.max_name)

    fetcher = Fetcher(delay=args.delay)
    # The catalog cache belongs to the project, not to src/.
    cache_dir = Path(__file__).resolve().parent.parent / ".cache"
    catalog = Catalog.load(fetcher, args.locale, cache_dir,
                           refresh=args.refresh_catalog)
    durations = unit_duration_index(catalog)

    entry = catalog.resolve(args.target)
    if not entry:
        print("Could not resolve %r in the Microsoft Learn catalog.\n"
              "Expected a /training/courses/..., /training/paths/... or "
              "/training/modules/... URL, or a catalog uid." % args.target, file=sys.stderr)
        return 2
    print("Resolved: %s  (%s, %s)" % (entry["title"], entry["_kind"][:-1], entry["uid"]))

    course = build_hierarchy(catalog, entry, args.locale, args.learning_path, args.module)

    modules = [m for lp in course.learning_paths for m in lp.modules]
    print("Listing units for %d module(s)..." % len(modules))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda m: safe_fetch_units(fetcher, m, catalog, durations), modules))

    if args.list:
        print_tree(course)
        return 0

    total_units = sum(len(m.units) for m in modules)
    print("Downloading %d unit(s)..." % total_units)
    root, written, skipped, failed = download(course, args.out, fetcher, args.workers,
                                              args.force)
    print("\nWrote %s" % root)
    print("Done: %d written, %d skipped, %d failed." % (written, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
