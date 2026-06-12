"""Download the unique-scan icon corpus from the vendored EE2 data.

Corpus = UNIQUE-namespace items (vendor/Ritual uniques) plus every tradeTagged
item (currency, omens, gems — Ritual rewards), deduped by icon URL: one PNG
per distinct ART, with all item names sharing that art recorded together
(e.g. the 20 uncut-gem levels collapse to one entry).

Writes ~/.cache/poe2-overlay/unique-icons/{NNNN}.png plus index.json:
  { "<art key NNNN>": {"file": "NNNN.png", "names": [...], "icon": url,
                       "w": int, "h": int, "kind": "unique"|"tagged"} }
Idempotent: existing files are skipped, so re-runs only fetch what's missing.
"""
import json
import os
import time
import urllib.request

DATA = os.path.join(
    os.path.dirname(__file__), "..", "brain", "vendor", "ee2", "public",
    "data", "en", "items.ndjson",
)
OUT = os.path.expanduser("~/.cache/poe2-overlay/unique-icons")
UA = "poe2-overlay/dev (unique-scan concept test)"


def fetch(url: str, path: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r, open(path, "wb") as f:
        f.write(r.read())


def collect() -> dict[str, dict]:
    """icon URL -> {names, w, h, kind}; uniques win the kind label on overlap."""
    arts: dict[str, dict] = {}
    with open(DATA) as f:
        for line in f:
            j = json.loads(line)
            is_unique = j.get("namespace") == "UNIQUE"
            # Some entries carry a "%NOT_FOUND%" placeholder instead of a URL.
            if not str(j.get("icon", "")).startswith("https://"):
                continue
            if not (is_unique or j.get("tradeTag")):
                continue
            name = j.get("refName") or j.get("name")
            art = arts.setdefault(
                j["icon"],
                {"names": [], "w": j.get("w"), "h": j.get("h"),
                 "kind": "unique" if is_unique else "tagged"},
            )
            if name not in art["names"]:
                art["names"].append(name)
            if is_unique:
                art["kind"] = "unique"
    return arts


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    arts = collect()
    index: dict[str, dict] = {}
    misses = 0
    for i, (url, meta) in enumerate(sorted(arts.items())):
        key = f"{i:04d}"
        path = os.path.join(OUT, f"{key}.png")
        if not os.path.exists(path):
            try:
                fetch(url, path)
                time.sleep(0.05)  # politeness
            except OSError as e:
                print(f"MISS {meta['names'][0]}: {e}")
                misses += 1
                continue
        index[key] = {"file": f"{key}.png", "icon": url, **meta}

    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump(index, f, indent=0)
    uniq = sum(1 for m in index.values() if m["kind"] == "unique")
    print(f"{len(index)} arts in {OUT} ({uniq} unique, "
          f"{len(index) - uniq} tagged, {misses} misses)")


if __name__ == "__main__":
    main()
