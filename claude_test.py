import requests
from bs4 import BeautifulSoup
import json
import re
from urllib.parse import urljoin, urlparse
 
HOME = "https://cvc.cervantes.es/"
BASE = "https://cvc.cervantes.es/ensenanza/biblioteca_ele/plan_curricular/niveles/"
 
URLS = {
    "A1_A2": BASE + "09_nociones_especificas_inventario_a1-a2.htm",
    "B1_B2": BASE + "09_nociones_especificas_inventario_b1-b2.htm",
    "C1_C2": BASE + "09_nociones_especificas_inventario_c1-c2.htm",
}
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": HOME,
}
 
# ── Cache ─────────────────────────────────────────────────────────────────────
PAGE_CACHE: dict = {}
 
 
_CROSSREF_RE = re.compile(r'^\s*\[\s*v\.', re.IGNORECASE)

def is_crossref_text(text: str) -> bool:
    """Return True for navigation lines like '[ v. Saberes ... ]' or '[ v. Nociones ... ]'.
    These are cross-reference pointers, never actual content items."""
    return bool(_CROSSREF_RE.match(text))


def make_slug(text: str) -> str:
    text = text.strip()
    text = re.sub(r'\.', '_', text)
    text = re.sub(r'\s+', '_', text)
    text = re.sub(r'_+', '_', text)
    return text.strip('_').lower()
 
 
def get_soup(session, url: str) -> BeautifulSoup:
    bare = url.split('#')[0]
    if bare not in PAGE_CACHE:
        r = session.get(bare, headers=HEADERS, timeout=30)
        r.encoding = "utf-8"
        PAGE_CACHE[bare] = BeautifulSoup(r.text, "html.parser")
    return PAGE_CACHE[bare]
 
 
# ── Helpers to read a single table ───────────────────────────────────────────
 
def parse_li(li, session=None, page_url=None, depth=0) -> dict:
    """
    Parse one <li> into a structured dict:
        {
          "text":    "Instituciones educativas",
          "detail":  "institutos de enseñanza secundaria, centros de formación...",
          "refs":    { "saberes_y_comportamientos": [...], ... }   ← followed links
        }
 
    The <li> structure on CVC pages:
        <li>
          Title text
          <br/>
          <em>detail text</em>
          <p>[<a href="...#fragment">v. Label</a>]</p>
        </li>
    """

    # -- title: direct text nodes only (before <br> or <em>) --
    title_parts = []
    for node in li.children:
        node_name = getattr(node, 'name', None)
        if node_name is not None:
            if node_name in ("br", "em", "p", "ul", "ol"):
                break                              # these END the title
            if node_name in ("strong", "b", "span", "abbr"):
                t = node.get_text(" ", strip=True) # these ARE the title
                if t:
                    title_parts.append(t)
        else:
            t = str(node).strip()                  # plain text node
            if t:
                title_parts.append(t)
    title = " ".join(title_parts).strip()
 
    # -- detail: text inside <em> --
    em = li.find("em")
    detail = em.get_text(" ", strip=True) if em else ""
 
    # -- inline [v.] links: <a href> tags inside <p> children --
    inline_refs_text = []
    refs = {
        "saberes_y_comportamientos": [],
        "nociones_generales":        [],   # ← ADD
        "nociones_especificas":      [],
        "referentes_culturales":     {},
    }
    # Capture [v. ...] text from <p> tags regardless of depth
    for p in li.find_all("p"):
        p_text = p.get_text(" ", strip=True)
        if p_text:
            inline_refs_text.append(p_text)

    if session and page_url and depth == 0:
        for a in li.find_all("a", href=True):
            href      = a["href"]
            link_text = a.get_text(strip=True)
            if href.startswith("#") or "#" not in href:
                continue
            category = classify_href(href)
            print(f"          ↳ inline [{link_text}] → {category}")
            content = resolve_linked_section(session, href, page_url, link_text, depth=depth+1)
            if category == "referentes_culturales":
                if isinstance(content, dict):
                    for k, v in content.items():
                        refs["referentes_culturales"].setdefault(k, [])
                        if isinstance(v, list):
                            refs["referentes_culturales"][k].extend(v)
                else:
                    refs["referentes_culturales"].setdefault("all", [])
                    refs["referentes_culturales"]["all"].extend(content)
            else:
                if isinstance(content, list):
                    refs[category].extend(content)
                elif isinstance(content, dict):
                    # flatten dict values into the list
                    for v in content.values():
                        if isinstance(v, list):
                            refs[category].extend(v)
 
    result = {}
    if title:
        result["text"] = title
    if detail:
        result["detail"] = detail
    if inline_refs_text:
        result["links"] = inline_refs_text
    # Only include refs if something was actually followed
    has_refs = (
        refs["saberes_y_comportamientos"]
        or refs["nociones_generales"]
        or refs["nociones_especificas"]
        or refs["referentes_culturales"]
    )
    if has_refs:
        result["refs"] = refs
 
    return result
 
 
