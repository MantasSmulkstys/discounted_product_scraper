#!/usr/bin/env python3
"""Global family reclustering.

Per-item normalization assigns ``family_key`` to each offer in isolation, so the
same product drifts apart across stores (``gazuotas vanduo`` vs ``vanduo`` vs
``mineralinis vanduo``). This script fixes that by sending the WHOLE catalog of
raw product names to one strong model in a single (batched) request, letting it
build an internally-consistent ``family_key`` for every product, then writing
those keys back by repointing each ``normalized_product`` to the right
``product_families`` row.

It also exports the learned vocabulary to a JSON file so the cheaper per-item
model can later be constrained to these keys (an ``allowed_family_keys`` list).

Dry-run by default — it only prints the proposed clusters and writes the
vocabulary file. Pass ``--apply`` to update the database.

    python -m scripts.recluster_families                 # preview + write vocab file
    python -m scripts.recluster_families --apply         # also update the DB
    python -m scripts.recluster_families --model gpt-5.5  # pick the model
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402
from normalization.ai_normalize import normalize_identity_text  # noqa: E402

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# A known-good default. The gpt-5.x ids are passed via --model once verified.
DEFAULT_MODEL = os.getenv("RECLUSTER_MODEL", "gpt-4.1-mini")
DEFAULT_OUTPUT = PROJECT_ROOT / "family_vocabulary.json"

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "index": {"type": "integer"},
                    "family_key": {"type": "string"},
                },
                "required": ["index", "family_key"],
            },
        }
    },
    "required": ["assignments"],
}

SYSTEM_INSTRUCTION = (
    "# ROLE\n"
    "You cluster Lithuanian grocery product names for cross-store comparison.\n"
    "You see the WHOLE catalog at once. Assign every item a `family_key` so that\n"
    "the SAME product type sold under different names/brands/sizes in different\n"
    "stores gets the SAME key. Be globally consistent — reuse one key everywhere.\n"
    "\n"
    "# family_key FORMAT (all must hold)\n"
    "- 1-3 words, lowercase, Lithuanian, NO diacritics (ė->e, š->s, ų->u, ...)\n"
    "- nominative plural; NO numbers, %, weights, volumes, brands, packaging\n"
    "- it is the HEAD NOUN of the product, never a genitive modifier:\n"
    "    `bulviu traskuciai` -> `traskuciai` (a snack, NOT `bulves`)\n"
    "    `bulviu cepelinai`  -> `cepelinai`  (a dish, NOT `bulves`)\n"
    "    `saulegrazu aliejus` -> `aliejus`\n"
    "\n"
    "# WHAT SPLITS vs MERGES\n"
    "- MERGE: fat % (sviestas 82% == sviestas 90%), brand, size, freshness when\n"
    "  fresh is the default state, declension variants.\n"
    "- SPLIT: lactose status (`pienas` vs `pienas be laktozes`), frozen vs fresh\n"
    "  (`avietes` vs `saldytos avietes`), variety colour for wine/grapes\n"
    "  (`raudonas vynas` vs `baltas vynas`).\n"
    "\n"
    "# PREFERRED KEYS\n"
    "When an item fits one of the already-used keys provided by the user, REUSE it\n"
    "verbatim instead of inventing a near-duplicate.\n"
    "\n"
    "# OUTPUT\n"
    "Return `assignments`: one {index, family_key} per input line, same indexes."
)


def dedupe_names(products: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[int]]]:
    """Collapse identical raw names so we only pay for each distinct string once.

    Returns the ordered unique names and a map name -> [normalized_product ids].
    """
    name_to_ids: dict[str, list[int]] = {}
    for product in products:
        name = display_name(product)
        if name == "":
            continue
        name_to_ids.setdefault(name, []).append(int(product["id"]))
    return list(name_to_ids.keys()), name_to_ids


def display_name(product: dict[str, Any]) -> str:
    """The full raw offer name, falling back to the canonical name."""
    for key in ("source_name", "canonical_name"):
        value = product.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def build_user_payload(names: list[str], existing_keys: list[str]) -> str:
    lines = []
    if existing_keys:
        lines.append("ALREADY-USED KEYS (reuse when they fit):")
        lines.append(", ".join(sorted(existing_keys)))
        lines.append("")
    lines.append("ITEMS:")
    for index, name in enumerate(names):
        lines.append(f"{index}\t{name}")
    return "\n".join(lines)


def parse_assignments(parsed: dict[str, Any], names: list[str]) -> dict[str, str]:
    """Map each batch name to its normalized family_key from the model output."""
    out: dict[str, str] = {}
    for item in parsed.get("assignments", []):
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if index < 0 or index >= len(names):
            continue
        key = normalize_identity_text(str(item.get("family_key") or ""))
        if key:
            out[names[index]] = key
    return out


def build_vocabulary(name_to_key: dict[str, str], examples_per_key: int = 6) -> dict[str, Any]:
    by_key: dict[str, list[str]] = {}
    for name, key in name_to_key.items():
        by_key.setdefault(key, []).append(name)
    return {
        "family_keys": sorted(by_key.keys()),
        "examples": {key: sorted(names)[:examples_per_key] for key, names in by_key.items()},
        "name_to_key": name_to_key,
    }


def cluster_names(
    call_model: Callable[[str, str], dict[str, Any]],
    names: list[str],
    batch_size: int,
) -> dict[str, str]:
    """Cluster every name, carrying the growing key vocabulary across batches so
    later batches stay consistent with earlier ones.
    """
    name_to_key: dict[str, str] = {}
    existing_keys: set[str] = set()

    for start in range(0, len(names), batch_size):
        batch = names[start : start + batch_size]
        payload = build_user_payload(batch, sorted(existing_keys))
        parsed = call_model(SYSTEM_INSTRUCTION, payload)
        batch_map = parse_assignments(parsed, batch)
        name_to_key.update(batch_map)
        existing_keys.update(batch_map.values())
        logger.info(
            "Clustered %d/%d names (%d distinct keys so far)",
            min(start + batch_size, len(names)),
            len(names),
            len(existing_keys),
        )
    return name_to_key


def make_openai_caller(model: str, api_key: str) -> Callable[[str, str], dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    def call(system: str, user: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "family_clusters",
                            "schema": RESPONSE_SCHEMA,
                            "strict": True,
                        }
                    },
                )
                parsed = getattr(response, "output_parsed", None)
                if isinstance(parsed, dict):
                    return parsed
                text = getattr(response, "output_text", "") or ""
                return json.loads(text)
            except Exception as exc:  # noqa: BLE001 - retry transient issues
                last_error = exc
                logger.warning("Model call failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Model call failed after retries: {last_error}")

    return call


def _connect(config: DBConfig):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("This script requires psycopg. Install: pip install psycopg[binary]") from exc

    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )


def fetch_products(cur, limit: int | None) -> list[dict[str, Any]]:
    sql = "SELECT id, source_name, canonical_name, generic_name, family_id FROM normalized_products"
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)
    cur.execute(sql, params)
    columns = [col.name for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def find_or_create_family(cur, family_key: str) -> int | None:
    normalized = normalize_identity_text(family_key)
    if not normalized:
        return None

    cur.execute(
        "SELECT id FROM product_families WHERE normalized_family_name = %s LIMIT 1",
        (normalized,),
    )
    row = cur.fetchone()
    if row:
        return int(row[0])

    label = family_key.strip()
    label = label[0].upper() + label[1:] if label else label
    cur.execute(
        """
        INSERT INTO product_families (family_name, normalized_family_name, generic_name, created_at, updated_at)
        VALUES (%s, %s, %s, NOW(), NOW())
        RETURNING id
        """,
        (label, normalized, family_key),
    )
    return int(cur.fetchone()[0])


def prune_empty_families(cur) -> int:
    cur.execute(
        """
        DELETE FROM product_families pf
        WHERE NOT EXISTS (SELECT 1 FROM normalized_products np WHERE np.family_id = pf.id)
          AND NOT EXISTS (SELECT 1 FROM family_subscriptions fs WHERE fs.family_id = pf.id)
        """
    )
    return cur.rowcount or 0


def apply_keys(conn, products: list[dict[str, Any]], name_to_key: dict[str, str]) -> dict[str, int]:
    reassigned = 0
    family_cache: dict[str, int | None] = {}
    with conn.cursor() as cur:
        for product in products:
            key = name_to_key.get(display_name(product))
            if not key:
                continue
            if key not in family_cache:
                family_cache[key] = find_or_create_family(cur, key)
            family_id = family_cache[key]
            if family_id is None or product.get("family_id") == family_id:
                continue
            cur.execute(
                "UPDATE normalized_products SET family_id = %s, updated_at = NOW() WHERE id = %s",
                (family_id, int(product["id"])),
            )
            reassigned += cur.rowcount or 0

        pruned = prune_empty_families(cur)
    conn.commit()
    return {"reassigned": reassigned, "pruned": pruned}


def run(
    db_config: DBConfig,
    model: str,
    api_key: str | None,
    batch_size: int,
    limit: int | None,
    output_path: Path,
    apply: bool,
) -> int:
    conn = _connect(db_config)
    try:
        with conn.cursor() as cur:
            products = fetch_products(cur, limit)
    finally:
        if not apply:
            conn.close()

    names, _ = dedupe_names(products)
    logger.info("Loaded %d products, %d distinct names.", len(products), len(names))
    if not names:
        logger.info("Nothing to cluster.")
        return 0

    if not api_key:
        logger.error("OPENAI_API_KEY is required (pass --openai-api-key or set it in the env).")
        return 1

    caller = make_openai_caller(model, api_key)
    name_to_key = cluster_names(caller, names, batch_size)

    vocabulary = build_vocabulary(name_to_key)
    output_path.write_text(json.dumps(vocabulary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "Wrote %d distinct family keys for %d names to %s",
        len(vocabulary["family_keys"]),
        len(name_to_key),
        output_path,
    )

    if not apply:
        logger.info("Dry-run: database left unchanged. Re-run with --apply to write the keys.")
        return 0

    try:
        stats = apply_keys(conn, products, name_to_key)
    finally:
        conn.close()
    logger.info("Reassigned %d products; pruned %d empty families.", stats["reassigned"], stats["pruned"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Recluster product families with a single global model pass.")
    parser.add_argument("--apply", action="store_true", help="Write the new keys to the database.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model id (default: {DEFAULT_MODEL}).")
    parser.add_argument("--batch-size", type=int, default=200, help="Names per model request.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N products (testing).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Vocabulary JSON output path.")
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    args = parser.parse_args()

    return run(
        db_config=DBConfig(
            host=args.db_host,
            port=args.db_port,
            dbname=args.db_name,
            user=args.db_user,
            password=args.db_password,
        ),
        model=args.model,
        api_key=args.openai_api_key,
        batch_size=args.batch_size,
        limit=args.limit,
        output_path=args.output,
        apply=args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
