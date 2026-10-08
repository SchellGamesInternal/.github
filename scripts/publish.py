#!/usr/bin/env python3
import os
import sys
import json
import re
import shutil
import subprocess
from pathlib import Path

def get_env_var(name, required=True, default=None):
    val = os.getenv(name, default)
    if required and not val:
        print(f"Error: Missing required environment variable: {name}")
        sys.exit(1)
    return val

def resolve_cmd(binary_name):
    """Resolves binary name to full path (handles npm.cmd / upm.exe on Windows)."""
    return shutil.which(binary_name)

def find_upm_binary():
    """Searches standard system and user paths across Linux and Windows."""
    candidates = [
        # Linux / macOS
        "/opt/upm/bin/upm",
        "/usr/local/bin/upm",
        "/usr/bin/upm",
        os.path.expanduser("~/.local/bin/upm"),
        # Windows
        r"C:\upm\bin\upm.exe",
        r"C:\Program Files\Unity\upm\bin\upm.exe",
    ]
    for path in candidates:
        if os.path.isfile(path) and (os.access(path, os.X_OK) or path.endswith(".exe")):
            return path

    return resolve_cmd("upm")

def determine_npm_tag(ref_name):
    if not ref_name:
        return "latest"

    match = re.search(r'-(preview|alpha|beta|rc)', ref_name, re.IGNORECASE)
    if match:
        return match.group(1).lower()

    if re.match(r'^v?\d+\.\d+\.\d+$', ref_name):
        return "latest"

    return "preview"

def main():
    print("=========================================")
    print("  Unity UPM Central Publishing Engine   ")
    print("=========================================\n")

    verdaccio_url = get_env_var("VERDACCIO_URL", required=True).rstrip('/')
    verdaccio_token = get_env_var("VERDACCIO_TOKEN", required=True)
    upm_org_id = os.getenv("UPM_ORGANIZATION_ID", "")
    git_ref = os.getenv("GITHUB_REF_NAME", "")

    npm_tag = determine_npm_tag(git_ref)
    print(f"--> Target Registry: {verdaccio_url}")
    print(f"--> Assigned NPM Release Tag: {npm_tag}")

    # Inject Master Template .npmignore
    template_ignore = Path(".org-configs/templates/.npmignore")
    if template_ignore.exists():
        shutil.copy(template_ignore, ".npmignore")
        print("--> Injected master .npmignore template.")

    # Inject publishConfig into package.json
    pkg_path = Path("package.json")
    if not pkg_path.exists():
        print("Error: package.json not found in repository root!")
        sys.exit(1)

    with open(pkg_path, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    if "publishConfig" not in pkg_data or not isinstance(pkg_data["publishConfig"], dict):
        pkg_data["publishConfig"] = {}

    pkg_data["publishConfig"]["registry"] = f"{verdaccio_url}/"

    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump(pkg_data, f, indent=2)
    print(f"--> Injected publishConfig ({verdaccio_url}/) into package.json.")

    # Clean up existing .tgz artifacts
    parent_dir = Path("..")
    for tgz_file in parent_dir.glob("*.tgz"):
        try:
            tgz_file.unlink()
        except OSError:
            pass

    # Pack Package
    upm_bin = find_upm_binary()
    npm_bin = resolve_cmd("npm")

    if upm_bin:
        print(f"--> Packaging using Unity UPM CLI: {upm_bin}")
        cmd = [upm_bin, "pack", ".", "--destination", ".."]
        if upm_org_id:
            cmd.extend(["--organization-id", upm_org_id])

        result = subprocess.run(cmd)
        if result.returncode != 0:
            print("Error: Unity UPM pack operation failed.")
            sys.exit(result.returncode)
    elif npm_bin:
        print("--> WARNING: Unity UPM CLI not found. Falling back to 'npm pack'...")
        result = subprocess.run([npm_bin, "pack", "--pack-destination", ".."])
        if result.returncode != 0:
            print("Error: 'npm pack' fallback failed.")
            sys.exit(result.returncode)
    else:
        print("Error: Neither Unity 'upm' CLI nor 'npm' executable could be found on system.")
        sys.exit(1)

    # Locate Packaged Tarball
    tgz_files = sorted(parent_dir.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not tgz_files:
        print("Error: No packaged .tgz artifact found in parent workspace.")
        sys.exit(1)

    artifact_path = tgz_files[0].resolve()
    print(f"--> Packaged Artifact: {artifact_path.name}")

    # Authenticate & Publish
    clean_url = re.sub(r'^https?://', '', verdaccio_url)
    npmrc_path = Path(".npmrc")
    npmrc_path.write_text(f"//{clean_url}/:_authToken=\"{verdaccio_token}\"\n", encoding="utf-8")

    if not npm_bin:
        print("Error: 'npm' executable not found for publish step.")
        sys.exit(1)

    publish_cmd = [
        npm_bin, "publish",
        str(artifact_path),
        "--registry", f"{verdaccio_url}/",
        "--tag", npm_tag
    ]

    print(f"--> Publishing {artifact_path.name} to Verdaccio...")
    publish_result = subprocess.run(publish_cmd)

    if npmrc_path.exists():
        npmrc_path.unlink()

    if publish_result.returncode != 0:
        print("Error: Failed to publish package to Verdaccio registry.")
        sys.exit(publish_result.returncode)

    print("\n=========================================")
    print("  SUCCESS: Package successfully published!")
    print("=========================================")

if __name__ == "__main__":
    main()