# Central GitHub Organization Configurations

This repository (`SchellGamesInternal/.github`) serves as the central configuration and workflow repository for `SchellGamesInternal`. It hosts reusable GitHub Actions workflows and master template files injected at runtime into organization package repositories.

---

## Repository Structure

```text
.github/
├── .github/
│   └── workflows/
│       └── reusable-upm-publish.yml   # Central UPM pack & publish workflow
└── templates/
    └── .npmignore                     # Master template for UPM packages
```

---

## Key Components

### 1. Master `.npmignore` (`templates/.npmignore`)
Dynamically injected into consumer package repositories before packaging.
* **Excludes:** Local workspace folders (`/Library/`, `/Temp/`), build outputs, IDE solution files, and internal test assemblies (`/Tests/`).
* **Preserves:** All critical Unity YAML `.meta` files required for package integrity.

### 2. Reusable UPM Publishing Workflow (`reusable-upm-publish.yml`)
A centralized GitHub Actions workflow triggered by individual package repositories. It performs the following automated tasks:
1. Clones this configuration repository to fetch master templates.
2. Injects `.npmignore` and dynamically updates `package.json` with the target Verdaccio registry URL.
3. Determines the NPM release tag based on Git tag naming conventions (`preview`, `alpha`, `beta`, `rc`, or `latest`).
4. Packs the Unity package via the host machine's Unity UPM CLI (`C:\upm\bin\upm.exe`).
5. Authenticates and publishes the `.tgz` artifact to the internal Verdaccio registry.

---

## Usage in Package Repositories

Package repositories do not need duplicate CI pipeline code. Add a workflow file at `.github/workflows/publish.yml` in any package repository to reference this pipeline:

```yaml
name: Publish Package

on:
  push:
    tags:
      - 'v*'

jobs:
  publish:
    uses: SchellGamesInternal/.github/.github/workflows/reusable-upm-publish.yml@main
    secrets:
      UPM_SERVICE_ACCOUNT_KEY_ID: ${{ secrets.UPM_SERVICE_ACCOUNT_KEY_ID }}
      UPM_SERVICE_ACCOUNT_KEY_SECRET: ${{ secrets.UPM_SERVICE_ACCOUNT_KEY_SECRET }}
      UPM_ORGANIZATION_ID: ${{ secrets.UPM_ORGANIZATION_ID }}
      VERDACCIO_TOKEN: ${{ secrets.VERDACCIO_TOKEN }}
```

---

## Prerequisites & Required Secrets

This workflow relies on self-hosted runners labeled `[self-hosted, Windows, X64, upm-runner]` and requires the following Organization-level secrets to be configured in GitHub:

| Secret Name | Description |
| :--- | :--- |
| `UPM_SERVICE_ACCOUNT_KEY_ID` | Service Account ID for Unity UPM CLI authentication. |
| `UPM_SERVICE_ACCOUNT_KEY_SECRET` | Service Account Key Secret for Unity UPM CLI. |
| `UPM_ORGANIZATION_ID` | Schell Games Unity Organization ID. |
| `VERDACCIO_TOKEN` | Authentication token for the private Verdaccio package registry. |

> **Permissions Note:** Ensure Organization Settings > **Actions** > **General** has **Workflow permissions** set to *"Read and write permissions"* and **Access** set to *"Accessible from repositories in the 'SchellGamesInternal' organization"* to allow cross-repository workflow access.