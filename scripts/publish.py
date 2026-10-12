#!/usr/bin/env python3
import os
import sys
import json
import re
import shutil
import base64
import subprocess
from pathlib import Path

def resolve_binary(cmd_name_or_path):
    """Resolves executable paths across Linux and Windows environments."""
    if os.path.isfile(cmd_name_or_path):
        return cmd_name_or_path

    base, ext = os.path.splitext(cmd_name_or_path)
    candidates = []

    if sys.platform == "win32":
        if ext:
            candidates.extend([cmd_name_or_path, f"{base}.cmd", f"{base}.exe", f"{base}.bat"])
        else:
            candidates.extend([f"{cmd_name_or_path}.cmd", f"{cmd_name_or_path}.exe", f"{cmd_name_or_path}.bat", cmd_name_or_path])
    else:
        candidates.append(cmd_name_or_path)

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm_global = os.path.join(appdata, "npm", f"{os.path.basename(base)}.cmd")
            if os.path.isfile(npm_global):
                return npm_global

    return cmd_name_or_path

def main():
    registry_url = os.environ.get("REGISTRY_URL", "").rstrip("/")
    org_id = os.environ.get("UPM_ORGANIZATION_ID", "")
    verdaccio_token = os.environ.get("VERDACCIO_TOKEN", "").strip().strip('"')
    github_ref = os.environ.get("GITHUB_REF_NAME", "")

    # Validate secret presence early
    if not verdaccio_token:
        print("Error: VERDACCIO_TOKEN environment variable is missing or empty!", file=sys.stderr, flush=True)
        sys.exit(1)

    # 1. Detect OS & Resolve UPM Executable Path
    if sys.platform == "win32":
        default_upm = r"C:\upm\bin\upm"
    else:
        default_upm = "/opt/upm/bin/upm"

    upm_path = resolve_binary(default_upm)
    if not os.path.isfile(upm_path):
        upm_path = resolve_binary("upm")

    print(f"--> Operating System: {sys.platform}", flush=True)
    print(f"--> Resolved UPM Executable: {upm_path}", flush=True)

    use_upm = os.path.isfile(upm_path) or shutil.which(upm_path) is not None
    if not use_upm:
        print("--> WARNING: UPM CLI binary not found. Falling back to 'npm pack'...", flush=True)

    # 2. Inject central .npmignore template
    template_npmignore = Path(".org-configs/templates/.npmignore")
    if template_npmignore.exists():
        shutil.copyfile(template_npmignore, ".npmignore")
        print("--> Injected central .npmignore template.", flush=True)

    # 3. Inject publishConfig registry target into package.json
    pkg_json_path = Path("package.json")
    if pkg_json_path.exists():
        with open(pkg_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "publishConfig" not in data or not isinstance(data["publishConfig"], dict):
            data["publishConfig"] = {}
        data["publishConfig"]["registry"] = f"{registry_url}/"
        with open(pkg_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"--> Injected publishConfig ({registry_url}/) into package.json", flush=True)

    # 4. Clean up cloned central configs directory prior to packaging
    if Path(".org-configs").exists():
        shutil.rmtree(".org-configs", ignore_errors=True)

    # 5. Determine NPM Release Tag based on Git reference
    match = re.search(r"-(preview|alpha|beta|rc)", github_ref)
    if match:
        npm_tag = match.group(1)
    elif re.match(r"^v?\d+\.\d+\.\d+$", github_ref):
        npm_tag = "latest"
    else:
        npm_tag = "preview"
    print(f"--> Assigned NPM publish tag: {npm_tag}", flush=True)

    # 6. Clean residual .tgz files & pack package
    parent_dir = Path("..")
    for tgz in parent_dir.glob("*.tgz"):
        try:
            tgz.unlink()
        except Exception:
            pass

    if use_upm:
        pack_cmd = [upm_path, "pack", ".", "--organization-id", org_id, "--destination", ".."]
        use_upm_shell = sys.platform == "win32" and upm_path.lower().endswith((".cmd", ".bat"))
        print(f"--> Packing package with: {upm_path}...", flush=True)
        subprocess.run(pack_cmd, check=True, shell=use_upm_shell)
    else:
        npm_bin = resolve_binary("npm")
        use_npm_shell = sys.platform == "win32" and npm_bin.lower().endswith((".cmd", ".bat"))
        subprocess.run([npm_bin, "pack", "--pack-destination", ".."], check=True, shell=use_npm_shell)

    # 7. Authenticate & Publish to Verdaccio
    clean_url = re.sub(r"^https?://", "", registry_url).rstrip('/')

    # Detect credential format (raw username:password vs Bearer token)
    if ":" in verdaccio_token and not verdaccio_token.startswith("http"):
        print("--> Detected 'username:password' credentials. Encoding to Base64 Basic Auth (_auth)...", flush=True)
        b64_val = base64.b64encode(verdaccio_token.encode("utf-8")).decode("utf-8")
        auth_line = f'//{clean_url}/:_auth="{b64_val}"'
    else:
        print("--> Detected Bearer token. Configuring _authToken...", flush=True)
        auth_line = f'//{clean_url}/:_authToken="{verdaccio_token}"'

    npmrc_path = Path(".npmrc")
    npmrc_content = (
        f"{auth_line}\n"
        f"//{clean_url}/:always-auth=true\n"
    )

    npmrc_path.write_text(npmrc_content, encoding="utf-8")

    tgz_files = sorted(parent_dir.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not tgz_files:
        print("Error: No .tgz package file found in parent directory!", file=sys.stderr, flush=True)
        sys.exit(1)

    target_tgz = tgz_files[0]
    npm_bin = resolve_binary("npm")
    pub_cmd = [
        npm_bin, "publish",
        str(target_tgz),
        "--registry", f"{registry_url}/",
        "--userconfig", str(npmrc_path.resolve()),
        "--tag", npm_tag
    ]
    use_npm_shell = sys.platform == "win32" and npm_bin.lower().endswith((".cmd", ".bat"))

    print(f"--> Publishing {target_tgz.name} to Verdaccio...", flush=True)
    pub_result = subprocess.run(pub_cmd, shell=use_npm_shell)

    # Cleanup temporary .npmrc
    if npmrc_path.exists():
        npmrc_path.unlink()

    if pub_result.returncode != 0:
        print("Error: Failed to publish package to Verdaccio registry.", file=sys.stderr, flush=True)
        sys.exit(pub_result.returncode)

    print("--> SUCCESS: Published package successfully!", flush=True)

if __name__ == "__main__":
    main()