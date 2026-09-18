"""Run every local suite in order and summarise."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SUITES = [
    ("interpretation accuracy", "test_interpret.py"),
    ("llm provider chain", "test_llm_chain.py"),
    ("spec compliance", "test_spec_compliance.py"),
    ("official sample pack", "test_official_samples.py"),
    ("end to end", "test_end_to_end.py"),
    ("randomised stress", "test_stress.py"),
]


def main() -> int:
    failed = []
    for label, script in SUITES:
        print("\n" + "=" * 72)
        print("  " + label)
        print("=" * 72)
        code = subprocess.call([sys.executable, os.path.join(HERE, script)], cwd=ROOT)
        if code:
            failed.append(label)

    print("\n" + "=" * 72)
    if failed:
        print("  FAILED: " + ", ".join(failed))
    else:
        print("  all {} suites passed".format(len(SUITES)))
    print("=" * 72)
    return len(failed)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
