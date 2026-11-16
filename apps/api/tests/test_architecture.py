from gitwise_api.architecture import extract_python_imports, module_name_for_path


def test_module_name_for_package_and_source_layouts() -> None:
    assert module_name_for_path("src/gitwise/core.py") == "gitwise.core"
    assert module_name_for_path("apps/api/gitwise_api/main.py") == "gitwise_api.main"
    assert module_name_for_path("src/gitwise/__init__.py") == "gitwise"


def test_extracts_absolute_and_relative_imports_without_execution() -> None:
    source = """import os
from .models import Repository
from gitwise_api import config
"""
    imports = extract_python_imports(source, "gitwise_api.service", "service.py")

    assert [(item.target_module, item.line_number) for item in imports] == [
        ("os", 1),
        ("gitwise_api.models", 2),
        ("gitwise_api.models.Repository", 2),
        ("gitwise_api", 3),
        ("gitwise_api.config", 3),
    ]


def test_invalid_python_source_is_ignored() -> None:
    assert extract_python_imports("def broken(:\n", "package.module", "module.py") == []
