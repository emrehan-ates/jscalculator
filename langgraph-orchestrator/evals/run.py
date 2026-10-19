"""Runner — versions the dataset, runs the experiment, prints a summary, gates.

    python -m evals.run                      # code evaluators only, no judge
    python -m evals.run --judge              # + LLM answer-quality judge
    python -m evals.run --self-test          # prove the evaluators catch failures

Exit code is non-zero when a critical evaluator falls below the pass threshold,
so this can run before a refactor or in CI.
"""
from __future__ import annotations

import argparse
import inspect
import sys
from collections import defaultdict

from . import config


# ------------------------------------------------------------ aggregation ----
def _instrument(fn, sink):
    """Wrap an evaluator so its result is also recorded locally.

    Aggregating from our own sink keeps the summary independent of whichever
    experiment-result schema the installed Phoenix version returns.
    """
    accepted = set(inspect.signature(fn).parameters)
    name = fn.__name__

    def wrapper(output, expected=None, input=None, metadata=None):
        available = {"output": output, "expected": expected,
                     "input": input, "metadata": metadata}
        result = fn(**{k: v for k, v in available.items() if k in accepted})
        sink.append((name, result))
        return result

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    wrapper.__doc__ = fn.__doc__
    return wrapper


def aggregate(sink):
    scores = defaultdict(list)
    labels = defaultdict(lambda: defaultdict(int))
    skipped = defaultdict(int)
    for name, result in sink:
        score = result.get("score") if isinstance(result, dict) else getattr(result, "score", None)
        label = (result.get("label") if isinstance(result, dict) else getattr(result, "label", None)) or "-"
        if score is None:
            skipped[name] += 1
        else:
            scores[name].append(float(score))
        labels[name][label] += 1

    summary = {}
    for name in sorted(set(list(scores) + list(skipped))):
        values = scores[name]
        summary[name] = {
            "n": len(values),
            "skipped": skipped[name],
            "mean": (sum(values) / len(values)) if values else None,
            "failures": sum(1 for v in values if v < 1.0),
            "labels": dict(labels[name]),
        }
    return summary


def print_summary(summary, threshold):
    print("\n" + "=" * 78)
    print(f"{'evaluator':<22}{'n':>4}{'skip':>6}{'mean':>8}{'fail':>6}  labels")
    print("-" * 78)
    for name, row in summary.items():
        mean = "  n/a" if row["mean"] is None else f"{row['mean']:.2f}"
        labels = ", ".join(f"{k}={v}" for k, v in sorted(row["labels"].items()))
        print(f"{name:<22}{row['n']:>4}{row['skipped']:>6}{mean:>8}{row['failures']:>6}  {labels}")
    print("=" * 78)
    print(f"gate: {list(config.CRITICAL_EVALUATORS)} must each be >= {threshold}")


def gate(summary, threshold) -> int:
    failed = []
    for name in config.CRITICAL_EVALUATORS:
        row = summary.get(name)
        if row is None or row["n"] == 0:
            failed.append(f"{name}: no example produced a score")
            continue
        if row["mean"] < threshold:
            failed.append(f"{name}: {row['mean']:.2f} < {threshold} "
                          f"({row['failures']}/{row['n']} failed)")
    if failed:
        print("\nFAILED:")
        for line in failed:
            print(f"  - {line}")
        return 1
    print("\nPASSED")
    return 0


