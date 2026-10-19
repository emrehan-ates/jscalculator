"""Dataset — seed examples and their versioned upload to Phoenix.

Seeds live in ``evals/seeds/*.jsonl``, one JSON object per line:

    {"input": "<user query>",
     "expected": {"route": "...", "intent": "...", "skill": [...], ...},
     "metadata": {"category": "..."}}

``expected`` only ever holds structural/behavioural ground truth. Live fabric
facts (counts, health scores, fault totals) change between runs and are never
recorded here.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

SEEDS_DIR = Path(__file__).parent / "seeds"
DEFAULT_SEED_FILES = ("routing", "skills", "adversarial")


def load_seeds(names=None) -> list[dict]:
    """Read seed files by stem name (default: all of them)."""
    examples: list[dict] = []
    for name in (names or DEFAULT_SEED_FILES):
        path = SEEDS_DIR / f"{Path(name).stem}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"seed file not found: {path}")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                example = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON — {e}") from e
            if not example.get("input"):
                raise ValueError(f"{path}:{lineno}: example has no 'input'")
            example.setdefault("expected", {})
            example.setdefault("metadata", {})
            example["metadata"].setdefault("category", path.stem)
            example["metadata"]["seed_file"] = path.stem
            examples.append(example)
    return examples


def _key(example: dict) -> str:
    """Stable identity of an example: its query text."""
    return " ".join(str(example["input"]).split()).lower()


def _example_input(item) -> dict:
    payload = item.get("input") if isinstance(item, dict) else getattr(item, "input", None)
    return payload if isinstance(payload, dict) else {}


def _existing_keys(dataset) -> set[str]:
    raw = getattr(dataset, "examples", None)
    items = raw.values() if isinstance(raw, dict) else (raw or [])
    keys = set()
    for item in items:
        query = _example_input(item).get("query")
        if query:
            keys.add(" ".join(str(query).split()).lower())
    return keys


def _get_dataset(client, name: str):
    datasets = getattr(client, "datasets", None)
    try:
        if datasets is not None:
            return datasets.get_dataset(dataset=name)
        return client.get_dataset(name=name)
    except Exception:
        return None


def sync_dataset(name: str | None = None, examples=None, client=None):
    """Upload the seeds to Phoenix, appending only what is new.

    Re-running with an unchanged seed set reuses the existing dataset instead
    of duplicating it; new seeds are appended as a new dataset version so past
    experiments stay comparable.
    """
    name = name or config.DEFAULT_DATASET_NAME
    examples = examples if examples is not None else load_seeds()
    client = client or config.phoenix_client()

    dataset = _get_dataset(client, name)
    if dataset is None:
        _upload(client, name, examples, append=False)
        return _get_dataset(client, name)

    known = _existing_keys(dataset)
    new = [ex for ex in examples if _key(ex) not in known]
    if not new:
        return dataset
    print(f"appending {len(new)} new seed(s) as a new version of '{name}'")
    _upload(client, name, new, append=True)
    return _get_dataset(client, name)


_DESCRIPTION = ("ACI agent regression seeds: routing, skill/tool selection and "
                "groundedness. Structural ground truth only.")


def _upload(client, name: str, examples: list[dict], append: bool):
    inputs = [{"query": ex["input"]} for ex in examples]
    outputs = [dict(ex.get("expected") or {}) for ex in examples]
    metadata = [dict(ex.get("metadata") or {}) for ex in examples]

    datasets = getattr(client, "datasets", None)
    if datasets is not None:
        upload = datasets.add_examples_to_dataset if append else datasets.create_dataset
    elif append:
        upload = getattr(client, "append_to_dataset", None)
        if upload is None:
            raise RuntimeError(
                f"Dataset '{name}' already exists and {len(examples)} seed(s) are new, "
                f"but this Phoenix client cannot append a version. Upgrade "
                f"arize-phoenix, or run with --dataset <new-name>.")
    else:
        upload = client.upload_dataset

    # Every alias both client generations use; the signature picks the real one.
    return config.call_with_supported_kwargs(
        upload,
        name=name,
        dataset_name=name,
        dataset=name,
        inputs=inputs,
        outputs=outputs,
        metadata=metadata,
        description=_DESCRIPTION,
        dataset_description=_DESCRIPTION,
    )


# --------------------------------------------------------- feedback seam ----
def append_curated_example(query: str, expected: dict, category: str,
                           source_span_id: str | None = None,
                           dataset_name: str | None = None):
    """Add one curated example to the eval dataset. INTERFACE ONLY (Step 6).

    This is the seam a 👎 on a Chainlit response will use later: the UI hands
    over the failing turn's query plus the expected behaviour a reviewer agreed
    on, and ``source_span_id`` links the example back to the trace it came
    from, so future curation reads real production queries out of Phoenix
    instead of re-typing them.

    Deliberately NOT wired to the UI yet, and deliberately not accepting an
    ``expected`` answer string: only structural expectations belong in the
    dataset.
    """
    if not query or not isinstance(expected, dict):
        raise ValueError("query and a structural 'expected' dict are required")

    example = {
        "input": query,
        "expected": expected,
        "metadata": {"category": category, "curated": True,
                     "source_span_id": source_span_id},
    }
    client = config.phoenix_client()
    return _upload(client, dataset_name or config.DEFAULT_DATASET_NAME, [example],
                   append=True)
