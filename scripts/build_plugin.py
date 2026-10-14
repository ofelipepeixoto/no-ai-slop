#!/usr/bin/env python3
"""Build and validate the distributable No AI Slop plugin archive."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
DIST = ROOT / "dist"
SKILL_ROOT = ROOT / "skills" / "no-ai-slop"
PACKAGE_SOURCES = {
    ".codex-plugin/plugin.json": MANIFEST,
    "assets/no-ai-slop.png": ROOT / "assets" / "no-ai-slop.png",
    "skills/no-ai-slop/SKILL.md": SKILL_ROOT / "SKILL.md",
    "skills/no-ai-slop/eval.md": SKILL_ROOT / "eval.md",
    "LICENSE": ROOT / "LICENSE",
    "PRIVACY.md": ROOT / "PRIVACY.md",
    "TERMS.md": ROOT / "TERMS.md",
}
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate without keeping build output")
    return parser.parse_args()


def require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{field} must be a non-empty string")


def validate_source(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise SystemExit("Manifest must be an object")
    required = ("name", "version", "description", "author", "skills", "interface")
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise SystemExit(f"Missing manifest fields: {', '.join(missing)}")

    for field in ("name", "version", "description", "skills"):
        require_text(manifest[field], field)
    if manifest["name"] != "no-ai-slop" or manifest["skills"] != "./skills/":
        raise SystemExit("Manifest name and skills must match the canonical package")
    if len(manifest["version"]) > 64 or not SEMVER.fullmatch(manifest["version"]):
        raise SystemExit("Version must be a safe semantic version of at most 64 characters")
    if not isinstance(manifest["author"], dict):
        raise SystemExit("author must be an object")
    for field in ("name", "url"):
        require_text(manifest["author"].get(field), f"author.{field}")

    interface = manifest["interface"]
    if not isinstance(interface, dict):
        raise SystemExit("interface must be an object")
    interface_required = (
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    )
    missing_interface = [key for key in interface_required if not interface.get(key)]
    if missing_interface:
        raise SystemExit(f"Missing interface fields: {', '.join(missing_interface)}")
    for field in interface_required:
        if field not in ("capabilities", "defaultPrompt"):
            require_text(interface[field], f"interface.{field}")
    capabilities = interface["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise SystemExit("interface.capabilities must be a non-empty list")
    for capability in capabilities:
        require_text(capability, "interface.capabilities entry")
    for field in ("composerIcon", "logo"):
        if interface.get(field) != "./assets/no-ai-slop.png":
            raise SystemExit(f"interface.{field} must reference the packaged logo")

    prompts = interface["defaultPrompt"]
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 3:
        raise SystemExit("Starter prompts must contain one to three string entries")
    for prompt in prompts:
        require_text(prompt, "Starter prompt")
    if any(len(prompt) > 128 for prompt in prompts):
        raise SystemExit("Starter prompts must contain at most three entries of 128 characters or fewer")

    for source in PACKAGE_SOURCES.values():
        if not source.is_file() or not source.resolve().is_relative_to(ROOT.resolve()):
            raise SystemExit(f"Missing package source: {source.relative_to(ROOT)}")


def build_plugin(manifest: dict, dist: Path = DIST) -> tuple[Path, Path]:
    if dist.is_symlink():
        raise SystemExit("Build directory must not be a symbolic link")
    plugin_root = dist / "no-ai-slop"
    if plugin_root.exists():
        shutil.rmtree(plugin_root)

    for relative, source in PACKAGE_SOURCES.items():
        target = plugin_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    archive = dist / f"no-ai-slop-plugin-{manifest['version']}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(plugin_root.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(dist))
    return plugin_root, archive


def validate_build(plugin_root: Path, archive: Path) -> None:
    expected = set(PACKAGE_SOURCES)
    actual = {
        str(path.relative_to(plugin_root))
        for path in plugin_root.rglob("*")
        if path.is_file()
    }
    if expected != actual:
        raise SystemExit(f"Unexpected package files: expected {sorted(expected)}, found {sorted(actual)}")

    for relative, source in PACKAGE_SOURCES.items():
        if (plugin_root / relative).read_bytes() != source.read_bytes():
            raise SystemExit(f"Packaged {relative} does not match the canonical file")
    if not zipfile.is_zipfile(archive):
        raise SystemExit("Plugin archive is not a valid ZIP file")
    with zipfile.ZipFile(archive) as package:
        expected_members = {f"no-ai-slop/{relative}" for relative in expected}
        members = package.namelist()
        if len(members) != len(expected_members) or set(members) != expected_members:
            raise SystemExit("Plugin archive contains unexpected or duplicate members")
        if package.testzip() is not None:
            raise SystemExit("Plugin archive contains corrupt data")
        for relative, source in PACKAGE_SOURCES.items():
            if package.read(f"no-ai-slop/{relative}") != source.read_bytes():
                raise SystemExit(f"Archive {relative} does not match the canonical file")


def main() -> None:
    args = parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    validate_source(manifest)
    if args.check:
        with tempfile.TemporaryDirectory(prefix="no-ai-slop-check-") as temporary:
            plugin_root, archive = build_plugin(manifest, Path(temporary))
            validate_build(plugin_root, archive)
        print("Validated plugin in a temporary build directory")
    else:
        plugin_root, archive = build_plugin(manifest)
        validate_build(plugin_root, archive)
        print(f"Built {archive.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
