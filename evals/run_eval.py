#!/usr/bin/env python3
"""Run a prompt configuration against the labelled set and report against the
previous result.

One command, one number that matters: precision on `end_customer_company`. A
false positive there puts a hosting provider in a rep's call list and costs the
tool its credibility; a false negative removes a real company from the market
invisibly. Both are reported, weighted toward precision.

Results are keyed on (prompt_version, model), not version alone. A model swap
inside one prompt version silently contaminated an earlier comparison — the
configuration is the pair, not either half.

Usage:
    python run_eval.py                        # current default config
    python run_eval.py --prompt-version v2
    python run_eval.py --prompt-version v3 --model claude-haiku-4-5
    python run_eval.py --compare v2:claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "llm"))

from classify import Classifier  # noqa: E402
from tracing import TraceWriter, summarise  # noqa: E402

LABELLED = REPO / "evals" / "labelled_set.jsonl"
RESULTS_DIR = REPO / "evals" / "results"
TRACES = REPO / "data" / "traces" / "eval.jsonl"

CLASSES = [
    "end_customer_company",
    "hosting_or_cloud",
    "cdn_or_security_vendor",
    "isp_telco",
    "government_or_education",
    "unknown",
]
HEADLINE = "end_customer_company"
CONCURRENCY = 6


def load_labelled() -> list[dict]:
    if not LABELLED.exists():
        print(f"no labelled set at {LABELLED} — run build_labelled_set.py first")
        sys.exit(1)

    rows, unlabelled = [], 0
    for line in LABELLED.open(encoding="utf-8"):
        row = json.loads(line)
        if not row.get("true_class"):
            unlabelled += 1
            continue
        if row["true_class"] not in CLASSES:
            print(f"invalid label {row['true_class']!r} on {row['entity_domain']}")
            sys.exit(1)
        rows.append(row)

    if unlabelled:
        print(f"skipping {unlabelled} unlabelled rows")
    if not rows:
        print("no labelled rows — fill in `true_class`")
        sys.exit(1)
    return rows


def score(pairs: list[tuple[str, str]]) -> dict:
    """Per-class precision, recall and F1 from (truth, prediction) pairs.

    Computed directly rather than via a library: the whole point of the metric
    is that someone can check it, and six classes of arithmetic is cheaper to
    verify than a dependency.
    """
    per_class = {}
    for cls in CLASSES:
        tp = sum(1 for t, p in pairs if t == cls and p == cls)
        fp = sum(1 for t, p in pairs if t != cls and p == cls)
        fn = sum(1 for t, p in pairs if t == cls and p != cls)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else None)
        per_class[cls] = {"support": tp + fn, "tp": tp, "fp": fp, "fn": fn,
                          "precision": precision, "recall": recall, "f1": f1}

    return {
        "n": len(pairs),
        "accuracy": sum(1 for t, p in pairs if t == p) / len(pairs),
        "per_class": per_class,
    }


def run_config(rows: list[dict], args) -> dict:
    tracer = TraceWriter(TRACES)
    classifier = Classifier(args.prompt_version, tracer)
    if args.model:
        classifier.model = args.model

    started = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        outputs = list(pool.map(classifier.classify, rows))
    tracer.close()

    pairs, errors, details = [], 0, []
    for row, out in zip(rows, outputs):
        if not out:
            errors += 1
            continue
        pairs.append((row["true_class"], out["entity_class"]))
        details.append({
            "entity_domain": row["entity_domain"],
            "true_class": row["true_class"],
            "predicted": out["entity_class"],
            "confidence": out["confidence"],
            "correct": row["true_class"] == out["entity_class"],
            "model_reasoning": out["reasoning"],
            "labeller_note": row.get("labeller_note", ""),
        })

    result = score(pairs)
    result.update({
        "prompt_version": args.prompt_version,
        "model": classifier.model,
        "errors": errors,
        "elapsed_s": round(time.time() - started, 1),
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "details": details,
    })
    return result


def fmt(value) -> str:
    return "  --  " if value is None else f"{value:6.3f}"


def delta(now, before) -> str:
    if now is None or before is None:
        return ""
    diff = now - before
    if abs(diff) < 0.005:
        return "   ="
    return f" {diff:+.2f}"


def report(result: dict, previous: dict | None) -> None:
    print(f"\n{result['prompt_version']} on {result['model']}")
    print(f"{result['n']} labelled · {result['errors']} errors · "
          f"{result['elapsed_s']}s")
    if previous:
        print(f"compared against {previous['prompt_version']} on "
              f"{previous['model']} ({previous['run_at']})")

    prev_classes = (previous or {}).get("per_class", {})
    print(f"\n{'class':<26}{'n':>4}{'prec':>8}{'':>6}{'recall':>8}{'':>6}{'f1':>8}")
    for cls in CLASSES:
        m = result["per_class"][cls]
        p = prev_classes.get(cls, {})
        marker = " <<" if cls == HEADLINE else ""
        print(f"{cls:<26}{m['support']:>4}{fmt(m['precision'])}"
              f"{delta(m['precision'], p.get('precision')):>6}"
              f"{fmt(m['recall'])}{delta(m['recall'], p.get('recall')):>6}"
              f"{fmt(m['f1'])}{marker}")

    prev_acc = (previous or {}).get("accuracy")
    print(f"\naccuracy {result['accuracy']:.3f}{delta(result['accuracy'], prev_acc)}")

    head = result["per_class"][HEADLINE]
    print(f"\nHEADLINE — precision on {HEADLINE}: {fmt(head['precision']).strip()}")
    if head["fp"]:
        print(f"  {head['fp']} false positive(s): infrastructure that would "
              f"reach a rep's call list")
        for d in result["details"]:
            if d["predicted"] == HEADLINE and not d["correct"]:
                print(f"    {d['entity_domain']:<30} actually {d['true_class']} "
                      f"(conf {d['confidence']:.2f}) — {d['model_reasoning']}")
    if head["fn"]:
        print(f"  {head['fn']} false negative(s): real companies removed from "
              f"the market")
        for d in result["details"]:
            if d["true_class"] == HEADLINE and not d["correct"]:
                print(f"    {d['entity_domain']:<30} called {d['predicted']} "
                      f"(conf {d['confidence']:.2f}) — {d['model_reasoning']}")

    wrong = [d for d in result["details"] if not d["correct"]]
    if wrong:
        print(f"\nall {len(wrong)} disagreement(s):")
        for d in wrong:
            print(f"  {d['entity_domain']:<30} said {d['predicted']:<24} "
                  f"labelled {d['true_class']:<24} conf {d['confidence']:.2f}")


def previous_result(args) -> dict | None:
    """The run to compare against: an explicit --compare, else the most recent
    result from a different configuration."""
    if args.compare:
        version, _, model = args.compare.partition(":")
        path = RESULTS_DIR / f"{version}__{model.replace('/', '_')}.json"
        return json.loads(path.read_text()) if path.exists() else None

    current = f"{args.prompt_version}__{args.model or ''}"
    candidates = [p for p in RESULTS_DIR.glob("*.json")
                  if not p.stem.startswith(current)]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-version", default="v3")
    parser.add_argument("--model", default=None,
                        help="override the model in the prompt frontmatter")
    parser.add_argument("--compare", default=None,
                        help="explicit baseline, as version:model")
    args = parser.parse_args()

    rows = load_labelled()
    result = run_config(rows, args)
    previous = previous_result(args)
    report(result, previous)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{result['prompt_version']}__{result['model']}.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"\nsaved {out.relative_to(REPO)}")
    print(json.dumps(summarise(TRACES), indent=2))


if __name__ == "__main__":
    main()
