from pathlib import Path
from types import SimpleNamespace

from app.system_resources import GIB, ResourceSnapshot, assess_model_fit, resource_snapshot


def _snapshot(
    *,
    total_gb: int = 48,
    available_gb: int = 40,
    memory_percent: float = 16,
    swap_percent: float | None = 0,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        platform="TestOS",
        total_memory_bytes=total_gb * GIB,
        available_memory_bytes=available_gb * GIB,
        memory_percent=memory_percent,
        swap_total_bytes=8 * GIB if swap_percent is not None else None,
        swap_used_bytes=0 if swap_percent is not None else None,
        swap_percent=swap_percent,
        disk_free_bytes=100 * GIB,
    )


def _assess(
    snapshot: ResourceSnapshot,
    *,
    model_gb: int = 24,
    currently_loaded: bool = False,
):
    return assess_model_fit(
        model="test-model",
        model_size_bytes=model_gb * GIB,
        context_length=16_384,
        memory_reserve_gb=6,
        memory_block_percent=92,
        swap_block_percent=80,
        currently_loaded=currently_loaded,
        snapshot=snapshot,
    )


def test_model_fit_reports_ok_warning_and_structural_block() -> None:
    assert _assess(_snapshot(available_gb=40)).status == "ok"
    assert _assess(_snapshot(available_gb=26)).status == "warning"
    assert _assess(_snapshot(total_gb=16, available_gb=12)).status == "block"


def test_model_fit_blocks_dangerous_memory_or_swap_pressure() -> None:
    assert (
        _assess(
            _snapshot(available_gb=10, memory_percent=95),
            model_gb=9,
        ).status
        == "block"
    )
    assert (
        _assess(
            _snapshot(available_gb=10, swap_percent=90),
            model_gb=9,
        ).status
        == "block"
    )


def test_loaded_model_only_requires_incremental_runtime_headroom() -> None:
    assessment = _assess(
        _snapshot(available_gb=12),
        currently_loaded=True,
    )

    assert assessment.status == "ok"
    assert assessment.additional_memory_bytes == 3 * GIB


def test_resource_snapshot_keeps_working_when_swap_telemetry_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "app.system_resources.psutil.virtual_memory",
        lambda: SimpleNamespace(
            total=48 * GIB,
            available=30 * GIB,
            percent=37.5,
        ),
    )
    monkeypatch.setattr(
        "app.system_resources.psutil.swap_memory",
        lambda: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        "app.system_resources.psutil.disk_usage",
        lambda path: SimpleNamespace(free=90 * GIB),
    )

    snapshot = resource_snapshot(tmp_path)

    assert snapshot.swap_percent is None
    assert snapshot.model_dump()["swap_percent"] is None
    assert snapshot.disk_free_bytes == 90 * GIB
