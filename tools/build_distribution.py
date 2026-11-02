#!/usr/bin/env python3
"""Build a reproducible, allowlisted GitHub download (Python 3.11+, no dependencies)."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import tomllib
import uuid
import zipfile


REPOSITORY = Path(__file__).resolve().parents[1]
UNITY_PACKAGE = Path("verification/unity-6000.2/Packages/com.kuko.motionbridge")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)
EXCLUDED_PARTS = {"__pycache__", ".git", ".DS_Store", ".pytest_cache", ".ruff_cache"}


def read_file(root: Path, relative: str | Path) -> bytes:
    path = root / relative
    # Never follow links out of the reviewed source trees, or into private files.
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Required regular file missing: {relative}")
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"Symlink in distribution source: {relative}")
    return path.read_bytes()


def tree_files(root: Path, subtree: str, suffixes: set[str]) -> dict[str, bytes]:
    directory = root / subtree
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Required source directory missing: {directory}")
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(root)
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink in distribution source: {relative}")
        if path.is_file() and path.suffix in suffixes:
            result[relative.as_posix()] = read_file(root, relative)
    if not result:
        raise ValueError(f"Empty distribution source: {directory}")
    return result


def zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def tgz_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo("package/" + name)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_files(root: Path, *, preview: bool = False) -> tuple[str, dict[str, bytes]]:
    root = root.resolve()
    blender = root / "blender_addon"
    unity = root / UNITY_PACKAGE
    extension = tomllib.loads(read_file(blender, "blender_manifest.toml").decode())
    package = json.loads(read_file(unity, "package.json"))
    compatibility = json.loads(read_file(root, "compatibility_manifest.json"))
    version = extension["version"]
    if extension["id"] != "motion_bridge":
        raise ValueError("Unexpected Blender extension id")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Expected a stable three-part extension version")
    if package["name"] != "com.kuko.motionbridge":
        raise ValueError("Unexpected Unity package name")
    if not re.fullmatch(r"\d+\.\d+\.\d+", package["version"]):
        raise ValueError("Expected a stable three-part Unity version")
    if not any(sample.get("path") == "Samples~/Canonical" for sample in package.get("samples", [])):
        raise ValueError("Unity Canonical sample is not registered")
    if compatibility["blender"]["extensionVersion"] != version:
        raise ValueError("Blender version disagrees with compatibility_manifest.json")
    if compatibility["unity"]["packageVersion"] != package["version"]:
        raise ValueError("Unity version disagrees with compatibility_manifest.json")
    if compatibility["unity"]["editorVersion"] != package["unity"]:
        raise ValueError("Unity minimum version disagrees with compatibility manifest")
    client = read_file(blender, "motion_bridge/live/control_client.py").decode()
    if re.search(r'addon_version:\s*str\s*=\s*"' + re.escape(version) + r'"', client) is None:
        raise ValueError("Blender handshake version disagrees with extension version")
    unity_protocol = read_file(unity, "Runtime/Protocol/MotionBridgeProtocol.cs").decode()
    if re.search(
        r'const\s+string\s+PackageVersion\s*=\s*"' + re.escape(package["version"]) + r'"\s*;',
        unity_protocol,
    ) is None:
        raise ValueError("Unity handshake version disagrees with package version")
    for module in ("animation", "imgui", "jsonserialize"):
        if package.get("dependencies", {}).get("com.unity.modules." + module) != "1.0.0":
            raise ValueError(f"Required Unity module dependency missing or unsupported: {module}")

    addon = tree_files(blender, "motion_bridge", {".py"})
    for name in ("__init__.py", "addon.py", "motion_quality_worker.py", "motion_quality_process.py"):
        if "motion_bridge/" + name not in addon:
            raise ValueError(f"Required Blender runtime file missing: {name}")
    for name in ("__init__.py", "blender_manifest.toml", "LICENSE.txt"):
        addon[name] = read_file(blender, name)
    for name in ("MIT.txt", "GPL-3.0-or-later.txt"):
        content = read_file(root, "licenses/" + name)
        if read_file(blender, "licenses/" + name) != content:
            raise ValueError(f"Blender license copy is out of date: {name}")
        addon["licenses/" + name] = content

    upm = {}
    for folder in ("Editor", "Runtime"):
        upm.update(tree_files(unity, folder, {".cs", ".asmdef", ".meta"}))
        upm[folder + ".meta"] = read_file(unity, folder + ".meta")
    for name in ("package.json", "package.json.meta", "README.md", "README.md.meta"):
        upm[name] = read_file(unity, name)
    upm["LICENSE.md"] = read_file(root, "licenses/COMMERCIAL_TERMS_JA.md")
    license_guid = uuid.uuid5(uuid.NAMESPACE_URL, "urn:motionbridge:unity:LICENSE.md").hex
    upm["LICENSE.md.meta"] = (
        f"fileFormatVersion: 2\nguid: {license_guid}\nTextScriptImporter:\n"
        "  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n"
    ).encode()
    for name in ("MB_Canonical_Humanoid_v1.fbx", "README.md"):
        key = "Samples~/Canonical/" + name
        upm[key] = read_file(unity, key)
        if name.endswith(".fbx") and re.search(rb"/(?:Users|home)/|[A-Za-z]:[\\/]Users[\\/]", upm[key]):
            raise ValueError(f"Personal source path in Unity sample; regenerate with build_canonical.py: {key}")
    # Missing/duplicate .meta GUIDs cause broken Unity references on import.
    guids = set()
    for name, data in upm.items():
        if name.endswith((".cs", ".asmdef")) and name + ".meta" not in upm:
            raise ValueError(f"Missing Unity metadata: {name}")
        if name.endswith(".meta"):
            match = re.search(rb"(?m)^guid: ([a-f0-9]{32})$", data)
            if match is None or match[1] in guids:
                raise ValueError(f"Missing or duplicate Unity GUID: {name}")
            guids.add(match[1])

    files = {
        f"Blender/motion_bridge-{version}.zip": zip_bytes(addon),
        f"Unity/com.kuko.motionbridge-{package['version']}.tgz": tgz_bytes(upm),
        "Samples/mb_canonical_humanoid_v1.blend": read_file(
            root, "samples/canonical/mb_canonical_humanoid_v1.blend"
        ),
        "START_HERE.md": read_file(root, "docs/distribution/README_JA.md"),
        "QUICKSTART.md": read_file(root, "QUICKSTART.md"),
        "CHANGELOG.md": read_file(root, "CHANGELOG.md"),
        "compatibility_manifest.json": read_file(root, "compatibility_manifest.json"),
    }
    for name in ("README_JA.md", "MIT.txt", "GPL-3.0-or-later.txt", "COMMERCIAL_TERMS_JA.md"):
        files["Licenses/" + name] = read_file(root, "licenses/" + name)
    if preview:
        warning = "利用条件の確認用です。公開・配布には利用条件が確定したGitHub版ZIPを使用してください。\n"
        files["PREVIEW_NOT_FOR_SALE.txt"] = warning.encode()
        files["START_HERE.md"] = ("> " + warning + "\n").encode() + files["START_HERE.md"]
    # Seller drafts and validation projects deliberately never enter this file map.
    files["release_manifest.json"] = json_bytes({
        "schemaVersion": 1,
        "product": "Motion Bridge",
        "releaseVersion": version,
        "distributionStatus": "preview" if preview else "release",
        "blenderExtensionVersion": version,
        "unityPackageVersion": package["version"],
        "files": {name: {"bytes": len(data), "sha256": sha256(data)}
                  for name, data in sorted(files.items())},
    })
    files["SHA256SUMS.txt"] = "".join(
        f"{sha256(data)}  {name}\n" for name, data in sorted(files.items())
    ).encode()
    return version, files


def build(root: Path, output_dir: Path, *, preview: bool = False) -> Path:
    policy = json.loads(read_file(root.resolve(), "distribution_config.json"))
    if policy.get("licensePolicyConfirmed") is not True and not preview:
        raise ValueError("License terms are pending. Use --preview for a review ZIP; confirm terms before distribution.")
    version, files = build_files(root, preview=preview)
    product = f"MotionBridge-{version}"
    data = zip_bytes({f"{product}/{name}": value for name, value in files.items()})
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{product}-{'PREVIEW' if preview else 'GitHub'}.zip"
    # A failed build must leave the previous release intact.
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    target.with_suffix(".zip.sha256").write_text(
        f"{sha256(data)}  {target.name}\n", encoding="utf-8"
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPOSITORY / "dist")
    parser.add_argument("--preview", action="store_true", help="Build a review-only ZIP while license terms are pending")
    arguments = parser.parse_args()
    target = build(REPOSITORY, arguments.output_dir.expanduser().resolve(), preview=arguments.preview)
    print(f"Built {target} ({target.stat().st_size:,} bytes)")
    print(f"SHA-256 {sha256(target.read_bytes())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
