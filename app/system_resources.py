import platform
from dataclasses import dataclass
from pathlib import Path

import psutil

GIB = 1024**3


@dataclass(frozen=True)
class ResourceSnapshot:
    platform: str
    total_memory_bytes: int
    available_memory_bytes: int
    memory_percent: float
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    swap_percent: float | None
    disk_free_bytes: int

    def model_dump(self) -> dict:
        return {
            "platform": self.platform,
            "total_memory_gb": round(self.total_memory_bytes / GIB, 1),
            "available_memory_gb": round(self.available_memory_bytes / GIB, 1),
            "memory_percent": self.memory_percent,
            "swap_used_gb": (
                round(self.swap_used_bytes / GIB, 1)
                if self.swap_used_bytes is not None
                else None
            ),
            "swap_percent": self.swap_percent,
            "disk_free_gb": round(self.disk_free_bytes / GIB, 1),
        }


@dataclass(frozen=True)
class ModelFitAssessment:
    status: str
    model: str
    model_size_bytes: int
    estimated_runtime_bytes: int
    additional_memory_bytes: int
    currently_loaded: bool
    snapshot: ResourceSnapshot
    reason: str

    def model_dump(self) -> dict:
        return {
            "status": self.status,
            "model": self.model,
            "model_size_gb": round(self.model_size_bytes / GIB, 1),
            "estimated_runtime_gb": round(self.estimated_runtime_bytes / GIB, 1),
            "additional_memory_gb": round(self.additional_memory_bytes / GIB, 1),
            "currently_loaded": self.currently_loaded,
            "reason": self.reason,
            "resources": self.snapshot.model_dump(),
        }


def resource_snapshot(path: Path | None = None) -> ResourceSnapshot:
    memory = psutil.virtual_memory()
    try:
        swap = psutil.swap_memory()
    except (OSError, PermissionError, NotImplementedError):
        swap = None
    disk = psutil.disk_usage(str((path or Path.home()).expanduser().resolve()))
    return ResourceSnapshot(
        platform=platform.system() or "Unknown",
        total_memory_bytes=int(memory.total),
        available_memory_bytes=int(memory.available),
        memory_percent=round(float(memory.percent), 1),
        swap_total_bytes=int(swap.total) if swap is not None else None,
        swap_used_bytes=int(swap.used) if swap is not None else None,
        swap_percent=round(float(swap.percent), 1) if swap is not None else None,
        disk_free_bytes=int(disk.free),
    )


def assess_model_fit(
    *,
    model: str,
    model_size_bytes: int,
    context_length: int,
    memory_reserve_gb: float,
    memory_block_percent: float,
    swap_block_percent: float,
    currently_loaded: bool = False,
    snapshot: ResourceSnapshot | None = None,
) -> ModelFitAssessment:
    current = snapshot or resource_snapshot()
    context_headroom = max(
        3 * GIB,
        int((context_length / 16_384) * 2 * GIB),
        int(model_size_bytes * 0.12),
    )
    estimated = model_size_bytes + context_headroom
    additional = context_headroom if currently_loaded else estimated
    reserve = int(memory_reserve_gb * GIB)
    if estimated + reserve > current.total_memory_bytes:
        status = "block"
        reason = (
            "model estimate plus configured memory reserve exceeds total physical memory"
        )
    elif (
        current.memory_percent >= memory_block_percent
        and current.available_memory_bytes < additional
    ):
        status = "block"
        reason = "current memory pressure is above the configured safety ceiling"
    elif (
        current.swap_total_bytes
        and current.swap_percent is not None
        and current.swap_percent >= swap_block_percent
        and current.available_memory_bytes < additional
    ):
        status = "block"
        reason = "swap pressure is above the configured safety ceiling"
    elif not currently_loaded and current.available_memory_bytes < model_size_bytes:
        status = "block"
        reason = "available memory is below the model weight size after unloading"
    elif current.available_memory_bytes < additional + reserve:
        status = "warning"
        reason = (
            "model should load, but current available memory is below the preferred "
            "runtime-plus-reserve target"
        )
    else:
        status = "ok"
        reason = "model fits the current memory and configured reserve"
    return ModelFitAssessment(
        status=status,
        model=model,
        model_size_bytes=model_size_bytes,
        estimated_runtime_bytes=estimated,
        additional_memory_bytes=additional,
        currently_loaded=currently_loaded,
        snapshot=current,
        reason=reason,
    )


def assess_model_download(
    *,
    model: str,
    expected_size_bytes: int,
    snapshot: ResourceSnapshot | None = None,
) -> ModelFitAssessment:
    current = snapshot or resource_snapshot()
    required = int(expected_size_bytes * 1.15)
    status = "ok" if current.disk_free_bytes >= required else "block"
    return ModelFitAssessment(
        status=status,
        model=model,
        model_size_bytes=expected_size_bytes,
        estimated_runtime_bytes=required,
        additional_memory_bytes=required,
        currently_loaded=False,
        snapshot=current,
        reason=(
            "download fits available disk space"
            if status == "ok"
            else "download plus verification headroom exceeds available disk space"
        ),
    )
