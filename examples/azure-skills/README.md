# Azure Skills Plugin A/B Evaluation

Measures the impact of [Azure Skills Plugin](https://github.com/microsoft/azure-skills) on Copilot CLI Azure operations.

| Variant | Description |
|---------|-------------|
| **baseline** | Copilot CLI + Azure CLI |
| **azure-skills** | Copilot CLI + Azure Skills Plugin (MCP) |

## Tasks

- **compliance-audit** — Audit security posture of a resource group
- **app-deploy** — Deploy a Node.js app to App Service
- **diagnostics** — Diagnose a broken App Service

## Prerequisites

1. Azure Service Principal with Contributor on eval resource groups:
   ```bash
   cp .env.example .env
   # Set AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SUBSCRIPTION_ID

   for RG in rg-copilot-eval-compliance rg-copilot-eval-deploy rg-copilot-eval-diag; do
     az group create --name "$RG" --location southeastasia
   done
   ```

2. Deploy infrastructure:
   ```bash
   # See infra/main.bicep for the environment (VNet, App Service, SQL, Storage, etc.)
   ```

## Run

```bash
uv run copilot-eval run --config-dir examples/azure-skills --epochs 3
uv run copilot-eval analyze --run-id <RUN_ID> --config-dir examples/azure-skills -o markdown
```

## Results

Model: claude-sonnet-4, 3 tasks × 2 variants × 3 epochs = 18 runs.

### compliance-audit

| Evaluator | baseline | azure-skills | Δ |
|-----------|:---:|:---:|---|
| coverage | 7 | 7 | 0% |
| accuracy | 5 | **6** | +20% |
| remediation | 6 | 6 | -17% |

Near-parity. azure-skills uses MCP's `azqr` for structured checks but costs 3× more input tokens.

### app-deploy

| Evaluator | baseline | azure-skills | Δ |
|-----------|:---:|:---:|---|
| approach | 5 | **7** | +40% |
| deploy_success | 1 | 1 | 0% |
| post_check | **9** | 1 | -89% |

azure-skills is 2× faster with better approach, but baseline excels at post-deployment verification.

### diagnostics

| Evaluator | baseline | azure-skills | Δ |
|-----------|:---:|:---:|---|
| breadth | 5 | **7** | +40% |
| evidence | 6 | **7** | +17% |
| root_cause | **5** | 2 | -60% |

azure-skills gathers richer data via MCP tools, but baseline is better at pinpointing root cause.

## Cleanup

```bash
for RG in rg-copilot-eval-compliance rg-copilot-eval-deploy rg-copilot-eval-diag; do
  az group delete --name "$RG" --yes --no-wait
done
```
