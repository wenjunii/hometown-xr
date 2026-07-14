"""Hardware benchmark, FP16 drift audit, and local profile autotuning."""

from __future__ import annotations

import gc
import json
import multiprocessing
import os
import platform
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

from config import (
    EVALUATION_DIR,
    HARDWARE_OVERRIDE_PATH,
    SEMANTIC_THRESHOLD,
    get_hardware_profile,
)

_CPU_TEXT = (
    "I remember the old family home where we grew up, the rooms, the garden, "
    "and the feeling of returning to my hometown after many years away."
)
_DRIFT_TEXTS = [
    _CPU_TEXT,
    "After migration, my idea of home became a memory carried between countries.",
    "The apartment was only temporary and never gave our family a sense of belonging.",
    "Our neighborhood changed, but the kitchen still reminds me of childhood.",
    "This page lists property prices, floor plans, and contact details for buyers.",
    "I left my village for work and returned decades later to a place I barely knew.",
    "Home is not a building for me; it is where my language and community are understood.",
    "Read the privacy policy, accept cookies, and subscribe to the newsletter.",
]


def _cpu_keyword_task(iterations: int) -> int:
    from matcher import KeywordMatcher

    matcher = KeywordMatcher()
    matches = 0
    for _ in range(iterations):
        matches += bool(matcher.find_matches(_CPU_TEXT))
    return matches


def _cpu_benchmark(quick: bool) -> list[dict]:
    cpu_count = os.cpu_count() or 1
    worker_counts = [count for count in (1, 2, 4, 7) if count <= cpu_count]
    iterations_per_worker = 2_000 if quick else 10_000
    context = multiprocessing.get_context("spawn")
    results = []
    for workers in worker_counts:
        started = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
            total_matches = sum(
                executor.map(_cpu_keyword_task, [iterations_per_worker] * workers)
            )
        elapsed = time.perf_counter() - started
        results.append(
            {
                "workers": workers,
                "paragraphs": iterations_per_worker * workers,
                "matches": total_matches,
                "seconds": round(elapsed, 4),
                "paragraphs_per_second": round(iterations_per_worker * workers / elapsed, 2),
            }
        )
    return results


def _drift_texts(quick: bool) -> tuple[list[str], str]:
    annotation_path = EVALUATION_DIR / "annotations.jsonl"
    limit = 32 if quick else 128
    if annotation_path.exists():
        rows = []
        with annotation_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    if row.get("paragraph"):
                        rows.append(row)
        rows.sort(key=lambda row: str(row.get("sample_id", "")))
        if rows:
            return [str(row["paragraph"]) for row in rows[:limit]], "annotation_corpus"
    return _DRIFT_TEXTS, "fixed_fallback"


def _precision_run(
    precision: str,
    batch_sizes: list[int],
    calibration_texts: list[str],
) -> tuple[list[dict], list]:
    import torch

    from matcher import SemanticMatcher

    matcher = SemanticMatcher(
        encoding_batch_size=max(batch_sizes),
        precision=precision,
        adaptive_batching=False,
    )
    calibration = matcher.score_paragraphs(calibration_texts)
    matcher.score_paragraphs([_CPU_TEXT] * 8)
    results = []
    for batch_size in batch_sizes:
        matcher.encoding_batch_size = batch_size
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        try:
            matcher.score_paragraphs([_CPU_TEXT] * batch_size)
            torch.cuda.synchronize()
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            results.append({"batch_size": batch_size, "status": "out_of_memory"})
            torch.cuda.empty_cache()
            break
        elapsed = time.perf_counter() - started
        results.append(
            {
                "batch_size": batch_size,
                "status": "ok",
                "seconds": round(elapsed, 4),
                "paragraphs_per_second": round(batch_size / elapsed, 2),
                "peak_vram_mb": round(torch.cuda.max_memory_allocated() / 1024**2, 1),
            }
        )
    del matcher
    gc.collect()
    torch.cuda.empty_cache()
    return results, calibration


