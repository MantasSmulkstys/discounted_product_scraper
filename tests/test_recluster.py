"""Tests for the global family-reclustering helpers that don't touch the
network or the database. The OpenAI call and the DB write-back are excluded;
everything around them (deduping, batching, parsing, vocabulary export) is
covered here since that is where logic regressions land.
"""

from __future__ import annotations

from scripts.recluster_families import (
    build_user_payload,
    build_vocabulary,
    cluster_names,
    dedupe_names,
    display_name,
    parse_assignments,
)


def test_display_name_prefers_raw_source_name():
    assert display_name({"source_name": "RAW name 1 kg", "canonical_name": "Clean"}) == "RAW name 1 kg"
    assert display_name({"source_name": None, "canonical_name": "Clean"}) == "Clean"
    assert display_name({"source_name": "   ", "canonical_name": ""}) == ""


def test_dedupe_names_collapses_identical_strings_and_maps_ids():
    products = [
        {"id": 1, "source_name": "Vanduo gazuotas 1 l"},
        {"id": 2, "source_name": "Vanduo gazuotas 1 l"},
        {"id": 3, "source_name": "Sviestas 82%"},
        {"id": 4, "source_name": "   "},
    ]
    names, name_to_ids = dedupe_names(products)

    assert names == ["Vanduo gazuotas 1 l", "Sviestas 82%"]
    assert name_to_ids["Vanduo gazuotas 1 l"] == [1, 2]
    assert name_to_ids["Sviestas 82%"] == [3]


def test_build_user_payload_numbers_items_and_lists_existing_keys():
    payload = build_user_payload(["Vanduo gazuotas", "Sviestas"], ["vanduo"])

    assert "ALREADY-USED KEYS" in payload
    assert "vanduo" in payload
    assert "0\tVanduo gazuotas" in payload
    assert "1\tSviestas" in payload


def test_parse_assignments_maps_index_to_name_and_normalizes_key():
    names = ["Vanduo gazuotas 1 l", "Sviestas 82%"]
    parsed = {
        "assignments": [
            {"index": 0, "family_key": "Vanduo"},
            {"index": 1, "family_key": "sviestas"},
            {"index": 9, "family_key": "ignored-out-of-range"},
        ]
    }
    result = parse_assignments(parsed, names)

    assert result == {"Vanduo gazuotas 1 l": "vanduo", "Sviestas 82%": "sviestas"}


def test_cluster_names_batches_and_carries_vocabulary_forward():
    seen_existing_keys: list[list[str]] = []

    def fake_call(system: str, user: str) -> dict:
        # Record which already-used keys each batch was told about.
        keys_line = ""
        lines = user.splitlines()
        if lines and lines[0].startswith("ALREADY-USED"):
            keys_line = lines[1]
        seen_existing_keys.append([k for k in keys_line.split(", ") if k])

        assignments = []
        for line in lines:
            if "\t" in line and line.split("\t", 1)[0].isdigit():
                idx = int(line.split("\t", 1)[0])
                name = line.split("\t", 1)[1]
                key = "vanduo" if "vanduo" in name.lower() else "sviestas"
                assignments.append({"index": idx, "family_key": key})
        return {"assignments": assignments}

    names = ["Vanduo A", "Vanduo B", "Sviestas C"]
    result = cluster_names(fake_call, names, batch_size=2)

    assert result == {"Vanduo A": "vanduo", "Vanduo B": "vanduo", "Sviestas C": "sviestas"}
    # First batch starts with no vocabulary; the second batch is told about "vanduo".
    assert seen_existing_keys[0] == []
    assert "vanduo" in seen_existing_keys[1]


def test_build_vocabulary_groups_names_by_key():
    name_to_key = {
        "Vanduo gazuotas": "vanduo",
        "Mineralinis vanduo": "vanduo",
        "Sviestas 82%": "sviestas",
    }
    vocab = build_vocabulary(name_to_key)

    assert vocab["family_keys"] == ["sviestas", "vanduo"]
    assert sorted(vocab["examples"]["vanduo"]) == ["Mineralinis vanduo", "Vanduo gazuotas"]
    assert vocab["name_to_key"] == name_to_key
