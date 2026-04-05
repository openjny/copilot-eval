# Azure Skills Plugin A/B Evaluation

An eval set that measures the impact of adding Azure Skills Plugin (`microsoft/azure-skills`) to Copilot CLI through A/B comparison.

## Overview

Runs Copilot CLI inside Docker containers and compares Azure resource operation tasks across two variants:

| Variant | Description |
|---------|-------------|
| **baseline** | Copilot CLI + Azure CLI (no plugins) |
| **azure-skills** | Copilot CLI + Azure Skills Plugin |

Each task is executed over multiple epochs and scored using LLM-as-Judge + script-based verification.

## Tasks

### 1. compliance-audit

Audit the security and compliance posture of all resources in the resource group.

- **Prompt**: `Audit the security and compliance posture of the resources in resource group {resource_group}...`
- **Evaluators**: verify (script), coverage (judge), finding_accuracy (judge), remediation_quality (judge), methodology (judge)
- **Features demonstrated**: Script evaluator validates actual Azure config + judge evaluators assess audit quality

### 2. app-deploy

Deploy a Node.js Express app to an existing App Service.

- **Prompt**: `I have a simple Node.js Express app in the current directory. Deploy it to the existing Azure App Service...`
- **Fixture**: `fixtures/app-deploy/` (Express app mounted at `/workspace`)
- **Evaluators**: verify (script), deployment_approach (judge), completeness (judge), verification (judge)
- **Features demonstrated**: Fixture mounting + post-deployment HTTP verification

### 3. diagnostics

Diagnose an intentionally broken App Service.

- **Prompt**: `There is an App Service in resource group {resource_group} that seems to be having issues...`
- **before_run hook**: `prepare-diagnostics.sh` (resets environment, then deploys a broken app + sets a wrong startup command)
- **Evaluators**: verify (script), diagnostic_depth (judge), root_cause (judge), actionability (judge), tool_usage (judge)
- **Features demonstrated**: Custom before_run hook to construct a failure scenario

## Directory Structure

```
examples/azure-skills/
├── eval-config.yaml          # Task, variant, and evaluator definitions
├── .env.example              # Azure SP credentials template
├── docker/
│   ├── Dockerfile.baseline   # Variant: Copilot CLI + Azure CLI
│   └── Dockerfile.azure-skills # Variant: + Azure Skills Plugin + MCP
├── infra/
│   ├── main.bicep            # Baseline Azure environment (VNet, App Service, SQL, Storage, ...)
│   └── main.bicepparam.example
├── fixtures/
│   ├── app-deploy/           # Node.js Express app for app-deploy task
│   │   ├── index.js
│   │   └── package.json
│   └── diagnostics/          # Intentionally broken Node.js app for diagnostics task
│       ├── index.js          # require('./config') — module does not exist
│       └── package.json
└── scripts/
    ├── azure-login.sh        # SP login inside container (run script)
    ├── reset-environment.sh  # Reset environment via Bicep Complete mode (shared hook)
    ├── prepare-diagnostics.sh # diagnostics: reset + deploy broken app
    ├── verify-compliance-audit.sh   # compliance-audit verification
    ├── verify-app-deploy.sh         # app-deploy verification
    └── verify-diagnostics.sh        # diagnostics verification
```

## Azure Environment

`infra/main.bicep` deploys the following resources:

- VNet (2 subnets: app + private endpoint)
- App Service Plan (B1) + App Service (Node 20, HTTPS only, VNet integrated)
- Storage Account (private endpoint, public access disabled)
- SQL Server (Entra-only auth) + Database
- Log Analytics + Application Insights
- Private Endpoints (Storage, SQL)

## Prerequisites

1. Create an Azure Service Principal and configure `.env`:
   ```bash
   cp .env.example .env
   # Set AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SUBSCRIPTION_ID
   ```

2. Grant the SP Contributor + SQL Admin permissions on the resource group

3. Build Docker images:
   ```bash
   uv run copilot-eval build --config-dir examples/azure-skills
   ```

4. Start Jaeger:
   ```bash
   docker compose up -d
   ```

## Running

```bash
# Run all tasks (epoch=3)
uv run copilot-eval run --config-dir examples/azure-skills --epochs 3

# Run a single task
uv run copilot-eval run --config-dir examples/azure-skills --task resource-explorer --epochs 3

# Analyze results
uv run copilot-eval analyze --run-id <RUN_ID> -o markdown
```

## Evaluation Methodology

### Scoring

- **Script evaluators** (verify): Pass/Fail — inspect the actual Azure environment (resource existence, HTTP response)
- **Judge evaluators**: 1-10 scale — an LLM evaluates the Copilot CLI output

### Environment Reset

Before each run, `reset-environment.sh` (or `prepare-diagnostics.sh`) resets the Azure environment using Bicep Complete mode deployment. This reverts any resources Copilot created or modified in a previous run, ensuring reproducibility.

### Isolation

Each Copilot CLI execution runs in an isolated Docker container. Containers are ephemeral (disposable), preventing environment contamination between variants.