def cells_from_td(td, session=None, page_url=None, depth=0) -> list:
    """
    Walk the direct children of <td> in document order.
    Groups items under their nearest preceding sub-section header (<strong>).
    Falls back to the old flat <li> approach for simple cells (nociones pages).
    """
    # ── Detect whether this td uses sub-section headers ──────────────────────
    # A td has sub-section headers when it contains <strong> tags that are
    # direct (or near-direct) children — not nested inside <li>.
    direct_strongs = [
        ch for ch in td.children
        if getattr(ch, 'name', None) == 'strong'
    ]
 
    if not direct_strongs:
        # Simple td: just extract <li> items as before (nociones-style)
        return _cells_flat(td, session, page_url, depth)
 
    # ── Sub-section aware parsing ─────────────────────────────────────────────
    items = []
    current_section: dict | None = None   # {"heading": "1.5.1...", "links": [], "items": [...]}
 
    def flush_section():
        nonlocal current_section
        if current_section is not None:
            items.append(current_section)
            current_section = None
 
    for child in td.children:
        tag = getattr(child, 'name', None)
 
        if tag == 'strong':
            # New sub-section
            flush_section()
            heading_text = child.get_text(" ", strip=True)
            if heading_text:
                current_section = {
                    "heading": heading_text,
                    "links": [],      # [v. ...] <p> tags right after the <strong>
                    "items": [],      # parsed <li> entries from following <ul>/<ol>
                }
 
        elif tag == 'p':
            p_text = child.get_text(" ", strip=True)
            if not p_text:
                continue
            if is_crossref_text(p_text):
                # [v. ...] cross-reference pointer — store as link metadata only,
                # never as a content item (regardless of whether we have a section)
                if current_section is not None:
                    current_section["links"].append(p_text)
                    if session and page_url and depth == 0:
                        for a in child.find_all("a", href=True):
                            href      = a["href"]
                            link_text = a.get_text(strip=True)
                            if href.startswith("#") or "#" not in href:
                                continue
                            category = classify_href(href)
                            print(f"        ↳ section-link [{link_text}] → {category}")
                            content = resolve_linked_section(
                                session, href, page_url, link_text, depth=depth+1)
                            current_section.setdefault("refs", {}).setdefault(category, {})
                            merge_into(current_section["refs"][category], content)
                # If no current_section, it's a cross-ref orphan — silently skip it
            elif current_section is not None:
                # Regular paragraph belonging to the current sub-section
                current_section["links"].append(p_text)
            else:
                # Plain paragraph before any heading → bare content item
                items.append({"text": p_text})
 
        elif tag in ('ul', 'ol'):
            lis = child.find_all("li", recursive=False) or child.find_all("li")
            parsed_lis = []
            for li in lis:
                p = parse_li(li, session, page_url, depth=depth)
                if p:
                    parsed_lis.append(p)
            if current_section is not None:
                current_section["items"].extend(parsed_lis)
            else:
                items.extend(parsed_lis)
 
        elif tag is None:
            # plain text node
            t = str(child).strip()
            if t:
                if current_section is not None:
                    # Rare: stray text after heading, before <ul>
                    current_section.setdefault("text_nodes", []).append(t)
                else:
                    items.append({"text": t})
 
        # other tags (br, span, a, …) — skip or absorb into surrounding context
 
    flush_section()
 
    # Clean up: remove empty link lists
    for item in items:
        if isinstance(item, dict):
            if "links" in item and not item["links"]:
                del item["links"]
 
    return items
 
 
def _cells_flat(td, session=None, page_url=None, depth=0) -> list:
    """Original flat logic for simple <td> cells (nociones-style)."""
    items = []
    lis = td.find_all("li", recursive=False) or td.find_all("li")
    if lis:
        for li in lis:
            parsed = parse_li(li, session, page_url, depth=depth)
            if parsed:
                items.append(parsed)
    else:
        w = td.get_text(" ", strip=True)
        if w and not is_crossref_text(w):
            items.append(w)
    return items
 
 
def read_table_flat(table, session=None, page_url=None, depth=0) -> list:
    items = []
    for td in table.find_all("td"):
        items.extend(cells_from_td(td, session, page_url, depth=depth))
    return items
 
 
def slugify_header(raw: str) -> str:
    """Normalise a <th> text to an ASCII slug."""
    raw = raw.lower()
    for accented, plain in [
        ("áàä","a"), ("éèë","e"), ("íìï","i"), ("óòö","o"), ("úùü","u")
    ]:
        for ch in accented:
            raw = raw.replace(ch, plain)
    raw = re.sub(r'\s+', '_', raw.strip())
    return raw
 
 
