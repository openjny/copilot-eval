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

2. Create resource groups and grant the SP permissions:
   ```bash
   # Each task uses its own resource group for parallel execution
   SUBSCRIPTION_ID="<your-subscription-id>"
   SP_APP_ID="<your-sp-client-id>"

   for RG in rg-copilot-eval-compliance rg-copilot-eval-deploy rg-copilot-eval-diag; do
     az group create --name "$RG" --location southeastasia
     az role assignment create \
       --assignee "$SP_APP_ID" \
       --role Contributor \
       --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG"
   done

   # SQL Server Entra admin requires Directory Readers or the SP's object ID
   SP_OBJECT_ID=$(az ad sp show --id "$SP_APP_ID" --query id -o tsv)
   ```

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
# Run all tasks in parallel (epoch=3)
uv run copilot-eval run --config-dir examples/azure-skills --epochs 3

# Run a single task
uv run copilot-eval run --config-dir examples/azure-skills --task compliance-audit --epochs 3

# Analyze results
uv run copilot-eval analyze --run-id <RUN_ID> -o markdown
```

## Cleanup

```bash
# Delete all eval resource groups after testing
for RG in rg-copilot-eval-compliance rg-copilot-eval-deploy rg-copilot-eval-diag; do
  az group delete --name "$RG" --yes --no-wait
done
```

## Evaluation Methodology

### Scoring

- **Script evaluators** (verify): Pass/Fail — inspect the actual Azure environment (resource existence, HTTP response)
- **Judge evaluators**: 1-10 scale — an LLM evaluates the Copilot CLI output

### Environment Reset

Before each run, `reset-environment.sh` (or `prepare-diagnostics.sh`) resets the Azure environment using Bicep Complete mode deployment. This reverts any resources Copilot created or modified in a previous run, ensuring reproducibility.

### Isolation

Each Copilot CLI execution runs in an isolated Docker container. Containers are ephemeral (disposable), preventing environment contamination between variants.

## Results

Results from a full eval run (3 tasks × 2 variants × 3 epochs = 18 runs, model: claude-sonnet-4).

### OTel Metrics (median across epochs)

| Metric | azure-skills | baseline | Delta |
|--------|--------:|--------:|------:|
| Duration (s) | 238.7 | 204.0 | -14.5% |
| Turn count | 18 | 26 | **+44.4%** |
| Tool calls | 28 | 36 | **+28.6%** |
| Tool duration (s) | 64.0 | 94.6 | **+47.8%** |
| Input tokens | 1,084K | 751K | -30.7% |
| Output tokens | 6,935 | 5,323 | -23.2% |

### Tool Usage Patterns

**azure-skills** used 14 distinct tool types including Azure MCP tools:

| Tool | Calls | Description |
|------|------:|-------------|
| `bash` | 110 | Shell commands (az CLI) |
| `azure-appservice` | 24 | MCP: App Service operations |
| `view` | 38 | File viewer |
| `azure-monitor` | 15 | MCP: Metrics and logs |
| `create` | 15 | File creation |
| `report_intent` | 13 | Intent reporting |
| `azure-group_resource_list` | 10 | MCP: Resource listing |
| `skill` | 9 | Skill activation |
| `azure-subscription_list` | 7 | MCP: Subscription queries |
| `azure-applens` | 6 | MCP: App diagnostics |
| `azure-resourcehealth` | 6 | MCP: Resource health |
| `azure-extension_azqr` | 5 | MCP: Azure Quick Review |

**baseline** relied primarily on shell commands:

| Tool | Calls |
|------|------:|
| `bash` | 233 |
| `sql` | 24 |
| `report_intent` | 22 |
| `read_bash` | 18 |
| `view` | 10 |

### Judge Scores (median, 1-10 scale)

| Judge | azure-skills | baseline | Winner |
|-------|:-----------:|:--------:|--------|
| deployment_approach | **7** | 2 | azure-skills |
| diagnostic_depth | 2 | **6** | baseline |
| coverage | 1 | **6** | baseline |
| methodology | 2 | **7** | baseline |
| tool_usage | 1 | **6** | baseline |
| finding_accuracy | 1 | **4** | baseline |
| remediation_quality | 1 | **3** | baseline |
| actionability | 1 | **2** | baseline |
| completeness | 2 | 2 | tie |
| root_cause | 1 | 1 | tie |
| verification | 1 | 1 | tie |

### Key Insights

1. **MCP tools are active**: azure-skills uses structured Azure MCP tools (`azure-appservice`, `azure-monitor`, `azure-applens`, `azure-resourcehealth`) instead of raw `az` CLI commands. The baseline variant uses 2× more shell commands (233 vs 110).

2. **Efficiency vs quality trade-off**: azure-skills completes tasks with fewer turns (18 vs 26) and faster tool execution (64s vs 95s), but the quality scores are lower on most judge evaluators.

3. **deployment_approach is the clear win**: azure-skills scores 7 vs 2 on deployment approach — the `azure-prepare` → `azure-validate` → `azure-deploy` workflow stands out.

4. **Compliance/diagnostics favor baseline**: baseline's brute-force approach (many `az` CLI + `sql` commands) produces more thorough audits and diagnostics, despite taking more turns and time.

5. **High variance**: Per-run results show significant variance — azure-skills epoch 1 scored very differently from epoch 3 on the same task. More epochs would improve statistical confidence.
