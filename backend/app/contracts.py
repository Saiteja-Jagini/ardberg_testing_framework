"""Compare checked-in OpenAPI descriptions from the pinned base and PR."""

import json
from pathlib import Path

import yaml


METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _expand_refs(value, document: dict, active: tuple[str, ...] = (), depth: int = 0):
    """Include local component definitions in operation comparisons without looping on cycles."""
    if depth > 64:
        return {"truncated": True}
    if isinstance(value, list):
        return [_expand_refs(item, document, active, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in active:
            return {"$ref": ref, "cycle": True}
        target = document
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or key not in target:
                return {"$ref": ref, "unresolved": True}
            target = target[key]
        return {"$ref": ref, "resolved": _expand_refs(target, document, active + (ref,), depth + 1)}
    return {key: _expand_refs(item, document, active, depth + 1) for key, item in value.items()}


def _operations(root: Path) -> tuple[dict[str, dict], list[str]]:
    found = {}
    sources = []
    for path in root.rglob("*"):
        if (not path.is_file() or path.is_symlink() or
                path.suffix.lower() not in {".json", ".yaml", ".yml"} or
                not any(part in path.name.lower() for part in ("openapi", "swagger")) or
                path.stat().st_size > 2_000_000):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            document = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(document, dict) or not (document.get("openapi") or document.get("swagger")):
            continue
        relative = path.relative_to(root).as_posix()
        sources.append(relative)
        paths = document.get("paths")
        if not isinstance(paths, dict):
            continue
        for route, methods in paths.items():
            if not isinstance(route, str):
                continue
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if isinstance(method, str) and method.lower() in METHODS and isinstance(operation, dict):
                    requirements = operation.get("security", document.get("security"))
                    components = document.get("components")
                    schemes = components.get("securitySchemes", {}) if isinstance(components, dict) else {}
                    referenced_schemes = {
                        name: schemes.get(name) for requirement in requirements or []
                        if isinstance(requirement, dict) for name in requirement
                    } if isinstance(schemes, dict) and isinstance(requirements, list) else {}
                    found[f"{method.upper()} {route}"] = {
                        "source": relative,
                        "parameters": _expand_refs(methods.get("parameters", []) + operation.get("parameters", []), document)
                        if isinstance(methods.get("parameters", []), list) and isinstance(operation.get("parameters", []), list)
                        else _expand_refs(operation.get("parameters", []), document),
                        "requestBody": _expand_refs(operation.get("requestBody"), document),
                        "responses": _expand_refs(operation.get("responses", {}), document),
                        "security": _expand_refs({"requirements": requirements, "schemes": referenced_schemes}, document)
                        if requirements is not None else None,
                    }
    return found, sources


def compare_openapi(base: Path | None, head: Path) -> dict:
    current, current_sources = _operations(head)
    if base is None or not base.is_dir():
        return {"status": "unavailable", "reason": "Pinned base source is unavailable",
                "head_sources": current_sources, "changes": []}
    original, base_sources = _operations(base)
    if not current_sources and not base_sources:
        return {"status": "not_applicable", "reason": "No checked-in OpenAPI document was found",
                "head_sources": [], "base_sources": [], "changes": []}
    changes = []
    for route in sorted(original.keys() | current.keys()):
        before, after = original.get(route), current.get(route)
        if before == after:
            continue
        changes.append({"route": route,
                        "kind": "added" if before is None else "removed" if after is None else "changed",
                        "base_source": before["source"] if before else None,
                        "head_source": after["source"] if after else None,
                        "changed_fields": [field for field in ("parameters", "requestBody", "responses", "security")
                                           if before and after and before[field] != after[field]]})
    return {"status": "compared", "head_sources": current_sources,
            "base_sources": base_sources, "changes": changes[:300],
            "truncated": len(changes) > 300}
