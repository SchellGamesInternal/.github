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
            candidates.extend(
                [f"{cmd_name_or_path}.cmd", f"{cmd_name_or_path}.exe", f"{cmd_name_or_path}.bat", cmd_name_or_path]
            )
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


def find_packages_to_publish():
    """
    Locates packages to publish.
    1. If package.json exists in root, returns [Path(".")] for 100% backward compatibility.
    2. Otherwise, scans subdirectories for monorepos, ignoring submodules and build folders.
    """
    root_pkg_json = Path("package.json")
    if root_pkg_json.exists():
        # Standard repository: 100% backward compatible
        return [Path(".")]

    # Monorepo / subfolder repository (e.g. UnityAutoPerfData)
    ignored_names = {
        ".git",
        ".github",
        ".org-configs",
        "node_modules",
        "Library",
        "Temp",
        "obj",
        "bin",
        "Build",
        "Builds",
        "Samples~",
        "Documentation~",
        "Tests",
    }

    discovered = []
    for pkg_json in Path(".").rglob("package.json"):
        # Ignore files inside system/build directories
        if any(part in ignored_names for part in pkg_json.parts):
            continue

        pkg_dir = pkg_json.parent

        # Ignore submodules (submodules contain their own .git file or folder)
        if (pkg_dir / ".git").exists():
            print(f"--> Skipping submodule folder: '{pkg_dir}'", flush=True)
            continue

        try:
            with open(pkg_json, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                if "name" in data and "version" in data:
                    discovered.append(pkg_dir)
        except Exception as e:
            print(f"--> WARNING: Failed to parse {pkg_json}: {e}", flush=True)

    discovered.sort(key=lambda p: (len(p.parts), str(p)))
    return discovered


def main():
    # 0. Load and sanitize input arguments & environment secrets
    registry_url = os.environ.get("REGISTRY_URL", "").rstrip("/")
    github_ref = os.environ.get("GITHUB_REF_NAME", "")

    verdaccio_token = (
            os.environ.get("REPO_SECRET_VERDACCIO_TOKEN")
            or os.environ.get("VERDACCIO_TOKEN")
            or ""
    ).strip().strip('"').strip("'")

    org_id = (
            os.environ.get("REPO_SECRET_UPM_ORGANIZATION_ID")
            or os.environ.get("UPM_ORGANIZATION_ID")
            or ""
    ).strip().strip('"').strip("'")

    service_account_id = (
            os.environ.get("REPO_SECRET_UPM_SERVICE_ACCOUNT_KEY_ID")
            or os.environ.get("UPM_SERVICE_ACCOUNT_KEY_ID")
            or ""
    ).strip().strip('"').strip("'")

    service_account_secret = (
            os.environ.get("REPO_SECRET_UPM_SERVICE_ACCOUNT_KEY_SECRET")
            or os.environ.get("UPM_SERVICE_ACCOUNT_KEY_SECRET")
            or ""
    ).strip().strip('"').strip("'")

    # Re-export sanitized credentials so child processes inherit them
    if org_id:
        os.environ["UPM_ORGANIZATION_ID"] = org_id
    if service_account_id:
        os.environ["UPM_SERVICE_ACCOUNT_KEY_ID"] = service_account_id
    if service_account_secret:
        os.environ["UPM_SERVICE_ACCOUNT_KEY_SECRET"] = service_account_secret

    if not verdaccio_token:
        print("Error: VERDACCIO_TOKEN is missing from both repository secrets and runner host .env!", file=sys.stderr,
              flush=True)
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

    # 2. Identify Packages to Publish
    packages = find_packages_to_publish()
    if not packages:
        print("Error: No valid package.json found to publish!", file=sys.stderr, flush=True)
        sys.exit(1)

    print(f"--> Found {len(packages)} package(s) to process:", flush=True)
    for p in packages:
        print(f"    * {p}", flush=True)

    # 3. Determine NPM Release Tag based on Git reference
    match = re.search(r"-(preview|alpha|beta|rc)", github_ref)
    if match:
        npm_tag = match.group(1)
    elif re.match(r"^v?\d+\.\d+\.\d+$", github_ref):
        npm_tag = "latest"
    else:
        npm_tag = "preview"
    print(f"--> Assigned NPM publish tag: {npm_tag}", flush=True)

    # 4. Generate Root .npmrc for Verdaccio Authentication
    clean_url = re.sub(r"^https?://", "", registry_url).rstrip("/")
    if ":" in verdaccio_token and not verdaccio_token.startswith("http"):
        print("--> Detected 'username:password' credentials. Encoding to Base64 Basic Auth (_auth)...", flush=True)
        b64_val = base64.b64encode(verdaccio_token.encode("utf-8")).decode("utf-8")
        auth_line = f'//{clean_url}/:_auth="{b64_val}"'
    else:
        print("--> Detected Bearer token. Configuring _authToken...", flush=True)
        auth_line = f'//{clean_url}/:_authToken="{verdaccio_token}"'

    npmrc_path = Path(".npmrc").resolve()
    npmrc_content = f"{auth_line}\n//{clean_url}/:always-auth=true\n"
    npmrc_path.write_text(npmrc_content, encoding="utf-8")

    template_npmignore = Path(".org-configs/templates/.npmignore")
    npm_bin = resolve_binary("npm")
    use_npm_shell = sys.platform == "win32" and npm_bin.lower().endswith((".cmd", ".bat"))
    use_upm_shell = sys.platform == "win32" and upm_path.lower().endswith((".cmd", ".bat"))

    try:
        for idx, pkg_dir in enumerate(packages, 1):
            pkg_json_path = pkg_dir / "package.json"

            # Load package.json with BOM support (utf-8-sig)
            with open(pkg_json_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            pkg_name = data.get("name", "UnknownPackage")
            pkg_version = data.get("version", "0.0.0")

            print(f"\n[{idx}/{len(packages)}] Processing package '{pkg_name}@{pkg_version}' in '{pkg_dir}'...",
                  flush=True)

            # Inject publishConfig into package.json
            if "publishConfig" not in data or not isinstance(data["publishConfig"], dict):
                data["publishConfig"] = {}
            data["publishConfig"]["registry"] = f"{registry_url}/"
            with open(pkg_json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"--> Injected publishConfig ({registry_url}/) into {pkg_json_path}", flush=True)

            # Copy central .npmignore template if none exists in package dir
            dest_npmignore = pkg_dir / ".npmignore"
            if template_npmignore.exists() and not dest_npmignore.exists():
                shutil.copyfile(template_npmignore, dest_npmignore)
                print(f"--> Injected central .npmignore into '{pkg_dir}'", flush=True)

            # Clean existing .tgz files in destination directory
            dest_output_dir = (pkg_dir / "..").resolve()
            for old_tgz in dest_output_dir.glob("*.tgz"):
                try:
                    old_tgz.unlink()
                except Exception:
                    pass

            # Pack the package
            if use_upm:
                pack_cmd = [upm_path, "pack", "."]
                if org_id:
                    pack_cmd.extend(["--organization-id", org_id])
                pack_cmd.extend(["--destination", ".."])

                print(f"--> Packing package with UPM CLI: {upm_path}...", flush=True)
                subprocess.run(pack_cmd, check=True, cwd=str(pkg_dir.resolve()), shell=use_upm_shell)
            else:
                pack_cmd = [npm_bin, "pack", "--pack-destination", ".."]
                print(f"--> Packing package with NPM CLI: {npm_bin}...", flush=True)
                subprocess.run(pack_cmd, check=True, cwd=str(pkg_dir.resolve()), shell=use_npm_shell)

            # Locate generated .tgz
            tgz_files = sorted(dest_output_dir.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not tgz_files:
                print(f"Error: No .tgz package file found in {dest_output_dir} for {pkg_name}!", file=sys.stderr,
                      flush=True)
                sys.exit(1)

            target_tgz = tgz_files[0]
            print(f"--> Publishing {target_tgz.name} to Verdaccio...", flush=True)

            pub_cmd = [
                npm_bin,
                "publish",
                str(target_tgz),
                "--registry",
                f"{registry_url}/",
                "--userconfig",
                str(npmrc_path),
                "--tag",
                npm_tag,
            ]
            pub_result = subprocess.run(pub_cmd, shell=use_npm_shell)

            # Clean up tarball immediately so next package has a clean target directory
            try:
                target_tgz.unlink()
            except Exception:
                pass

            if pub_result.returncode != 0:
                print(f"Error: Failed to publish {pkg_name} to Verdaccio registry.", file=sys.stderr, flush=True)
                sys.exit(pub_result.returncode)

            print(f"--> SUCCESS: Published {pkg_name}@{pkg_version} successfully!", flush=True)

    finally:
        # Cleanup temporary auth & configs
        if npmrc_path.exists():
            npmrc_path.unlink()
        if Path(".org-configs").exists():
            shutil.rmtree(".org-configs", ignore_errors=True)

    print("\n--> All publish steps completed successfully!", flush=True)


if __name__ == "__main__":
    main()
