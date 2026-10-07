"""Copied to discovery workspaces: bounded identity lookup using standard-library Python."""

import json
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def lookup(query: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    key = unicodedata.normalize("NFKD", query).casefold()
    matches = []
    for record in records:
        names = [str(record["name"]), str(record.get("domain", "")), *record.get("aliases", [])]
        names = [str(name).casefold() for name in names]
        contains = any(key in name for name in names)
        score = 0.0
        for name in names:
            matcher = SequenceMatcher(None, key, name)
            # Substring hits retain their exact score/ranking even below 0.5.
            # Otherwise upper bounds can discard impossible fuzzy matches.
            if contains or (matcher.real_quick_ratio() >= 0.5 and matcher.quick_ratio() >= 0.5):
                score = max(score, matcher.ratio())
        if contains or score >= 0.5:
            matches.append({**record, "similarity": round(score, 4)})
    return sorted(matches, key=lambda value: float(value["similarity"]), reverse=True)[:10]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 lookup.py 'company name or domain'")
    data = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
    print(json.dumps(lookup(sys.argv[1], data), ensure_ascii=False))
