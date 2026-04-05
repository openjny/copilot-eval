# Azure Skills Plugin A/B Evaluation

Azure Skills Plugin (`microsoft/azure-skills`) を Copilot CLI に追加することによる効果を A/B 評価する eval set。

## Overview

Copilot CLI を Docker コンテナ内で実行し、Azure リソースに対する操作タスクを 2 つの variant で比較する:

| Variant | Description |
|---------|-------------|
| **baseline** | Copilot CLI + Azure CLI (プラグインなし) |
| **azure-skills** | Copilot CLI + Azure Skills Plugin |

各タスクを複数 epoch 実行し、LLM-as-Judge + スクリプト検証でスコアリングする。

## Tasks

### 1. resource-explorer

RG 内の全リソースを一覧し、アーキテクチャを説明するタスク。

- **Prompt**: `List all resources in resource group {resource_group} and explain the architecture`
- **Evaluators**: verify (script), completeness (judge), architecture_accuracy (judge), actionability (judge)
- **Features demonstrated**: Script evaluator + Judge evaluator の組み合わせ

### 2. app-deploy

Node.js Express アプリを既存の App Service にデプロイするタスク。

- **Prompt**: `I have a simple Node.js Express app in the current directory. Deploy it to the existing Azure App Service...`
- **Fixture**: `fixtures/app-deploy/` (Express アプリ — `/workspace` にマウント)
- **Evaluators**: verify (script), deployment_approach (judge), completeness (judge), verification (judge)
- **Features demonstrated**: Fixture マウント + デプロイ後の HTTP 検証

### 3. diagnostics

意図的に壊れた App Service を診断するタスク。

- **Prompt**: `There is an App Service in resource group {resource_group} that seems to be having issues...`
- **before_run hook**: `prepare-diagnostics.sh` (環境リセット後、壊れたアプリをデプロイ + 誤った startup command を設定)
- **Evaluators**: verify (script), diagnostic_depth (judge), root_cause (judge), actionability (judge), tool_usage (judge)
- **Features demonstrated**: カスタム before_run hook で障害シナリオを構築

## Directory Structure

```
examples/azure-skills/
├── eval-config.yaml          # Task, variant, evaluator 定義
├── .env.example              # Azure SP credentials テンプレート
├── infra/
│   ├── main.bicep            # ベースライン Azure 環境 (VNet, App Service, SQL, Storage, ...)
│   └── main.bicepparam.example
├── fixtures/
│   ├── app-deploy/           # app-deploy 用 Node.js Express アプリ
│   │   ├── index.js
│   │   └── package.json
│   └── diagnostics/          # diagnostics 用 壊れた Node.js アプリ
│       ├── index.js          # require('./config') — 存在しないモジュール
│       └── package.json
└── scripts/
    ├── azure-login.sh        # コンテナ内 SP ログイン (run script)
    ├── build-baseline.sh     # baseline variant ビルド (noop)
    ├── build-azure-skills.sh # azure-skills variant ビルド (plugin install)
    ├── reset-environment.sh  # Bicep Complete mode で環境リセット (shared hook)
    ├── prepare-diagnostics.sh # diagnostics 用: リセット + 壊れたアプリデプロイ
    ├── verify-resource-explorer.sh  # resource-explorer 検証
    ├── verify-app-deploy.sh         # app-deploy 検証
    └── verify-diagnostics.sh        # diagnostics 検証
```

## Azure Environment

`infra/main.bicep` で以下のリソースをデプロイ:

- VNet (2 subnets: app + private endpoint)
- App Service Plan (B1) + App Service (Node 20, HTTPS only, VNet integrated)
- Storage Account (private endpoint, public access disabled)
- SQL Server (Entra-only auth) + Database
- Log Analytics + Application Insights
- Private Endpoints (Storage, SQL)

## Prerequisites

1. Azure Service Principal の作成と `.env` 設定:
   ```bash
   cp .env.example .env
   # AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SUBSCRIPTION_ID を設定
   ```

2. SP に RG の Contributor + SQL Admin 権限を付与

3. Docker イメージのビルド:
   ```bash
   uv run copilot-eval build --config-dir examples/azure-skills
   ```

4. Jaeger の起動:
   ```bash
   docker compose up -d
   ```

## Running

```bash
# 全タスク実行 (epoch=3)
uv run copilot-eval run --config-dir examples/azure-skills --epochs 3

# 単一タスク
uv run copilot-eval run --config-dir examples/azure-skills --task resource-explorer --epochs 3

# 結果分析
uv run copilot-eval analyze --run-id <RUN_ID> -o markdown
```

## Evaluation Methodology

### Scoring

- **Script evaluators** (verify): Pass/Fail — 実際の Azure 環境を検査 (リソース存在確認, HTTP レスポンス確認)
- **Judge evaluators**: 1-10 スケール — Copilot CLI の出力を LLM が評価

### Environment Reset

各 run の前に `reset-environment.sh` (or `prepare-diagnostics.sh`) が実行され、Bicep Complete mode で Azure 環境をリセット。これにより前の run で Copilot が作成/変更したリソースが元に戻り、再現性を確保。

### Isolation

各 Copilot CLI 実行は独立した Docker コンテナ内で行われる。コンテナは使い捨て (ephemeral) で、variant 間の環境汚染を防ぐ。
