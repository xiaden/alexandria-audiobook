#!/usr/bin/env python3
"""Unit tests for resolve_task_llm temperature resolution.

Run:  python test_resolve_task_llm.py

Covers the temperature threading fix: TaskLLMConfig/LLMConfig gained a
``temperature`` field and ``resolve_task_llm`` now returns it with the
resolution order task override -> global default -> hardcoded fallback (0.6).
"""

import os
import sys
import tempfile

# Make app/ importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import resolve_task_llm  # noqa: E402

RESULTS = {"passed": 0, "failed": 0}
FAILURES = []


def check(name, got, expected):
    if got == expected:
        RESULTS["passed"] += 1
        return
    RESULTS["failed"] += 1
    FAILURES.append(f"{name}: got {got!r}, expected {expected!r}")


def _write_config(data):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(__import__("json").dumps(data))
    return path


def main():
    # 1. No config -> hardcoded fallbacks.
    r = resolve_task_llm("scene_segmentation")
    check("fallback temp", r["temperature"], 0.6)
    check("fallback model", r["model_name"], "richardyoung/qwen3-14b-abliterated:Q8_0")
    check("fallback reasoning", r["reasoning_effort"], None)

    # 2. Task override temperature wins.
    cfg = {
        "llm": {
            "temperature": 0.6,
            "task_overrides": {
                "scene_segmentation": {"temperature": 0.1},
                "delivery": {"temperature": 0.3},
            },
        }
    }
    path = _write_config(cfg)
    check("task override 0.1", resolve_task_llm("scene_segmentation", path)["temperature"], 0.1)
    check("task override 0.3", resolve_task_llm("delivery", path)["temperature"], 0.3)

    # 3. Unlisted task inherits global default.
    check("global default", resolve_task_llm("unknown_task", path)["temperature"], 0.6)

    # 4. Global default changed.
    cfg2 = {"llm": {"temperature": 0.5}}
    path2 = _write_config(cfg2)
    check("global 0.5", resolve_task_llm("unknown_task", path2)["temperature"], 0.5)

    # 5. Explicit 0.0 task override honored (not treated as unset).
    cfg3 = {"llm": {"task_overrides": {"scene_segmentation": {"temperature": 0.0}}}}
    path3 = _write_config(cfg3)
    check("explicit 0.0", resolve_task_llm("scene_segmentation", path3)["temperature"], 0.0)

    print(f"passed={RESULTS['passed']} failed={RESULTS['failed']}")
    for f in FAILURES:
        print("FAIL:", f)
    return 1 if RESULTS["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
