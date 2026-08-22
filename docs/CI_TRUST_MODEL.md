# CI trust model

## 結論

Hooklaneのrepository-local CI contractは、workflow、Make target、bootstrap、action pinのaccidentalな弱体化を検出するdefense-in-depthである。同じpull requestはverifierとexpected digestを同時に変更できるため、repository内の自己検証だけをCI control-planeのtrust boundaryとは扱わない。

外部trust boundaryは、base branch上の[CODEOWNERS](../.github/CODEOWNERS)と、GitHub Settingsで人間が有効にするactiveな`main` rulesetの組み合わせである。`Require review from Code Owners`が有効になるまでCODEOWNERSはreview request metadataにすぎず、enforcement済みとは主張しない。

## CI control-plane

control-planeは、workflowから実行または解釈され、command、tool、revision、working tree、gate orderingをredirectできるfileに限定する。

| Protected path | 理由 |
|---|---|
| `/.github/CODEOWNERS` | ownership rule自身の改変 |
| `/.github/workflows/**` | trigger、permissions、job、action、run body |
| `/Makefile` | workflowが呼ぶtargetとdependency chain |
| `/scripts/**` | bootstrap、validator、security／test／E2E orchestration、cleanup |
| `/tests/unit/test_ci_toolchain_contract.py`、`/tests/unit/test_local_image_tag_contract.py` | local structural contractとsource image identityのnegative tests |
| `/.python-version`、`/pyproject.toml`、`/requirements.lock` | quality／E2E bootstrapのruntimeとdependency |
| `/cloudflare/.nvmrc`、`/cloudflare/.python-version` | Cloudflare Node／Python runtime |
| `/cloudflare/package.json`、`/cloudflare/package-lock.json` | Node／Wrangler dependency resolution |
| `/cloudflare/pyproject.toml`、`/cloudflare/uv.lock`、`/cloudflare/pylock.toml` | Python Worker dependencyとruntime resolution |
| `/cloudflare/uv-bootstrap.lock`、`/cloudflare/harness-requirements.lock` | uv bootstrapとroot mock-sink harness |
| `/cloudflare/wrangler.jsonc` | local workerd、D1、Queue bindingとproduction-safe defaults |

`src/`、`cloudflare/src/`、migration、通常のunit／integration test、deployment manifest、docsはPRで検証されるproduct/test inputであり、このcontrol-plane ownershipへ無意味に含めない。rulesetの通常review要件はこれらを含む全pull requestへ適用する。

## Responsibility split

### Repository-local contract

[`scripts/ci_contract.py`](../scripts/ci_contract.py)は次をfail-closedで検査する。

- workflow／job／stepの構造、順序、shell、env、permissions、timeouts
- approved action repositoryとexact commit SHA
- canonical concurrency group
- checkoutしたevent revisionである`GITHUB_SHA`をimage identityに使うrun body
- Cloudflare Make targetの一意性、phony属性、dependencyとrecipe完全一致
- Makefile全体とroot／Cloudflare bootstrap、clean-room、local-flowのapproved source digest
- Cloudflare bootstrapが`GITHUB_PATH`を変更しないこと
- CODEOWNERSのcontrol-plane coverageとself-ownership

この検査はaccidental driftと、review中のM31／M32のような変更を可視化する。digestの更新には意図したsource変更とcontract変更が必要だが、両方を同じPRで変更できるため、それ自体は外部攻撃者に対するtrust boundaryではない。

### GitHub repository governance

GitHubはbase branchのCODEOWNERSを評価する。active rulesetでcode-owner reviewをrequiredにすると、external pull requestは`@sudoworks-lab`の承認なしにcontrol-plane変更をmergeできない。repository ownerはruleset自体を変更またはbypassできるため、ownerの悪意やaccount compromiseはこのmodelの保護対象外である。

## Source identity

`pull_request` workflowのdefault checkout対象はPR merge refであり、`GITHUB_SHA`はそのmerge commitを指す。`push`では`GITHUB_SHA`がpushed commitを指す。qualityとe2e-kindは両eventで`IMAGE_TAG=git-$GITHUB_SHA`を使うため、同じtagが異なるchecked-out contentを表す旧来のPR-head ambiguityは持たない。

PR head SHAはGitHub event／PR UI上のcorrelation metadataとして残るが、build content identityには使用しない。

## Desired GitHub ruleset specification

