"""Dependency-free test runner — also works under pytest.

    python run_tests.py        # no pytest needed
    pytest                     # if installed

Discovers every `test_*` function in the `tests` package and runs it.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback


def main() -> int:
    import tests

    passed = 0
    failures: list[tuple[str, str]] = []
    for modinfo in pkgutil.iter_modules(tests.__path__, "tests."):
        mod = importlib.import_module(modinfo.name)
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            label = f"{modinfo.name}.{name}"
            try:
                fn()
                passed += 1
                print(f"  ok  {label}")
            except Exception:  # noqa: BLE001
                failures.append((label, traceback.format_exc()))
                print(f" FAIL {label}")

    print()
    for label, tb in failures:
        print(f"=== {label} ===\n{tb}")
    total = passed + len(failures)
    print(f"{passed}/{total} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
