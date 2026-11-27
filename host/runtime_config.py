"""Stage trusted runtime files and apply the caller's web-tool setting."""

import argparse
import json
from pathlib import Path
import shutil


WEB_ACTIONS = ("websearch", "webfetch")
COPIED_FILES = ("limits.json", "credential-proxy.json")


def stage(source, target, *, allow_web):
    """Validate the global permissions-list shape, apply web policy, and copy runtime files.

    OpenCode validates the remaining configuration schema when it loads the files.
    The workflow supplies a target directory outside the trusted source checkout.
    """
    if not isinstance(allow_web, bool):
        raise ValueError("allow_web must be a boolean")
    source, target = Path(source), Path(target)
    config = json.loads((source / "opencode.json").read_text())
    if not isinstance(config, dict) or not isinstance(config.get("permissions"), list):
        raise ValueError("opencode.json must define a global permissions list")
    # OpenCode appends agent rules to global rules; CLI tests check the effective policy.
    if not allow_web:
        config["permissions"].extend(
            {"action": action, "resource": "*", "effect": "deny"} for action in WEB_ACTIONS)
    target.mkdir(parents=True, exist_ok=True)
    (target / "opencode.json").write_text(json.dumps(config) + "\n")
    for filename in COPIED_FILES:
        shutil.copyfile(source / filename, target / filename)


def main(argv=None):
    """Accept only literal workflow boolean strings and stage the trusted runtime files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--allow-web", required=True, choices=("true", "false"))
    args = parser.parse_args(argv)
    try:
        stage(args.source, args.target, allow_web=args.allow_web == "true")
    except (ValueError, OSError) as error:
        parser.exit(1, f"runtime config: {error}\n")


if __name__ == "__main__":
    main()
