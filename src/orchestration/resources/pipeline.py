"""
src/orchestration/resources/pipeline.py
=========================================
Shared Dagster resource for the pipeline.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from dagster import ConfigurableResource
from dotenv import load_dotenv
from pydantic import Field

# Load env files
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)
_BACKEND_ENV = Path(__file__).resolve().parent.parent.parent / "backend" / ".env"
load_dotenv(_BACKEND_ENV)

# Repo root — computed once at module level as a plain string
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)


class PipelineConfig(ConfigurableResource):
    """
    Central configuration resource injected into all pipeline assets.
    All fields must be plain Python primitives (str, int, bool, float)
    to satisfy Dagster's config validation.
    """

    repo_root:        str  = Field(default=REPO_ROOT)
    dbt_project_dir:  str  = Field(default=os.getenv("DBT_PROJECT_DIR", "src/transformations"))
    dbt_profiles_dir: str  = Field(default=os.getenv("DBT_PROFILES_DIR", "src/transformations"))
    django_api_base:  str  = Field(default=os.getenv("DJANGO_API_BASE", "http://localhost:8000/api/v1"))
    django_api_key:   str  = Field(default=os.getenv("DJANGO_API_KEY", ""))
    redis_url:        str  = Field(default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    local_dev_mode:   bool = Field(
        default=os.getenv("R2_LOCAL_DEV_MODE", "False").lower() in ("true", "1")
    )

    @property
    def bronze_root(self) -> Path:
        return Path(self.repo_root) / "data" / "bronze"

    @property
    def silver_root(self) -> Path:
        return Path(self.repo_root) / "data" / "silver" / "products"

    def _backend_python(self) -> str:
        return str(Path(self.repo_root) / "src" / "backend" / ".venv" / "bin" / "python")

    def run_manage_py(self, *args: str) -> subprocess.CompletedProcess:
        """Run a Django management command in the backend venv."""
        cmd = [
            self._backend_python(),
            str(Path(self.repo_root) / "src" / "backend" / "manage.py"),
            *args,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self.repo_root,
            env={
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "core.settings",
                "PYTHONPATH": (
                    str(Path(self.repo_root) / "src" / "backend") +
                    ":" +
                    str(Path(self.repo_root) / "src")
                ),
            },
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result

    def run_dbt(self, *args: str, date_vars: dict | None = None) -> subprocess.CompletedProcess:
        """Run a dbt command in the transformations venv."""
        dbt_bin = str(
            Path(self.repo_root) / "src" / "transformations" / ".venv" / "bin" / "dbt"
        )
        cmd = [
            dbt_bin,
            *args,
            "--project-dir",  str(Path(self.repo_root) / self.dbt_project_dir),
            "--profiles-dir", str(Path(self.repo_root) / self.dbt_profiles_dir),
        ]
        if date_vars:
            cmd += ["--vars", json.dumps(date_vars)]

        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            cwd=self.repo_root,
            env={
                **os.environ,
                "R2_LOCAL_DEV_MODE": "True" if self.local_dev_mode else "False",
            },
        )
