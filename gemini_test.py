import requests
from bs4 import BeautifulSoup
import json
import copy
import re
import time
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
        retries = 3
        for attempt in range(retries):
            try:
                # Defense spacing delay: execution pause ONLY on fresh downloads
                time.sleep(1) 
                
                r = session.get(bare, headers=HEADERS, timeout=30)
                r.encoding = "utf-8"
                PAGE_CACHE[bare] = BeautifulSoup(r.text, "html.parser")
                break  # Successful fetch, exit retry loop
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt == retries - 1:
                    raise e  # Hard crash only if all fallback attempts fail
                print(f"    ⚠ Network connection dropped on {bare} (attempt {attempt+1}/{retries}). Retrying in 3 seconds...")
                time.sleep(3)
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
    title = re.sub(r'^[—\-\s•]+', '', title).strip()
 
    # -- detail: text inside <em> --
    em = None
    for cand in li.find_all("em"):
        parent = cand.parent
        is_nested = False
        while parent and parent != li:
            if parent.name in ("ul", "ol"):
                is_nested = True
                break
            parent = parent.parent
        if not is_nested:
            em = cand
            break
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
        parent = p.parent
        is_nested = False
        while parent and parent != li:
            if parent.name in ("ul", "ol"):
                is_nested = True
                break
            parent = parent.parent
        if is_nested:
            continue
        p_text = p.get_text(" ", strip=True)
        if p_text:
            inline_refs_text.append(p_text)

    if session and page_url:
        for a in li.find_all("a", href=True):
            href      = a["href"]
            link_text = a.get_text(strip=True)
            if href.startswith("#") or "#" not in href:
                continue
            category = classify_href(href, link_text)
            print(f"          ↳ inline [{link_text}] → {category}")
            content = resolve_linked_section(session, href, page_url, link_text, depth=depth+1)
            if content:
                if category == "referentes_culturales":
                    refs["referentes_culturales"].setdefault("all", [])
                    if content not in refs["referentes_culturales"]["all"]:
                        refs["referentes_culturales"]["all"].append(content)
                else:
                    if content not in refs[category]:
                        refs[category].append(content)

    # -- Recursively capture immediate nested sub-lists --
    sub_items = []
    for sub_list in li.find_all(["ul", "ol"]):
        if sub_list.find_parent("li") == li:
            for sub_li in sub_list.find_all("li", recursive=False):
                parsed_sub = parse_li(sub_li, session, page_url, depth=depth)
                if parsed_sub:
                    sub_items.append(parsed_sub)
 
    result = {}
    if title:
        result["text"] = title
    if detail:
        result["detail"] = detail
    if inline_refs_text:
        result["links"] = inline_refs_text
    if sub_items:
        result["sub_items"] = sub_items
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
    Groups items under their nearest preceding sub-section header.
    Falls back to a flat structure if no valid sub-section patterns are detected.
    """
    import re
    # Matches any section numbering pattern at the start of a line (e.g., 1.5.1, 6.1.1, 2.3)
    _SEC_NUM_RE = re.compile(r'^\d+(?:\.\d+)+')
 
    items = []
    current_section: dict | None = None
 
    def flush_section():
        nonlocal current_section
        if current_section is not None:
            items.append(current_section)
            current_section = None
 
    for child in td.children:
        tag = getattr(child, 'name', None)
 
        if tag is None:
            # Plain text node
            t = str(child).strip()
            if t:
                if current_section is not None:
                    current_section.setdefault("text_nodes", []).append(t)
                else:
                    items.append({"text": t})
            continue
 
        # Extract full text content of the element to check for numbering patterns
        text_content = child.get_text(" ", strip=True)
        
        # Robustly detect section headers even when wrapped inside <p> or heading tags
        is_header = False
        if tag == 'strong' and _SEC_NUM_RE.match(text_content):
            is_header = True
        elif tag == 'p' and _SEC_NUM_RE.match(text_content) and (child.find('strong') or child.find('b')):
            is_header = True
        elif tag in ('h4', 'h5', 'h6') and _SEC_NUM_RE.match(text_content):
            is_header = True
 
        if is_header:
            flush_section()
            current_section = {
                "heading": text_content,
                "links": [],      # [v. ...] cross-references right after the heading
                "items": [],      # Parsed <li> entries belonging to this subsection
            }
            continue
 
        if tag == 'p':
            if not text_content:
                continue
            if is_crossref_text(text_content):
                if current_section is not None:
                    current_section["links"].append(text_content)
                    if session and page_url:
                        for a in child.find_all("a", href=True):
                            href      = a["href"]
                            link_text = a.get_text(strip=True)
                            if href.startswith("#") or "#" not in href:
                                continue
                            category = classify_href(href, link_text)
                            content = resolve_linked_section(
                                session, href, page_url, link_text, depth=depth+1)
                            current_section.setdefault("refs", {})
                            merge_into(current_section["refs"].setdefault(category, {}), content)
            elif current_section is not None:
                current_section["links"].append(text_content)
            else:
                items.append({"text": text_content})
 
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
 
    flush_section()
 
    # Clean up: remove empty link metadata lists
    for item in items:
        if isinstance(item, dict):
            if "links" in item and not item["links"]:
                del item["links"]
 
    # Global Production Protection: If no section markers were found anywhere in the cell,
    # fall back to flat parsing (safeguards simple vocabulary tables)
    has_subsections = any(isinstance(item, dict) and "heading" in item for item in items)
    if not has_subsections:
        return _cells_flat(td, session, page_url, depth)
 
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
 
def classify_href(href: str, link_text: str = "") -> str:
    h = href.lower()
    t = link_text.lower()
    if "saberes" in h or "comportamientos" in h or "saberes" in t or "comportamientos" in t:
        return "saberes_y_comportamientos"
    if "referentes" in h or "culturales" in h or "referentes" in t or "culturales" in t:
        return "referentes_culturales"
    if "nociones_generales" in h or "08_nociones" in h or "generales" in t:
        return "nociones_generales"
    return "nociones_especificas"

def structure_sub_items(items: list) -> list:
    """Group subordinate items (like 'En la interacción', 'Servicios', 'Funciones') under their parent item."""
    structured = []
    current_main = None
    
    # Common subordinate text identifiers used across Cervantes table sections
    SUB_KEYWORDS = (
        "en la interacción", "en el aula", "al comenzar estudios", 
        "servicios", "funciones", "actuaciones", "aspectos sectoriales"
    )
    
    for item in items:
        if isinstance(item, dict) and "text" in item:
            text_lower = item["text"].lower().strip()
            is_sub = any(text_lower.startswith(kw) for kw in SUB_KEYWORDS)
            
            if is_sub and current_main is not None:
                current_main.setdefault("sub_items", []).append(item)
            else:
                structured.append(item)
                current_main = item
        else:
            structured.append(item)
            current_main = None
            
    return structured

def filter_table_by_section(table_data: dict, target_sec: str) -> dict:
    """Filter parsed multi-phase table data to only include items matching a target subsection."""
    if not target_sec:
        return table_data
    
    # Check if this table actually uses subheadings (parsed via cells_from_td)
    has_subsections = any(
        isinstance(item, dict) and "heading" in item
        for items in table_data.values() if isinstance(items, list)
        for item in items
    )
    if not has_subsections:
        return table_data

    filtered = {}
    for col_name, items in table_data.items():
        filtered[col_name] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and "heading" in item:
                    # Keep items if the target subsection digits are part of the subheading text
                    if target_sec in item["heading"].strip():
                        if "items" in item and isinstance(item["items"], list):
                            filtered[col_name].extend(item["items"])

    # Return phases that contain matching elements
    return {k: v for k, v in filtered.items() if v} 
 
def resolve_linked_section(session, href: str, base_url: str, link_text: str = "", depth=0):
    """
    Resolves and extracts linked sections with a global cache check 
    to prevent redownloading or processing duplicate paths.
    """
    if not hasattr(session, "_active_crawl_path"):
        session._active_crawl_path = []
    if not hasattr(session, "_global_parsed_cache"):
        session._global_parsed_cache = {}

    abs_url = urljoin(base_url, href)
    fragment = urlparse(abs_url).fragment

    # Extract target section digits cleanly
    target_sec = ""
    if link_text:
        m = re.search(r'(\d+(?:\.\d+)*)', link_text)  # ← Cambiado + por *
        if m:
            target_sec = m.group(1)
    if not target_sec and fragment:
        m = re.search(r'(\d+(?:_\d+)*)', fragment)   # ← Cambiado + por *
        if m:
            target_sec = m.group(1).replace('_', '.')

    # Short-circuit and return standard JSON reference if the section is mapped
    if target_sec:
        base_sec = ".".join(target_sec.split(".")[:2])
        
        # Si es un dígito único (ej: "13"), busca la primera subsección que empiece con ese número (ej: "13.1")
        if base_sec not in SECCION_A_SLUG and "." not in base_sec:
            matching_sub = next((k for k in SECCION_A_SLUG.keys() if k.startswith(f"{base_sec}.")), None)
            if matching_sub:
                base_sec = matching_sub

        if base_sec in SECCION_A_SLUG:
            # Map the URL string back to your master dictionary level keys
            level_key = "A1_A2"
            if "b1-b2" in abs_url.lower():
                level_key = "B1_B2"
            elif "c1-c2" in abs_url.lower():
                level_key = "C1_C2"
                
            return {
                "$ref": f"#/master_inventories/{SECCION_A_SLUG[base_sec]}/{level_key}/keywords",
                "target_section": target_sec
            }

    base_sec = ".".join(target_sec.split(".")[:2])
    if base_sec in SECCION_A_SLUG:
        return f"master_{SECCION_A_SLUG[base_sec]}"

    # ── Global Cache Lookup ──
    cache_key = (abs_url.split('#')[0], fragment, target_sec)
    if cache_key in session._global_parsed_cache:
        return copy.deepcopy(session._global_parsed_cache[cache_key])

    # Cycle breaker fallback
    target_key = (abs_url.split('#')[0], fragment, link_text.strip())
    if target_key in session._active_crawl_path or len(session._active_crawl_path) > 8:
        return {}

    session._active_crawl_path.append(target_key)

    try:
        try:
            soup = get_soup(session, abs_url)
        except Exception as e:
            print(f"    ⚠  fetch failed: {e}")
            return {}

        result_data = {}

        # Strategy 1: URL Element Identifier
        if fragment:
            el = soup.find(id=fragment) or soup.find(attrs={"name": fragment})
            if el is not None:
                if el.name == "table":
                    res = read_table_by_fase(el, session, abs_url, depth=depth)
                    result_data = filter_table_by_section(res, target_sec)
                else:
                    next_table = el.find_next("table")
                    if next_table:
                        res = read_table_by_fase(next_table, session, abs_url, depth=depth)
                        result_data = filter_table_by_section(res, target_sec)

        # Strategy 2: Caption / Heading Search
        if not result_data and target_sec:
            base_sec = ".".join(target_sec.split(".")[:2]) if len(target_sec.split(".")) > 2 else target_sec
            for cap in soup.find_all("caption"):
                if base_sec in cap.get_text():
                    tbl = cap.find_parent("table")
                    if tbl:
                        res = read_table_by_fase(tbl, session, abs_url, depth=depth)
                        result_data = filter_table_by_section(res, target_sec)
                        break

            if not result_data:
                for h in soup.find_all(["h2", "h3", "h4", "h5"]):
                    if target_sec in h.get_text():
                        next_table = h.find_next("table")
                        if next_table:
                            res = read_table_by_fase(next_table, session, abs_url, depth=depth)
                            result_data = filter_table_by_section(res, target_sec)
                        else:
                            result_data = {"all": extract_keywords(h)}
                        break

        # Strategy 3: Standard Page Fallback
        if not result_data:
            first_table = soup.find("table")
            if first_table:
                res = read_table_by_fase(first_table, session, abs_url, depth=depth)
                result_data = filter_table_by_section(res, target_sec)
            else:
                h = soup.find("h3") or soup.find("h2")
                result_data = {"all": extract_keywords(h)} if h else {}

        # Determine cross-reference section code string
        sec_code = target_sec if target_sec else (fragment if fragment else "all")
        category = classify_href(abs_url, link_text)#

        level_suffix = ""
        if "a1-a2" in abs_url.lower():
            level_suffix = "_a1_a2"
        elif "b1-b2" in abs_url.lower():
            level_suffix = "_b1_b2"
        elif "c1-c2" in abs_url.lower():
            level_suffix = "_c1_c2"

        # Create a unique database pointer ID
        ref_id = f"{category}_{sec_code}{level_suffix}".replace(".", "_").lower()
        
        # Save to global cache
        session._global_parsed_cache[cache_key] = ref_id
        
        # If it doesn't exist in the database registry yet, add it cleanly
        if ref_id not in GLOBAL_REGISTRY and result_data:
            GLOBAL_REGISTRY[ref_id] = result_data
            
        return ref_id
    finally:
        session._active_crawl_path.pop()
 
# ── Main cross-reference extractor ───────────────────────────────────────────
 
def EMPTY_REFS():
    return {
        "saberes_y_comportamientos": {},
        "nociones_generales":        {},   # ← NEW
        "nociones_especificas":      {},
        "referentes_culturales":     {},
    }
 
 
def merge_into(target: dict, content: any):
    """Accumulates section pointer strings under their respective structural phases."""
    if not content:
        return
        
    # Standard JSON Pointer Reference interception
    if isinstance(content, dict) and "$ref" in content:
        target.setdefault("all", [])
        if content not in target["all"]:
            target["all"].append(content)
        return
        
    if isinstance(content, str):
        # Top level loose text identifier fallback
        target.setdefault("all", [])
        if content not in target["all"]:
            target["all"].append(content)
        return

    if isinstance(content, dict):
        for fase_name, items in content.items():
            target.setdefault(fase_name, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str) and item not in target[fase_name]:
                        target[fase_name].append(item)
            elif isinstance(items, str) and items not in target[fase_name]:
                target[fase_name].append(items)
 
 
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
        category = classify_href(href, link_text)
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
GLOBAL_REGISTRY: dict = {}
SECCION_A_SLUG: dict = {}

# Run a quick pre-scan to map the headers across all index pages
for level, url in URLS.items():
    try:
        r_pre = session.get(url, headers=HEADERS, timeout=15)
        r_pre.encoding = "utf-8"
        soup_pre = BeautifulSoup(r_pre.text, "html.parser")
        for h3 in soup_pre.find_all("h3"):
            raw_text = h3.get_text(strip=True)
            slug_text = make_slug(raw_text)
            m_sec = re.match(r'^(\d+(?:\.\d+)*)', raw_text)
            if m_sec:
                num_sec = m_sec.group(1)
                SECCION_A_SLUG[num_sec] = slug_text
    except Exception:
        pass
 
SAMPLE = False   # ← set False for full production run

# ── Pasada previa para construir el mapa de secciones ─────────────────────────
for level, url in URLS.items():
    try:
        res_pre = session.get(url, headers=HEADERS, timeout=15)
        soup_pre = BeautifulSoup(res_pre.text, "html.parser")
        for h3 in soup_pre.find_all("h3"):
            raw_text = h3.get_text(strip=True)
            slug_text = make_slug(raw_text)
            # Extrae el patrón numérico inicial (ej: "3.1.8")
            match_sec = re.match(r'^(\d+(?:\.\d+)*)', raw_text)
            if match_sec:
                num_sec = match_sec.group(1)
                SECCION_A_SLUG[num_sec] = slug_text
    except Exception:
        pass
 
for level, url in URLS.items():
    if SAMPLE and level != "A1_A2":
        continue
 
    try:
        response = session.get(url, headers=HEADERS, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        print(f"    ⚠ Main index connection dropped for {level}. Waiting 5 seconds before retry...")
        time.sleep(5)
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
final_bundle = {
    "master_inventories": master,
    "global_registry": GLOBAL_REGISTRY
}

with open(output_path, "w", encoding="utf-8") as f:
    json.dump(final_bundle, f, ensure_ascii=False, indent=2)

print(f"\n✅  Saved {len(master)} sections → {output_path}")

# Quick preview of first entry
first_key = next(iter(master))
entry = master[first_key]["A1_A2"]
print(f"\nFirst key : {first_key}")
print(f"keywords  : {entry['keywords'][:5]}")
print(f"saberes   : {list(entry['official_references']['saberes_y_comportamientos'].items())[:3]}")
print(f"nociones  : {list(entry['official_references']['nociones_especificas'].items())[:3]}")