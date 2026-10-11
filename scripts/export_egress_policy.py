#!/usr/bin/env python3
"""Generate the Azure egress rules from the single source of truth.

``data/policy/egress-policy.json`` is read by the local simulator in
:mod:`payer_agents.phi.egress`. This script projects the same file into the ARM shape
Bicep deploys, so the policy tested locally and the policy deployed to Azure cannot
drift apart without a test failing.

    python scripts/export_egress_policy.py            # write infra/generated/
    python scripts/export_egress_policy.py --check    # fail if the output is stale
    python scripts/export_egress_policy.py --format arm --stdout

``--check`` runs in CI and in ``tests/test_infra_sync.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from payer_agents.phi.egress import load_egress_policy  # noqa: E402

SOURCE = REPO_ROOT / "data" / "policy" / "egress-policy.json"
GENERATED_DIR = REPO_ROOT / "infra" / "generated"
RULES_FILE = GENERATED_DIR / "egress-rules.json"

BANNER = (
    "GENERATED FILE. Do not edit. Source: data/policy/egress-policy.json. "
    "Regenerate with: python scripts/export_egress_policy.py"
)


def build_rules() -> list[dict]:
    policy = load_egress_policy(SOURCE)
    return [rule.to_arm() for rule in policy.rules]


def build_arm_properties() -> dict:
    return load_egress_policy(SOURCE).to_arm_properties()


def render(fmt: str) -> str:
    if fmt == "arm":
        payload: object = {"properties": build_arm_properties()}
    else:
        payload = build_rules()
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["rules", "arm"], default="rules")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero if the generated file is stale"
    )
    args = parser.parse_args(argv)

    rendered = render(args.format)

    if args.stdout:
        print(rendered, end="")
        return 0

    if args.check:
        if not RULES_FILE.exists():
            print(f"missing {_short(RULES_FILE)}; run this script", file=sys.stderr)
            return 1
        if RULES_FILE.read_text(encoding="utf-8") != render("rules"):
            print(
                f"{_short(RULES_FILE)} is out of date with {_short(SOURCE)}; run "
                "python scripts/export_egress_policy.py",
                file=sys.stderr,
            )
            return 1
        print("egress rules are in sync")
        return 0

    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(render("rules"), encoding="utf-8")
    (GENERATED_DIR / "README.md").write_text(f"# Generated\n\n{BANNER}\n", encoding="utf-8")
    print(f"wrote {_short(RULES_FILE)} ({len(build_rules())} rules)")
    return 0


def _short(path: Path) -> str:
    """Repo-relative when possible; tests point these paths at a temp directory."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
