"""
directory -- built-in lookups for search suggestions. Nothing here calls an outside service.

  places.tsv    ~57,000 places: every US city/town with 1,000+ people (as "Garland, TX"), the 50
                states, and cities of 5,000+ elsewhere (as "Bhopal, India"). One line per place:
                    <search key> TAB <display name> TAB <population>
                sorted by search key, so a prefix is found by binary search straight on the file
                (memory-mapped: the list is never loaded into RAM, which matters on a small server).
                Built from GeoNames data (geonames.org, CC BY 4.0).
  services.txt  business types / search terms, one per line. Edit freely.
"""
import mmap
import os
import re
import threading
import unicodedata

_DIR = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()
_places = None          # (mmap, size) once opened; False if the file is missing
_services = None

MAX_SCAN = 4000         # lines looked at per lookup ("san" alone matches thousands of places)


def fold(text):
    """'São Paulo, Brazil' -> 'sao paulo brazil': lowercase, no accents, no punctuation."""
    t = unicodedata.normalize("NFD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _open_places():
    global _places
    with _lock:
        if _places is None:
            try:
                f = open(os.path.join(_DIR, "places.tsv"), "rb")
                size = os.fstat(f.fileno()).st_size
                _places = (mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ), size) if size else False
            except (OSError, ValueError):
                _places = False
    return _places


def _line_start(mm, pos):
    return mm.rfind(b"\n", 0, pos) + 1 if pos > 0 else 0


def places(q, limit=8, prefer=()):
    """Places whose name starts with `q`, biggest first. `prefer` is a set of endings such as
    {'TX', 'India'}: places ending in one of them (where this account already works) rank first."""
    key = fold(q).encode()
    opened = _open_places()
    if not key or not opened:
        return []
    mm, size = opened
    lo, hi = 0, size
    while lo < hi:                                  # first line whose key is >= what was typed
        mid = _line_start(mm, (lo + hi) // 2)
        end = mm.find(b"\n", mid)
        end = size if end < 0 else end
        if mm[mid:end].split(b"\t", 1)[0] < key:
            lo = end + 1
        else:
            hi = mid
    prefer = {p.lower() for p in prefer if p}
    found, pos, scanned = [], lo, 0
    while pos < size and scanned < MAX_SCAN:
        end = mm.find(b"\n", pos)
        end = size if end < 0 else end
        parts = mm[pos:end].split(b"\t")
        pos, scanned = end + 1, scanned + 1
        if not parts[0].startswith(key):
            break
        if len(parts) < 3:
            continue
        display = parts[1].decode("utf-8", "replace")
        try:
            pop = int(parts[2])
        except ValueError:
            pop = 0
        ending = display.rsplit(", ", 1)[-1]
        if ending.lower() in prefer:
            tier = 0                                # where this account already works
        elif ", " not in display or (len(ending) == 2 and ending.isupper()):
            tier = 1                                # US states and US places
        else:
            tier = 2
        rank = (tier, -pop)
        found.append((rank, display))
    found.sort()
    return [d for _, d in found[:limit]]


def _load_services():
    global _services
    with _lock:
        if _services is None:
            try:
                with open(os.path.join(_DIR, "services.txt"), encoding="utf-8") as f:
                    rows = [ln.strip() for ln in f]
                _services = [(fold(r), r) for r in rows if r and not r.startswith("#")]
            except OSError:
                _services = []
    return _services


def services(q, limit=8):
    """Business types matching `q`: ones that start with it, then ones where a later word
    starts with it ('rep' -> 'AC repair'), then anything containing it."""
    key = fold(q)
    if not key:
        return []
    starts, word, inside = [], [], []
    for folded, display in _load_services():
        if folded.startswith(key):
            starts.append(display)
        elif (" " + key) in folded:
            word.append(display)
        elif key in folded:
            inside.append(display)
    return (starts + word + inside)[:limit]
