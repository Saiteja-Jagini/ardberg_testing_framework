"""Resolve baseline execution from pinned files, without a model request."""

import json
import re
import shlex
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import TestService


def command_line(value: str) -> str:
    if not value.strip() or len(value) > 1000 or any(ord(c) < 32 for c in value):
        raise ValueError("Commands must be bounded, nonempty single lines")
    return value.strip()


class SetupProcess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    ready_url: str

    _command = field_validator("command")(command_line)

    @field_validator("ready_url")
    @classmethod
    def local_url(cls, value):
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or url.hostname not in
                {"localhost", "127.0.0.1", "0.0.0.0"} or not url.port or
                url.username or url.password):
            raise ValueError("Readiness URLs must use a local container endpoint and explicit port")
        return value


class NativeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    kind: Literal["test", "typecheck", "build"] = "test"
    command: str
    requires_application: bool = False
    requires_services: bool = True

    _command = field_validator("command")(command_line)


class SetupRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    install_commands: list[str] = Field(default_factory=list, max_length=12)
    setup_commands: list[str] = Field(default_factory=list, max_length=12)
    services: list[TestService] = Field(default_factory=list, max_length=1)
    processes: list[SetupProcess] = Field(default_factory=list, max_length=4)
    checks: list[NativeCheck] = Field(default_factory=list, max_length=20)
    runtime_versions: dict[Literal["node", "python"], str] = Field(default_factory=dict)
    test_environment: dict[str, str] = Field(default_factory=dict, max_length=40)

    @field_validator("install_commands", "setup_commands")
    @classmethod
    def commands(cls, values):
        return list(dict.fromkeys(command_line(v) for v in values))

    @model_validator(mode="after")
    def validate_recipe(self):
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError("Native check names must be unique")
        for version in self.runtime_versions.values():
            if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
                raise ValueError("Runtime versions must be numeric major, minor or exact versions")
        reserved = {"PATH", "HOME", "PYTHONPATH", "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH"}
        connections = set()
        for service in self.services:
            if not service.connection_environment_keys:
                raise ValueError("PostgreSQL needs a connection environment key")
            for key in service.connection_environment_keys:
                if not key.isidentifier() or key.upper() in reserved:
                    raise ValueError("Invalid service environment key")
            connections.update(service.connection_environment_keys)
            for cmd in service.setup_commands:
                command_line(cmd)
        for key, value in self.test_environment.items():
            if (not key.isidentifier() or key.upper() in reserved or key in connections or
                    len(value) > 2000 or any(ord(c) < 32 for c in value) or
                    value.lower().startswith(("postgres://", "postgresql://"))):
                raise ValueError("Only nonsecret test values are allowed; database URLs are generated")
        return self

    def analysis(self):
        return {
            "install_commands": self.install_commands,
            "runtime_setup_commands": self.setup_commands,
            "services": [s.model_dump() for s in self.services],
            "runtime_processes": [p.model_dump() for p in self.processes],
            "runtime_target_url": self.processes[0].ready_url if self.processes else "",
            "runtime_versions": self.runtime_versions,
            "test_environment": [{"key": k, "value": v} for k, v in self.test_environment.items()],
        }


def _read(source: Path, relative: str):
    path = source / relative
    if not path.exists():
        return None
    if path.is_symlink() or source not in path.resolve().parents or path.stat().st_size > 200_000:
        raise ValueError(f"Unsupported setup evidence: {relative}")
    return path.read_text(encoding="utf-8")


def resolve_recipe(source: Path) -> tuple[SetupRecipe, str, list[str]]:
    """Explicit configuration wins; never fall back after invalid configuration."""
    source = source.resolve()
    if not source.is_dir():
        raise ValueError("Pinned source snapshot unavailable")
    configured = _read(source, ".ardberg/setup.json")
    if configured is not None:
        recipe = SetupRecipe.model_validate_json(configured)
        for service in recipe.services:
            if not service.evidence_files or any(_read(source, p) is None for p in service.evidence_files):
                raise ValueError("Configured services need evidence files in the pinned checkout")
        return recipe, ".ardberg/setup.json", []
    manifest = _read(source, "package.json")
    if manifest is not None:
        package = json.loads(manifest)
        manager = package.get("packageManager", "").split("@")[0]
        if not manager:
            manager = next((name for lock, name in [("pnpm-lock.yaml", "pnpm"),
                           ("yarn.lock", "yarn"), ("package-lock.json", "npm")]
                           if (source / lock).is_file()), "npm")
        if manager not in {"npm", "pnpm", "yarn"}:
            raise ValueError("Package manager needs an explicit .ardberg/setup.json recipe")
        install = {"npm": "npm ci" if (source / "package-lock.json").is_file() else "npm install",
                   "pnpm": "pnpm install --frozen-lockfile" if (source / "pnpm-lock.yaml").is_file() else "pnpm install --no-frozen-lockfile",
                   "yarn": "yarn install --immutable" if (source / "yarn.lock").is_file() else "yarn install"}
        if manager == "yarn" and str(package.get("packageManager", "")).startswith("yarn@1."):
            install["yarn"] = "yarn install --frozen-lockfile"
        scripts = package.get("scripts", {})
        checks, seen = [], set()
        for name, body in scripts.items():
            if not re.fullmatch(r"[A-Za-z0-9:_-]+", name) or not isinstance(body, str):
                continue
            kind = ("test" if name == "test" or name.startswith("test:") else
                    "typecheck" if name in {"typecheck", "type-check"} or
                    name.startswith(("typecheck:", "type-check:")) else
                    "build" if name == "build" or name.startswith("build:") else None)
            if not kind or re.search(r"\b(watch|dev|deploy|publish)\b", name + " " + body):
                continue
            if body in seen or len(checks) >= 20:
                continue
            seen.add(body)
            checks.append(NativeCheck(name=f"{kind}-{len(checks) + 1}", kind=kind,
                                      command=f"{manager} run {shlex.quote(name)}"))
        return SetupRecipe(install_commands=[install[manager]], checks=checks), "package.json", [
            "Manifest defaults install dependencies and run declared checks. Application processes, "
            "services and fixtures require .ardberg/setup.json; application readiness is unverified."]
    if _read(source, "requirements.txt") is not None:
        return SetupRecipe(install_commands=["python -m pip install -r requirements.txt"]), "requirements.txt", [
            "Configure .ardberg/setup.json for native checks, runtime versions, services and application readiness."]
    raise ValueError("No supported setup recipe; add .ardberg/setup.json to the repository")