def read_table_by_fase(table, session=None, page_url=None, depth=0) -> dict:
    headers = {}
    for i, th in enumerate(table.find_all("th")):
        headers[i] = slugify_header(th.get_text(" ", strip=True))

    if not headers:
        return {"all": read_table_flat(table, session, page_url, depth=depth)}

    result = {v: [] for v in headers.values()}
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        for col_idx, td in enumerate(tds):
            col_name = headers.get(col_idx, f"col_{col_idx}")
            result.setdefault(col_name, [])
            result[col_name].extend(cells_from_td(td, session, page_url, depth=depth))

    return result

def _parse_li_no_session(li) -> dict:
    """parse_li without a session — structured output but never follows hrefs."""
    title_parts = []
    for node in li.children:
        node_name = getattr(node, 'name', None)
        if node_name in ("br", "em", "p", "ul", "ol"):
            break
        if node_name in ("strong", "b", "span", "abbr"):
            t = node.get_text(" ", strip=True)
            if t: title_parts.append(t)
        elif node_name is None:
            t = str(node).strip()
            if t: title_parts.append(t)
    title = " ".join(title_parts).strip()
    em = li.find("em")
    detail = em.get_text(" ", strip=True) if em else ""
    links = [p.get_text(" ", strip=True) for p in li.find_all("p") if p.get_text(strip=True)]
    result = {}
    if title:  result["text"]   = title
    if detail: result["detail"] = detail
    if links:  result["links"]  = links
    return result 
 
# ── Keywords on the main page ─────────────────────────────────────────────────
 
def extract_keywords_structured(h3_tag, session=None, page_url=None) -> dict:
    """
    Two cases:
      B) h3 is inside <caption> of its own table → use that table directly
      A) h3 is a standalone element → walk next siblings for table/list

    Always uses depth=1 so that inline [v. ...] links inside keyword cells are
    recorded as metadata (links[]) but never followed — keyword extraction is
    about the vocabulary/content items, not cross-references.
    """
    # ── Case B: h3 lives inside a <caption> (most nociones sections) ─────────
    parent_caption = h3_tag.find_parent("caption")
    if parent_caption is not None:
        parent_table = parent_caption.find_parent("table")
        if parent_table is not None:
            return read_table_by_fase(parent_table, session, page_url, depth=1)

    # ── Case A: h3 is a standalone sibling ───────────────────────────────────
    for sib in h3_tag.find_next_siblings():
        if sib.name in ("h1", "h2", "h3"):
            break
        if sib.name == "table":
            return read_table_by_fase(sib, session, page_url, depth=1)
        if sib.name in ("ul", "ol"):
            items = []
            for li in sib.find_all("li"):
                parsed = parse_li(li, session, page_url, depth=1)
                if parsed:
                    items.append(parsed)
            return {"all": items}

    return {}

def extract_keywords(h_tag) -> list:
    """
    Replaces the old flat get_text version.
    Returns list of dicts {text, detail?, links?} — used inside resolve_linked_section.
    """
    items = []
    for sib in h_tag.find_next_siblings():
        if sib.name in ("h1", "h2", "h3", "h4", "h5"):
            break
        if sib.name in ("ul", "ol"):
            for li in sib.find_all("li"):
                item = _parse_li_no_session(li)
                if item:
                    items.append(item)
        elif sib.name == "p":
            w = sib.get_text(" ", strip=True)
            if w and not is_crossref_text(w):
                items.append({"text": w})
        elif sib.name == "table":
            break  # caller's read_table_by_fase handles tables
    return items
 
# ── Cross-reference resolution ────────────────────────────────────────────────
 
def classify_href(href: str) -> str:
    h = href.lower()
    if "saberes" in h or "comportamientos" in h:
        return "saberes_y_comportamientos"
    if "referentes" in h or "culturales" in h:
        return "referentes_culturales"
    if "nociones_generales" in h or "08_nociones" in h:
        return "nociones_generales"        # ← NEW
    return "nociones_especificas"
 
 
