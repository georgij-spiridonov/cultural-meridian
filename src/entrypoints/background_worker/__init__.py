"""Пакет фонового воркера регулярного сбора, нормализации и проверки источников."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.entrypoints.background_worker.runner import (
        BackgroundWorkerRunner,
        build_argument_parser,
        main,
        run_background_worker,
    )

__all__ = ["BackgroundWorkerRunner", "build_argument_parser", "main", "run_background_worker"]


def __getattr__(name: str) -> object:
    """Лениво экспортирует публичные объекты модуля `runner` без предупреждения `runpy`."""
    if name in __all__:
        from src.entrypoints.background_worker import runner

        return getattr(runner, name)
    raise AttributeError(f"Модуль {__name__!r} не содержит атрибут {name!r}.")
