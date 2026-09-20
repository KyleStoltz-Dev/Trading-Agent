"""Experiments command implementations; registration stays in app.cli."""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from app.schemas import (
    StrategyExperimentCreate,
    StrategyExperimentRead,
    StrategyTestSampleCreate,
)
from app.services.market_features import (
    experiment_feature_correlations,
    strategy_experiment_report,
)
from app.services.strategy_workspace import (
    add_strategy_test_sample,
    complete_strategy_experiment,
    create_strategy_experiment,
    resolve_strategy_experiment,
)
from app.terminal.runtime import CommandRuntime


def experiment_start(
    runtime: CommandRuntime,
    strategy: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    mode: Annotated[str, typer.Option(help="backtest or forward_test")],
    hypothesis: Annotated[str, typer.Option()],
    instrument: Annotated[str | None, typer.Option()] = None,
    timeframe: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Start a test frozen to one exact strategy definition hash."""
    try:
        request = StrategyExperimentCreate(
            strategy=strategy,
            name=name,
            mode=mode,
            hypothesis=hypothesis,
            instrument=instrument,
            timeframe=timeframe,
        )
    except ValidationError as exc:
        runtime.console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    runtime.authorize(
        "create_strategy_experiment",
        request.model_dump(mode="json"),
        mutating=True,
        assume_yes=yes,
    )
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            runtime.print_model(
                StrategyExperimentRead.model_validate(
                    create_strategy_experiment(
                        db,
                        request,
                        scope=runtime.current_scope(db),
                    )
                )
            )
        except LookupError as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


def experiment_sample(
    runtime: CommandRuntime,
    experiment_id: str,
    file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="StrategyTestSampleCreate JSON."),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Add one eligible, excluded, or unclear test observation."""
    try:
        request = StrategyTestSampleCreate.model_validate_json(file.read_text())
    except (OSError, ValidationError) as exc:
        runtime.console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    runtime.authorize(
        "add_strategy_test_sample",
        {
            "experiment_id": str(experiment_id),
            **request.model_dump(mode="json"),
        },
        mutating=True,
        assume_yes=yes,
    )
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            sample = add_strategy_test_sample(
                db,
                experiment_id,
                request,
                scope=runtime.current_scope(db),
            )
            runtime.print_model(
                {
                    "id": sample.id,
                    "experiment_id": sample.experiment_id,
                    "classification": sample.classification,
                    "outcome_r": sample.outcome_r,
                    "feature_snapshot": sample.feature_snapshot,
                    "created_at": sample.created_at,
                }
            )
        except (ValueError, LookupError) as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


def experiment_correlations(
    runtime: CommandRuntime,
    experiment_id: str,
    minimum_samples: Annotated[int, typer.Option(min=5, max=1000)] = 10,
) -> None:
    """Measure descriptive feature/outcome correlations for one isolated test."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            experiment = resolve_strategy_experiment(
                db,
                experiment_id,
                scope=runtime.current_scope(db),
            )
        except LookupError as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        runtime.print_model(
            experiment_feature_correlations(
                db,
                experiment.id,
                scope=runtime.current_scope(db),
                minimum_samples=minimum_samples,
            )
        )


def experiment_report(runtime: CommandRuntime, experiment_id: str) -> None:
    """Show sample counts, exclusions, expectancy, and feature correlations."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            runtime.print_model(
                strategy_experiment_report(
                    db,
                    experiment_id,
                    scope=runtime.current_scope(db),
                )
            )
        except LookupError as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc


def experiment_complete(
    runtime: CommandRuntime,
    experiment_id: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Freeze a running backtest or forward test."""
    runtime.authorize(
        "complete_strategy_experiment",
        {"experiment_id": str(experiment_id)},
        mutating=True,
        assume_yes=yes,
    )
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            runtime.print_model(
                StrategyExperimentRead.model_validate(
                    complete_strategy_experiment(
                        db,
                        experiment_id,
                        scope=runtime.current_scope(db),
                    )
                )
            )
        except (ValueError, LookupError) as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


def experiment_show(runtime: CommandRuntime, experiment_id: str) -> None:
    """Show an experiment and its frozen strategy hash."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            experiment = resolve_strategy_experiment(
                db,
                experiment_id,
                scope=runtime.current_scope(db),
            )
        except LookupError as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        runtime.print_model(StrategyExperimentRead.model_validate(experiment))