def resolve_linked_section(session, href: str, base_url: str, link_text: str = "", depth=0):
    """
    depth=0  → called from the main page (follow inline links inside results)
    depth=1  → called from a linked page (do NOT follow further links)
    """
    abs_url = urljoin(base_url, href)
    fragment = urlparse(abs_url).fragment

    try:
        soup = get_soup(session, abs_url)
    except Exception as e:
        print(f"    ⚠  fetch failed: {e}")
        return {}

    # Pass depth+1 so tables read from linked pages don't follow their own links
    if fragment:
        el = soup.find(id=fragment) or soup.find(attrs={"name": fragment})
        if el is not None:
            if el.name == "table":
                return read_table_by_fase(el, session, abs_url, depth=depth)
            if el.name in ("h2", "h3", "h4"):
                return {"all": extract_keywords(el)}
            next_table = el.find_next("table")
            next_h     = el.find_next(["h2", "h3", "h4"])
            if next_table and (not next_h or next_table.find_previous(["h2","h3","h4"]) == next_h):
                return read_table_by_fase(next_table, session, abs_url, depth=depth)
            if next_h:
                return {"all": extract_keywords(next_h)}

    if link_text:
        m = re.search(r'(\d+\.\d+(?:\.\d+)?)', link_text)
        if m:
            sec_num = m.group(1)
            candidates = [sec_num]
            parts = sec_num.split(".")
            if len(parts) > 2:
                candidates.append(".".join(parts[:2]))

            for cand in candidates:
                for h in soup.find_all(["h2", "h3", "h4"]):
                    if cand in h.get_text():
                        return {"all": extract_keywords(h)}
                for cap in soup.find_all("caption"):
                    if cand in cap.get_text():
                        tbl = cap.find_parent("table")
                        if tbl:
                            return read_table_by_fase(tbl, session, abs_url, depth=depth)

    h = soup.find("h3") or soup.find("h2")
    return {"all": extract_keywords(h)} if h else {}
 
# ── Main cross-reference extractor ───────────────────────────────────────────
 
def EMPTY_REFS():
    return {
        "saberes_y_comportamientos": {},
        "nociones_generales":        {},   # ← NEW
        "nociones_especificas":      {},
        "referentes_culturales":     {},
    }
 
 
def merge_into(target: dict, content: dict):
    """Merge content dict into target dict, extending lists."""
    for k, v in content.items():
        target.setdefault(k, [])
        if isinstance(v, list):
            target[k].extend(v)
        elif isinstance(v, dict):
            # nested dict — recurse
            if not isinstance(target[k], dict):
                target[k] = {}
            merge_into(target[k], v)
 
 
def extract_cross_references(h3_tag, session, page_url: str) -> dict:
    """
    Read [v. ] links from the <caption> of the table following h3,
    follow each link, and store content under the right category.
    """
    refs = EMPTY_REFS()
 
    table = None
    for sib in h3_tag.find_next_siblings():
        if sib.name in ("h1", "h2", "h3"):
            break
        if sib.name == "table":
            table = sib
            break
    if table is None:
        return refs
 
    caption = table.find("caption")
    if caption is None:
        return refs
 
    for a in caption.find_all("a", href=True):
        href      = a["href"]
        link_text = a.get_text(strip=True)
        category  = classify_href(href)
        print(f"      → [{link_text}]  ({href})  → {category}")
 
        content = resolve_linked_section(session, href, page_url, link_text, depth=1)
        print(f"        got {len(content)} col(s)")
        merge_into(refs[category], content)
 
    return refs
 
 
def EMPTY_ENTRY():
    return {
        "official_references": EMPTY_REFS(),
        "keywords": [],
    }
 
 
# ── Main ──────────────────────────────────────────────────────────────────────
 
session = requests.Session()
try:
    session.get(HOME, headers=HEADERS, timeout=15)
except Exception:
    pass
 
master: dict = {}
 
SAMPLE = True   # ← set False for full production run
 
for level, url in URLS.items():
    if SAMPLE and level != "A1_A2":
        continue
 
    response = session.get(url, headers=HEADERS, timeout=30)
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
 
    h3_tags = soup.find_all("h3")
    if SAMPLE:
        h3_tags = h3_tags[20:24]   # 5.5, 5.6, 5.7, 6.1
 
    print(f"\n{'='*60}\n{level}: {len(h3_tags)} sections")
 
    for h3 in h3_tags:
        raw  = h3.get_text(strip=True)
        slug = make_slug(raw)
        print(f"\n  [{slug}]")
 
        if slug not in master:
            master[slug] = {
                "A1_A2": EMPTY_ENTRY(),
                "B1_B2": EMPTY_ENTRY(),
                "C1_C2": EMPTY_ENTRY(),
            }
 
        master[slug][level]["keywords"] = extract_keywords_structured(h3, session, url)
        master[slug][level]["official_references"]  = extract_cross_references(h3, session, url)
 
# ── Save ──────────────────────────────────────────────────────────────────────
 
suffix = "_sample" if SAMPLE else ""
output_path = f"outputs/nociones_especificas{suffix}.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(master, f, ensure_ascii=False, indent=2)

print(f"\n✅  Saved {len(master)} sections → {output_path}")

# Quick preview of first entry
first_key = next(iter(master))
entry = master[first_key]["A1_A2"]
print(f"\nFirst key : {first_key}")
print(f"keywords  : {entry['keywords'][:5]}")
print(f"saberes   : {list(entry['official_references']['saberes_y_comportamientos'].items())[:3]}")
print(f"nociones  : {list(entry['official_references']['nociones_especificas'].items())[:3]}")