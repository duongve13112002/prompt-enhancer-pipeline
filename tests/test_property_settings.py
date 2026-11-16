"""Audit every pipeline property module, including properties mixed into config/regression tests."""

import ast
import importlib
from pathlib import Path


def test_all_pipeline_properties_have_no_deadlines():
    properties = []
    for path in sorted(Path(__file__).parent.glob('test_*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        if not any(isinstance(node, ast.ImportFrom) and node.module == 'hypothesis'
                   or isinstance(node, ast.Import) and any(alias.name == 'hypothesis' for alias in node.names)
                   for node in ast.walk(tree)):
            continue
        module = importlib.import_module(path.stem)
        for name, value in vars(module).items():
            values = vars(value).items() if isinstance(value, type) and value.__module__ == module.__name__ else [(name, value)]
            for member, test in values:
                if getattr(test, 'is_hypothesis_test', False):
                    properties.append(f'{path.name}::{member}')
                    assert test._hypothesis_internal_use_settings.deadline is None, properties[-1]
    assert properties
