# Third-party notices

## 1. Scope

This document identifies third-party software referenced by the Hooklane source repository for transparency. It records repository facts and does not claim to be a complete legal determination. Exact versions and immutable references are maintained in the repository files identified below.

## 2. No vendored third-party source

The repository does not vendor third-party source trees or third-party executable binaries. It contains Hooklane source, configuration, tests, documentation, a generated Python dependency lockfile, and provisioning definitions. Upstream projects are installed, pulled, or invoked by users and CI when they run the documented workflows.

## 3. Python dependencies

Runtime and build dependencies are declared in `pyproject.toml`; the exact direct and transitive package versions are recorded in `requirements.lock`. A local application-image build installs those packages into the image. Package license names and notices should be reviewed from the metadata and license files supplied by each exact upstream package version. This repository does not reproduce those license texts here.

## 4. Container base and runtime images

The Dockerfile references the Python base image. Docker Compose and the Helm chart reference Redis, while the optional observability topology references Prometheus and Grafana. Exact tags and digests are recorded in `Dockerfile`, `compose.yaml`, `charts/hooklane/values.yaml`, `container-policy.json`, and `toolchain.toml`.

These upstream images are not stored in this Git repository. Users pull them or build application images locally. Their upstream licenses and notices apply independently and should be reviewed before redistribution.

## 5. Development and validation tools

The local and CI validation workflow uses kind, Helm, Gitleaks, OSV-Scanner, Trivy, and Kubeconform. Exact tool versions are recorded in `toolchain.toml` and `security-policy.json`. These tools are not vendored or distributed as repository binaries.

## 6. GitHub Actions

The workflow in `.github/workflows/ci.yml` references `actions/checkout`, `actions/setup-python`, and `actions/upload-artifact` by full commit SHA. The action implementations are not vendored in this repository. Review the corresponding upstream revision for its license and notices.

## 7. Distribution note

Hooklane v0.1 is published as source only. The repository does not publish prebuilt container images, container-registry artifacts, release archives, or binary distributions. Building or redistributing dependencies or images may create obligations under their respective upstream licenses; users should review the exact upstream materials for their distribution.

## 8. How to review exact versions

- Python packages: `pyproject.toml` and `requirements.lock`
- Python base image: `Dockerfile`
- Redis, Prometheus, and Grafana images: `compose.yaml`, `charts/hooklane/values.yaml`, and `container-policy.json`
- kind node and validation tools: `toolchain.toml`
- Security scanners: `security-policy.json`
- GitHub Actions: `.github/workflows/ci.yml`

For later revisions, review these files together with the license and notice files published by the exact upstream versions.