def _gpu_benchmark(quick: bool) -> tuple[str, dict]:
    import torch

    if not torch.cuda.is_available():
        return "CPU", {"precision_runs": {}, "fp16_drift": None}

    gpu_name = torch.cuda.get_device_name(0)
    batch_sizes = [64, 128] if quick else [64, 128, 256, 512]
    calibration_texts, drift_source = _drift_texts(quick)
    fp32_results, fp32_scores = _precision_run(
        "fp32",
        batch_sizes,
        calibration_texts,
    )
    fp16_error = None
    try:
        fp16_results, fp16_scores = _precision_run(
            "fp16",
            batch_sizes,
            calibration_texts,
        )
    except RuntimeError as exc:
        fp16_results = []
        fp16_scores = []
        fp16_error = f"{type(exc).__name__}: {exc}"

    drift = None
    if fp16_scores:
        differences = [
            abs(fp32_score - fp16_score)
            for (fp32_score, _fp32_concept), (fp16_score, _fp16_concept) in zip(
                fp32_scores,
                fp16_scores,
            )
        ]
        concept_agreement = sum(
            fp32_concept == fp16_concept
            for (_fp32_score, fp32_concept), (_fp16_score, fp16_concept) in zip(
                fp32_scores,
                fp16_scores,
            )
        ) / len(fp32_scores)
        threshold_agreement = sum(
            (fp32_score >= SEMANTIC_THRESHOLD) == (fp16_score >= SEMANTIC_THRESHOLD)
            for (fp32_score, _fp32_concept), (fp16_score, _fp16_concept) in zip(
                fp32_scores,
                fp16_scores,
            )
        ) / len(fp32_scores)
        drift = {
            "samples": len(differences),
            "source": drift_source,
            "mean_absolute_score_drift": round(sum(differences) / len(differences), 6),
            "max_absolute_score_drift": round(max(differences), 6),
            "concept_agreement": round(concept_agreement, 4),
            "threshold_decision_agreement": round(threshold_agreement, 4),
            "safe_for_profile": (
                max(differences) <= 0.005
                and concept_agreement >= 0.99
                and threshold_agreement == 1.0
            ),
        }
    return gpu_name, {
        "precision_runs": {"fp32": fp32_results, "fp16": fp16_results},
        "fp16_drift": drift,
        "fp16_error": fp16_error,
    }


def _best_gpu_run(results: list[dict]) -> dict | None:
    successful = [item for item in results if item.get("status") == "ok"]
    return (
        max(successful, key=lambda item: item["paragraphs_per_second"])
        if successful
        else None
    )


def run_benchmark(profile_name: str = "auto", quick: bool = False, write: bool = True) -> dict:
    profile = get_hardware_profile(profile_name)
    cpu_results = _cpu_benchmark(quick)
    gpu_name, gpu_results = _gpu_benchmark(quick)
    best_cpu = max(cpu_results, key=lambda item: item["paragraphs_per_second"])
    best_fp32 = _best_gpu_run(gpu_results["precision_runs"].get("fp32", []))
    best_fp16 = _best_gpu_run(gpu_results["precision_runs"].get("fp16", []))
    fp16_safe = bool((gpu_results.get("fp16_drift") or {}).get("safe_for_profile"))
    use_fp16 = bool(
        fp16_safe
        and best_fp16
        and (
            best_fp32 is None
            or best_fp16["paragraphs_per_second"]
            >= best_fp32["paragraphs_per_second"] * 1.05
        )
    )
    precision = "fp16" if use_fp16 else "fp32"
    best_gpu = best_fp16 if use_fp16 else best_fp32
    recommendation = {
        "profile": profile.name,
        "workers": int(best_cpu["workers"]),
        "candidate_batch_size": profile.candidate_batch_size,
        "inference_batch_size": (
            max(profile.inference_batch_size, int(best_gpu["batch_size"]) * 4)
            if best_gpu
            else profile.inference_batch_size
        ),
        "encoding_batch_size": (
            int(best_gpu["batch_size"]) if best_gpu else profile.encoding_batch_size
        ),
        "precision": precision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gpu": gpu_name,
        "host": platform.node(),
    }
    payload = {
        "schema_version": 2,
        "quick": quick,
        "cpu": cpu_results,
        "gpu": {"name": gpu_name, **gpu_results},
        "recommendation": recommendation,
    }
    if write:
        HARDWARE_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = HARDWARE_OVERRIDE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(recommendation, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, HARDWARE_OVERRIDE_PATH)
    return payload
