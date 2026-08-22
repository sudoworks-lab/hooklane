# Third-party notices

## 対象

この文書は、Hooklane source repositoryが参照する第三者softwareを整理する。repository内で確認できる事実を記録するものであり、完全な法的判断を示すものではない。exact versionとimmutable referenceは、以下のrepository fileを正本とする。

## vendored third-party sourceなし

このrepositoryは第三者source treeや実行binaryをvendorしない。Hooklaneのsource、configuration、test、documentation、generated Python dependency lockfile、provisioning definitionを含む。上流projectは、文書化されたworkflowの実行時に利用者またはCIがinstall、pull、invokeする。

## Python dependency

runtimeとbuild dependencyは[pyproject.toml](pyproject.toml)に、direct／transitive packageのexact versionは[requirements.lock](requirements.lock)に記録する。local application-image buildはこれらのpackageをimageへinstallする。license名とnoticeは各exact upstream package versionに付属するmetadataとlicense fileで確認する。このrepositoryはそれらのlicense本文を転載しない。

## container baseとruntime image

DockerfileはPython base imageを参照する。Docker ComposeとHelm chartはRedisを参照し、任意のobservability構成はPrometheusとGrafanaを参照する。exact tagとdigestは[Dockerfile](Dockerfile)、[compose.yaml](compose.yaml)、[charts/hooklane/values.yaml](charts/hooklane/values.yaml)、[container-policy.json](container-policy.json)、[toolchain.toml](toolchain.toml)に記録する。

これらのupstream imageはGit repositoryに保存しない。利用者がpullするかapplication imageをlocal buildする。redistribution前には各上流のlicenseとnoticeを確認する。

## 開発と検証tool

localとCIの検証workflowはTerraform、kind、Helm、Gitleaks、OSV-Scanner、Trivy、Kubeconformを使う。exact tool versionは[toolchain.toml](toolchain.toml)と[security-policy.json](security-policy.json)に記録する。これらのtoolをrepository binaryとして配布しない。

Terraform v1.15.5はHashiCorp公式の`terraform_1.15.5_linux_amd64.zip`を`~/.local/bin`へ取得する。CI installerは同じ公式archiveのSHA-256を、対応する`terraform_1.15.5_SHA256SUMS`から選択して検証してから展開し、sudoやsystem-global installを使わない。確認対象は[toolchain.toml](toolchain.toml)と[scripts/install_ci_tools.py](scripts/install_ci_tools.py)である。

## Cloudflare local spike

local-only Cloudflare spikeはPython Workers runtime SDK、FastAPI、Pydantic、Pywrangler、Wrangler／workerdを参照する。Python側のdirect dependencyとhost lockは[cloudflare/pyproject.toml](cloudflare/pyproject.toml)と[cloudflare/uv.lock](cloudflare/uv.lock)、Pyodide runtime wheelは[cloudflare/pylock.toml](cloudflare/pylock.toml)、Node.jsとWranglerは[cloudflare/package.json](cloudflare/package.json)、[cloudflare/package-lock.json](cloudflare/package-lock.json)、[cloudflare/.nvmrc](cloudflare/.nvmrc)を正本とする。CI bootstrapのuv wheelとroot mock sink subsetは[cloudflare/uv-bootstrap.lock](cloudflare/uv-bootstrap.lock)と[cloudflare/harness-requirements.lock](cloudflare/harness-requirements.lock)へ固定する。

dependency、`python_modules`、Wrangler、workerd、local D1 stateはrepositoryへvendorせず、local validation時にlockからinstallまたは生成する。Cloudflare account、credential、remote resource、cloud deploymentはこのvalidationに使用しない。license名とnoticeは各lockが示すexact upstream versionで確認する。

## GitHub Actions

[.github/workflows/ci.yml](.github/workflows/ci.yml)は`actions/checkout`、`actions/setup-node`、`actions/setup-python`、`actions/upload-artifact`をapproved revisionのexact commit SHAで参照し、local CI contractもrepository名とSHAの組を完全一致で検証する。action implementationはrepositoryへvendorしない。licenseとnoticeは該当するupstream revisionで確認する。

## 配布に関する注記

Hooklane v0.1.1はsource-onlyで公開している。prebuilt container image、container-registry artifact、release archive、binary distributionは配布しない。dependencyやimageのbuildまたはredistributionには各上流licenseに基づく義務が生じ得るため、exact upstream materialを確認する。

## exact versionの確認方法

- Python package: [pyproject.toml](pyproject.toml)と[requirements.lock](requirements.lock)
- Python base image: [Dockerfile](Dockerfile)
- Redis、Prometheus、Grafana image: [compose.yaml](compose.yaml)、[charts/hooklane/values.yaml](charts/hooklane/values.yaml)、[container-policy.json](container-policy.json)
- kind nodeとvalidation tool: [toolchain.toml](toolchain.toml)
- Terraform CLI: [toolchain.toml](toolchain.toml)、[scripts/install_ci_tools.py](scripts/install_ci_tools.py)、HashiCorp公式archiveとSHA256SUMS
- security scanner: [security-policy.json](security-policy.json)
- Cloudflare Python／Pyodide package: [cloudflare/pyproject.toml](cloudflare/pyproject.toml)、[cloudflare/uv.lock](cloudflare/uv.lock)、[cloudflare/pylock.toml](cloudflare/pylock.toml)
- Cloudflare Node.js／Wrangler tool: [cloudflare/package.json](cloudflare/package.json)、[cloudflare/package-lock.json](cloudflare/package-lock.json)、[cloudflare/.nvmrc](cloudflare/.nvmrc)
- GitHub Actions: [.github/workflows/ci.yml](.github/workflows/ci.yml)

後続revisionでは、これらのfileとexact upstream versionのlicense、noticeを合わせて確認する。