# -------------------------------------------------------------- self test ----
def self_test() -> int:
    """Score fabricated outputs to prove the evaluators catch real failures."""
    from . import evaluators as ev

    checks = [
        ("routing_correct catches a wrong route", 0.0, "wrong_route",
         ev.routing_correct({"route": "create", "intent": "create_tenant"},
                            {"route": "agentic"})),
        ("numbers_grounded catches an invented number", 0.0, "hallucinated",
         ev.numbers_grounded(
             {"answer": "There are 4291 endpoints on the fabric.",
              "tool_outputs": [{"tool": "apic_get", "truncated": False,
                                "text": "APIC response: totalCount=7, objects_returned=7."}]},
             input={"query": "how many endpoints are there?"})),
        ("numbers_grounded passes a grounded number", 1.0, "grounded",
         ev.numbers_grounded(
             {"answer": "There are 7 endpoints on the fabric.",
              "tool_outputs": [{"tool": "apic_get", "truncated": False,
                                "text": "APIC response: totalCount=7, objects_returned=7."}]},
             input={"query": "how many endpoints are there?"})),
        ("numbers_grounded separates truncation from hallucination", 0.0, "unverifiable",
         ev.numbers_grounded(
             {"answer": "One of them is on node 4291.",
              "tool_outputs": [{"tool": "apic_get", "truncated": True,
                                "text": "NOTE: result too large for context"}]},
             input={"query": "which node?"})),
        ("no_unexpected_write catches an attempted write", 0.0, "write_attempted",
         ev.no_unexpected_write({"route": "agentic",
                                 "tool_calls": [{"tool": "apic_post", "executed": False}]},
                                {"route": "agentic"})),
        ("completed catches a timeout", 0.0, "failed",
         ev.completed({"error": "timeout after 300s"})),
        ("skill_correct catches the wrong skill", 0.0, "wrong_selection",
         ev.skill_correct({"route": "agentic", "skill": ["aci_endpoint_troubleshooting"],
                           "tool_calls": []},
                          {"skill": ["aci_inventory_validation"]})),
    ]

    ok = True
    for description, want_score, want_label, result in checks:
        got_score, got_label = result.get("score"), result.get("label")
        passed = got_score == want_score and got_label == want_label
        ok = ok and passed
        print(f"[{'ok' if passed else 'FAIL'}] {description} "
              f"-> score={got_score} label={got_label}")
        if not passed:
            print(f"       expected score={want_score} label={want_label}")
    print("\nself-test", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# ------------------------------------------------------------------- main ----
def _run_experiment(client, **kwargs):
    """Call run_experiment on whichever Phoenix generation is installed."""
    experiments = getattr(client, "experiments", None)
    if experiments is not None:
        runner = experiments.run_experiment
    else:
        from phoenix.experiments import run_experiment as runner

    return config.call_with_supported_kwargs(runner, **kwargs)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run", description=__doc__)
    parser.add_argument("--dataset", default=config.DEFAULT_DATASET_NAME,
                        help="Phoenix dataset name to version the seeds into")
    parser.add_argument("--seeds", nargs="*", default=None,
                        help="seed files to load (default: all)")
    parser.add_argument("--name", default=None, help="experiment name")
    parser.add_argument("--judge", action="store_true",
                        help="also run the LLM answer-quality judge (off by default)")
    parser.add_argument("--fabric", type=int, default=None,
                        help="FABRIC_<n>_* config to evaluate against")
    parser.add_argument("--threshold", type=float, default=config.PASS_THRESHOLD,
                        help="minimum mean score for every critical evaluator")
    parser.add_argument("--self-test", action="store_true",
                        help="check the evaluators against fabricated outputs and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    # Tracing must be registered before anything imports LangChain.
    config.init_tracing()

    from . import dataset as dataset_mod
    from . import evaluators as ev
    from .task import AgentRunner, build_task

    examples = dataset_mod.load_seeds(args.seeds)
    client = config.phoenix_client()
    dataset = dataset_mod.sync_dataset(args.dataset, examples, client=client)
    print(f"dataset '{args.dataset}': {len(examples)} seed example(s)")

    sink: list = []
    # A dict, NOT a list: Phoenix auto-generates names for a bare list of
    # evaluators, and every _instrument() closure shares the same underlying
    # code object (same nested `def wrapper` compiled once), which collided
    # under that auto-naming and silently dropped all but the last evaluator.
    evaluators = {fn.__name__: _instrument(fn, sink) for fn in ev.CODE_EVALUATORS}
    if args.judge:
        judge_cfg = config.judge_config()
        if not judge_cfg["base_url"] or not judge_cfg["model"]:
            print("--judge requested but no judge endpoint/model is configured; "
                  "continuing with code evaluators only")
        else:
            if judge_cfg["self_judging"]:
                print(f"WARNING: judge model '{judge_cfg['model']}' is the model under "
                      f"test — self-judging is a weaker signal. Set EVAL_JUDGE_MODEL "
                      f"to a different model for an independent score.")
            judge_fn = ev.build_answer_quality_judge(judge_cfg)
            evaluators[judge_fn.__name__] = _instrument(judge_fn, sink)

    fabric = config.fabric_from_env(args.fabric)
    print(f"fabric: {fabric['name']} (id={fabric['id']}), "
          f"concurrency={config.CONCURRENCY}, timeout={config.TASK_TIMEOUT_S}s")

    runner = AgentRunner(fabric, team=config.EVAL_TEAM, profile=config.EVAL_PROFILE,
                         timeout_s=config.TASK_TIMEOUT_S)
    runner.start()
    try:
        _run_experiment(
            client,
            dataset=dataset,
            task=build_task(runner),
            evaluators=evaluators,
            experiment_name=args.name,
            experiment_metadata={
                "fabric": fabric["name"],
                "judge": bool(args.judge),
                "seed_files": args.seeds or list(dataset_mod.DEFAULT_SEED_FILES),
            },
            concurrency=config.CONCURRENCY,
            timeout=config.TASK_TIMEOUT_S + 30,
        )
    finally:
        runner.close()

    summary = aggregate(sink)
    print_summary(summary, args.threshold)
    return gate(summary, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