次のJSONは`Settings > Rules > Rulesets`で人間が設定するdesired stateである。GitHub APIから取得したactual stateではなく、このrunでは未変更・未確認である。

<!-- ci-governance-spec:start -->
```json
{
  "repository": "sudoworks-lab/hooklane",
  "owner_type": "personal_account",
  "ruleset_name": "main-ci-control-plane",
  "enforcement": "active",
  "target": {
    "type": "branch",
    "include": ["main"]
  },
  "bypass": [
    {
      "actor": "repository_administrators",
      "mode": "pull_requests_only"
    }
  ],
  "rules": {
    "restrict_deletions": true,
    "block_force_pushes": true,
    "require_pull_request": true,
    "required_approvals": 1,
    "require_code_owner_review": true,
    "dismiss_stale_approvals": true,
    "require_last_push_approval": false,
    "require_status_checks": true,
    "require_branch_up_to_date": true,
    "required_status_checks": [
      "Quality, security, and chart gates",
      "Cloudflare local backend gate",
      "kind delivery and recovery E2E"
    ]
  }
}
```
<!-- ci-governance-spec:end -->

Personal public repositoryで利用可能なrepository ruleset、required review、CODEOWNERS、status checksだけを前提にする。organization／enterprise専用のrequired workflowは前提にしない。

`repository_administrators`のbypassは`For pull requests only`とし、single ownerが自分のcontrol-plane PRをself-approveできないdeadlockを解消しつつ、direct pushではなくPR記録を残す。これはownerが意図的にbypassできることを明示的に受容するpolicyである。他のactor、collaborator、GitHub Appへbypassを付与しない。

GitHub UIでは次を確認する。

1. ruleset `main-ci-control-plane`を作成し、Enforcement statusを`Active`、targetを`main`にする。
2. Bypass listへ`Repository administrators`だけを`For pull requests only`で追加する。
3. `Restrict deletions`と`Block force pushes`を有効にする。
4. `Require a pull request before merging`を有効にし、approval 1、Code Owner review、stale approval dismissalを有効にする。
5. required status checksへjob ID `quality`、`cloudflare`、`e2e-kind`が報告する上記3つの表示名を選び、branch up-to-dateを必須にする。
6. external test PRで`Makefile`または`/scripts/**`を変更し、owner approvalなしではmerge不能であることを確認する。

### Initial activation order

GitHubはpull requestのbase branchにあるCODEOWNERSを使う。そのため、CODEOWNERSを初めて追加する今回のchange自体は新しいownership ruleでは保護されない。ownerがこのdiffを手動確認し、commit／push／mergeした後に次の順でactivateする。

1. base branch `main`に`.github/CODEOWNERS`と3 jobのworkflowが存在することを確認する。
2. rulesetを`Active`にし、review、deletion、force-push ruleを上記specどおり設定する。
3. 3 check名がUIで未選択なら、branch／PR上でremote workflowを一度実行してcheck contextを出現させる。結果を未実行のままgreen扱いしない。
4. 3 checkをrequiredにし、branch up-to-dateを有効にする。
5. external test PRでcontrol-plane fileを変更し、Code Owner reviewと3 checkがmerge条件になることを確認する。

現在のGitHub機能の根拠:

- [Creating rulesets for a repository](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository): public repositoryでのrulesetと`For pull requests only` bypass
- [About code owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners): base branch CODEOWNERSとrequired Code Owner review
- [About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches): required approvals、stale approval、status checks、force-push／deletion boundary

## Threat model

設定後に保護するもの:

- accidentalなworkflow、action、Make、bootstrapの弱体化
- external PRによるcontrol-plane変更がCode Owner reviewなしでmergeされること
- approved action revision drift、cross-ref concurrency drift
- Make recipeのno-op化、bootstrapからのgate command redirection
- common job／step skip、shell／environment override

保護しないもの:

- repository owner／adminによる意図的なruleset変更またはbypass
- compromised owner account、GitHub platform compromise
- ownerが明示承認したmalicious application／test code
- remote GitHub Actions未実行状態、Cloudflare production、account policy、billing、remote D1／Queues

## Verification status

CODEOWNERSとlocal contractはrepositoryに定義する。GitHub Settingsのruleset、Code Owner enforcement、required checksはこのrepository editから変更・確認できないため、人間が上記checklistを完了するまで`governance specified, enforcement unverified`と扱う。
