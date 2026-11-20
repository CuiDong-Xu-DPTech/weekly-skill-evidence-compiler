#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

from contract import dump_json, load_json


def main():
    parser = argparse.ArgumentParser(description="Read-only selective retrieval from actual analysis facts")
    parser.add_argument("fact_file", default="/app/work/analysis.json", nargs="?")
    parser.add_argument("--metric")
    parser.add_argument("--kind")
    parser.add_argument("--sheet")
    parser.add_argument("--entity")
    parser.add_argument("--ids", help="Comma-separated exact fact ids")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--out")
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000 or args.skip < 0:
        raise SystemExit("--limit must be 1..1000 and --skip nonnegative")
    data = load_json(Path(args.fact_file))
    ids = {item for item in (args.ids or "").split(",") if item}
    selected = []
    for fact in data.get("facts", []):
        if ids and fact.get("id") not in ids:
            continue
        if args.kind and fact.get("kind") != args.kind:
            continue
        if args.metric and args.metric.casefold() not in str(fact.get("metric", "")).casefold():
            continue
        if args.sheet and args.sheet.casefold() != str(fact.get("entity", {}).get("sheet", "")).casefold():
            continue
        if args.entity and args.entity.casefold() not in json.dumps(fact.get("entity", {}), ensure_ascii=False).casefold():
            continue
        selected.append(fact)
    total = len(selected)
    selected = selected[args.skip:args.skip + args.limit]
    output = {"candidate_facts": total, "returned_facts": len(selected), "skip": args.skip,
              "truncated": total > args.skip + len(selected), "facts": selected}
    text = json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.out:
        dump_json(Path(args.out), output)
        print(json.dumps({"written": args.out, "candidate_facts": total}))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
