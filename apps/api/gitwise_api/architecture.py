from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class PythonImport:
    target_module: str
    line_number: int


def module_name_for_path(path: str) -> str | None:
    """Map a Python source path to a conservative importable module name."""
    if not path.endswith(".py") or path.startswith("."):
        return None

    parts = path[:-3].split("/")
    if parts[0] in {"src", "lib"}:
        parts = parts[1:]
    elif len(parts) > 2 and parts[:2] == ["apps", "api"]:
        parts = parts[2:]

    if not parts or any(not part.isidentifier() for part in parts):
        return None
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts[-1] = parts[-1]
    return ".".join(parts) or None


def extract_python_imports(source: str, module_name: str, path: str) -> list[PythonImport]:
    """Extract import targets without importing or executing repository code."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, TypeError):
        return []

    is_package = path.endswith("/__init__.py") or path == "__init__.py"
    package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
    imports: list[PythonImport] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                PythonImport(target_module=alias.name, line_number=node.lineno)
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package_parts) - (node.level - 1)
                if keep < 0:
                    continue
                base_parts = package_parts[:keep]
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""

            if base:
                imports.append(PythonImport(target_module=base, line_number=node.lineno))
                imports.extend(
                    PythonImport(
                        target_module=f"{base}.{alias.name}",
                        line_number=node.lineno,
                    )
                    for alias in node.names
                    if alias.name != "*"
                )

    return imports
