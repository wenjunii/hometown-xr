"""Pickle-safe runtime settings shared by the CLI and worker processes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeSettings:
    profile_name: str
    workers: int
    candidate_batch_size: int
    inference_batch_size: int
    encoding_batch_size: int
    semantic_threshold: float
    language_threshold: float
    precision: str = "fp32"
    adaptive_batching: bool = True
    cache_enabled: bool = True
    filter_signature: str = ""
    run_id: str = ""

    @property
    def stream_batch_size(self) -> int:
        return self.candidate_batch_size
