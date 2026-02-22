from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.config.env_config import EnvConfig, load_env_config
from src.config.yaml_config import AppConfig, load_app_config


@dataclass(frozen=True)
class SettingsRuntime:
    app_cfg: AppConfig
    env_cfg: EnvConfig


def build_settings_runtime(config_path: Optional[Path] = None) -> SettingsRuntime:
    return SettingsRuntime(
        app_cfg=load_app_config(config_path),
        env_cfg=load_env_config(),
    )
