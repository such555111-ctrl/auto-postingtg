"""Логирование: консоль + ротируемый файл. Логгер «admin» — действия администраторов."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import Config

FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg.log_file:
        handlers.append(RotatingFileHandler(cfg.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
    logging.getLogger("aiogram").setLevel(max(level, logging.INFO))
