#!/usr/bin/env python3
import os
import sys
import json
import re
import shutil
import base64
import subprocess
from pathlib import Path

def log(msg):
    print(msg, flush=True)

def get_env_var(name, required=True, default=None):
    val = os.getenv(name, default)
    if required and not val:
        log(f"Error: Missing required environment variable: {name}")
        sys.exit(1)
    return val

def resolve_cmd(binary_name):
    """Resolves binary name to full path (handles npm.cmd / upm.exe on Windows)."""
    return shutil.which(binary_name)

def find_upm_binary():
    """Searches standard system and user paths across Linux and Windows."""
    candidates = [
        "/opt/upm/bin/upm",
        "/usr/local/bin/upm",
        "/usr/bin/upm",
        os.path.expanduser("~/.local/bin/upm"),
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
    log("=========================================")
    log("  Unity UPM Central Publishing Engine   ")
    log("=========================================\n")

    verdaccio_url = get_env_var("VERDACCIO_URL", required=True).rstrip('/')
    verdaccio_token = get_env_var("VERDACCIO_TOKEN", required=True).strip()
    upm_org_id = os.getenv("UPM_ORGANIZATION_ID", "")
    git_ref = os.getenv("GITHUB_REF_NAME", "")

    npm_tag = determine_npm_tag(git_ref)
    log(f"--> Target Registry: {verdaccio_url}")
    log(f"--> Assigned NPM Release Tag: {npm_tag}")

    # 1. Inject Master Template .npmignore
    template_ignore = Path(".org-configs/templates/.npmignore")
    if template_ignore.exists():
        shutil.copy(template_ignore, ".npmignore")
        log("--> Injected master .npmignore template.")

    # 2. Inject publishConfig into package.json
    pkg_path = Path("package.json")
    if not pkg_path.exists():
        log("Error: package.json not found in repository root!")
        sys.exit(1)

    with open(pkg_path, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    if "publishConfig" not in pkg_data or not isinstance(pkg_data["publishConfig"], dict):
        pkg_data["publishConfig"] = {}

    pkg_data["publishConfig"]["registry"] = f"{verdaccio_url}/"

    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump(pkg_data, f, indent=2)
    log(f"--> Injected publishConfig ({verdaccio_url}/) into package.json.")

    # 3. Clean up existing .tgz artifacts
    parent_dir = Path("..")
    for tgz_file in parent_dir.glob("*.tgz"):
        try:
            tgz_file.unlink()
        except OSError:
            pass

    # 4. Pack Package (Unity UPM CLI with npm pack fallback)
    upm_bin = find_upm_binary()
    npm_bin = resolve_cmd("npm")

    if upm_bin:
        log(f"--> Packaging using Unity UPM CLI: {upm_bin}")
        cmd = [upm_bin, "pack", ".", "--destination", ".."]
        if upm_org_id:
            cmd.extend(["--organization-id", upm_org_id])

        result = subprocess.run(cmd)
        if result.returncode != 0:
            log("Error: Unity UPM pack operation failed.")
            sys.exit(result.returncode)
    elif npm_bin:
        log("--> WARNING: Unity UPM CLI not found. Falling back to 'npm pack'...")
        result = subprocess.run([npm_bin, "pack", "--pack-destination", ".."])
        if result.returncode != 0:
            log("Error: 'npm pack' fallback failed.")
            sys.exit(result.returncode)
    else:
        log("Error: Neither Unity 'upm' CLI nor 'npm' executable could be found on system.")
        sys.exit(1)

    # 5. Locate Packaged Tarball Artifact
    tgz_files = sorted(parent_dir.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not tgz_files:
        log("Error: No packaged .tgz artifact found in parent workspace.")
        sys.exit(1)

    artifact_path = tgz_files[0].resolve()
    log(f"--> Packaged Artifact: {artifact_path.name}")

    # 6. Authenticate & Publish to Verdaccio
    clean_url = re.sub(r'^https?://', '', verdaccio_url).rstrip('/')

    npmrc_lines = [
        f"registry={verdaccio_url}/",
        f"//{clean_url}/:_authToken={verdaccio_token}",
        f"//{clean_url}:_authToken={verdaccio_token}",
        f"{verdaccio_url}/:_authToken={verdaccio_token}",
        f"//{clean_url}/:always-auth=true",
        ""
    ]
    npmrc_content = "\n".join(npmrc_lines)

    local_npmrc = Path(".npmrc")
    user_npmrc = Path.home() / ".npmrc"

    local_npmrc.write_text(npmrc_content, encoding="utf-8")
    user_npmrc.write_text(npmrc_content, encoding="utf-8")

    if not npm_bin:
        log("Error: 'npm' executable not found for publish step.")
        sys.exit(1)

    publish_cmd = [
        npm_bin, "publish",
        str(artifact_path),
        "--registry", f"{verdaccio_url}/",
        "--tag", npm_tag
    ]

    log(f"--> Publishing {artifact_path.name} to Verdaccio...")
    publish_result = subprocess.run(publish_cmd)

    # Clean up temporary credential files
    if local_npmrc.exists():
        local_npmrc.unlink()
    if user_npmrc.exists():
        user_npmrc.unlink()

    if publish_result.returncode != 0:
        log("Error: Failed to publish package to Verdaccio registry.")
        sys.exit(publish_result.returncode)

    log("\n=========================================")
    log("  SUCCESS: Package successfully published!")
    log("=========================================")

if __name__ == "__main__":
    main()