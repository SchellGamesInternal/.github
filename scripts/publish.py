import os
import sys
import json
import re
import shutil
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
    verdaccio_token = os.environ.get("VERDACCIO_TOKEN", "")
    github_ref = os.environ.get("GITHUB_REF_NAME", "")

    # 1. Detect OS & Resolve UPM Executable Path
    if sys.platform == "win32":
        default_upm = r"C:\upm\bin\upm"
    else:
        default_upm = "/opt/upm/bin/upm"

    upm_path = resolve_binary(default_upm)
    if not os.path.isfile(upm_path):
        upm_path = resolve_binary("upm")

    print(f"--> Operating System: {sys.platform}")
    print(f"--> Resolved UPM Executable: {upm_path}")

    if not os.path.isfile(upm_path) and not shutil.which(upm_path):
        print(f"Error: Could not locate UPM CLI binary on system! Checked path: '{upm_path}'", file=sys.stderr)
        sys.exit(1)

    # 2. Inject central .npmignore template
    template_npmignore = Path(".org-configs/templates/.npmignore")
    if template_npmignore.exists():
        shutil.copyfile(template_npmignore, ".npmignore")
        print("--> Injected central .npmignore template.")

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
        print(f"--> Injected publishConfig ({registry_url}/) into package.json")

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
    print(f"--> Assigned NPM publish tag: {npm_tag}")

    # 6. Clean residual .tgz files & pack via Unity UPM CLI
    parent_dir = Path("..")
    for tgz in parent_dir.glob("*.tgz"):
        try:
            tgz.unlink()
        except Exception:
            pass

    pack_cmd = [upm_path, "pack", ".", "--organization-id", org_id, "--destination", ".."]
    use_upm_shell = sys.platform == "win32" and upm_path.lower().endswith((".cmd", ".bat"))
    print(f"--> Packing package with: {upm_path}...")
    subprocess.run(pack_cmd, check=True, shell=use_upm_shell)

    # 7. Authenticate & Publish to Verdaccio
    clean_url = re.sub(r"^https?://", "", registry_url)
    with open(".npmrc", "w", encoding="utf-8") as f:
        f.write(f'//{clean_url}/:_authToken="{verdaccio_token}"\n')

    tgz_files = sorted(parent_dir.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not tgz_files:
        print("Error: No .tgz package file found in parent directory!", file=sys.stderr)
        sys.exit(1)

    target_tgz = tgz_files[0]
    npm_bin = resolve_binary("npm")
    pub_cmd = [npm_bin, "publish", str(target_tgz), "--registry", f"{registry_url}/", "--tag", npm_tag]
    use_npm_shell = sys.platform == "win32" and npm_bin.lower().endswith((".cmd", ".bat"))

    print(f"--> Publishing {target_tgz.name} to Verdaccio...")
    subprocess.run(pub_cmd, check=True, shell=use_npm_shell)
    print("--> SUCCESS: Published package successfully!")

if __name__ == "__main__":
    main()