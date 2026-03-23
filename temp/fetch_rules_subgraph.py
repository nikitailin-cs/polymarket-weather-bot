"""
Bulk extraction des règles de résolution via endpoint GraphQL (Subgraph).

Objectif:
- récupérer beaucoup de marchés historiques
- extraire question + texte de règles + source de résolution
- exporter en JSON/CSV pour tests regex/LLM offline

Usage:
  python temp/fetch_rules_subgraph.py --endpoint "https://.../subgraph/.../gn"
  python temp/fetch_rules_subgraph.py --endpoint "https://.../subgraph/.../gn" --contains temperature
  python temp/fetch_rules_subgraph.py --endpoint "https://.../subgraph/.../gn" --max-records 5000 --out-prefix temp/subgraph_rules
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.request
from typing import Any


def gql(endpoint: str, query: str, variables: dict[str, Any]) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def first_non_empty(d: dict, keys: list[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def build_where(id_gt: str, contains: str) -> str:
    parts = [f'id_gt: "{id_gt}"']
    if contains:
        parts.append(f'question_contains_nocase: "{contains}"')
    return "{ " + ", ".join(parts) + " }"


def query_templates(where_clause: str, page_size: int) -> list[tuple[str, str]]:
    # Schémas courants : fixedProductMarketMakers / markets / marketDatas
    return [
        (
            "fixedProductMarketMakers",
            f"""
            {{
              fixedProductMarketMakers(first: {page_size}, orderBy: id, orderDirection: asc, where: {where_clause}) {{
                id
                question
                title
                resolutionRules
                resolution_rules
                description
                rules
                resolutionSource
                resolution_source
              }}
            }}
            """,
        ),
        (
            "markets",
            f"""
            {{
              markets(first: {page_size}, orderBy: id, orderDirection: asc, where: {where_clause}) {{
                id
                question
                title
                resolutionRules
                resolution_rules
                description
                rules
                resolutionSource
                resolution_source
              }}
            }}
            """,
        ),
        (
            "marketDatas",
            f"""
            {{
              marketDatas(first: {page_size}, orderBy: id, orderDirection: asc, where: {where_clause}) {{
                id
                question
                title
                description
                rules
                resolutionRules
                resolutionSource
                resolution_source
              }}
            }}
            """,
        ),
    ]


def fetch_bulk(endpoint: str, contains: str, page_size: int, max_records: int) -> list[dict]:
    out: list[dict] = []
    cursor = ""
    failures = 0

    while len(out) < max_records:
        where_clause = build_where(cursor, contains)
        batch = None
        last_error = ""

        for root_key, query in query_templates(where_clause, page_size):
            try:
                resp = gql(endpoint, query, {})
            except Exception as e:
                last_error = str(e)
                continue

            if resp.get("errors"):
                last_error = str(resp["errors"][0].get("message", "graphql error"))
                continue

            data = resp.get("data", {})
            rows = data.get(root_key)
            if isinstance(rows, list):
                batch = rows
                break

        if batch is None:
            failures += 1
            print(f"⚠️ page fail #{failures}: {last_error[:180]}")
            if failures >= 3:
                break
            time.sleep(1.0)
            continue

        if not batch:
            break

        failures = 0
        for row in batch:
            q = first_non_empty(row, ["question", "title"])
            rules = first_non_empty(
                row,
                ["resolutionRules", "resolution_rules", "description", "rules"],
            )
            source = first_non_empty(row, ["resolutionSource", "resolution_source"])
            out.append(
                {
                    "id": str(row.get("id", "")),
                    "question": q,
                    "rules_text": rules,
                    "resolution_source": source,
                }
            )
            if len(out) >= max_records:
                break

        cursor = str(batch[-1].get("id", cursor))
        print(f"  fetched={len(out)} cursor={cursor[:18]}...")
        if len(batch) < page_size:
            break
        time.sleep(0.15)

    return out


def write_outputs(records: list[dict], out_prefix: str) -> tuple[str, str]:
    json_path = out_prefix + ".json"
    csv_path = out_prefix + ".csv"

    out_dir = os.path.dirname(json_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "question", "rules_text", "resolution_source"])
        w.writeheader()
        for r in records:
            w.writerow(r)

    return json_path, csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk rules extraction from subgraph")
    parser.add_argument("--endpoint", required=True, help="Subgraph GraphQL endpoint URL")
    parser.add_argument("--contains", default="temperature", help="Filtre question_contains_nocase")
    parser.add_argument("--page-size", type=int, default=500, help="Taille page GraphQL")
    parser.add_argument("--max-records", type=int, default=5000, help="Nombre max d'enregistrements")
    parser.add_argument("--out-prefix", default="temp/subgraph_rules", help="Préfixe de sortie (sans extension)")
    args = parser.parse_args()

    print(f"Endpoint: {args.endpoint}")
    print(f"Filter contains: {args.contains}")
    print(f"Max records: {args.max_records}")
    records = fetch_bulk(
        endpoint=args.endpoint,
        contains=args.contains,
        page_size=args.page_size,
        max_records=args.max_records,
    )

    with_rules = sum(1 for r in records if r["rules_text"])
    with_source = sum(1 for r in records if r["resolution_source"])
    print(f"Done: {len(records)} records | rules_text={with_rules} | resolution_source={with_source}")
    jp, cp = write_outputs(records, args.out_prefix)
    print(f"JSON: {jp}")
    print(f"CSV : {cp}")


if __name__ == "__main__":
    main()
