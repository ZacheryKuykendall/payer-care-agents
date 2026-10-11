# Infrastructure

Bicep for the Azure side of the reference architecture, plus the API Management
policy XML that is the actual control point between an agent and a clinical system.

Nothing here is required to run the demo. `make demo` and `make serve` run entirely
offline. This directory is the answer to "and how would you actually deploy it".

## Layout

```
main.bicep                       resource-group scope entry point
main.bicepparam                  development parameters
generated/egress-rules.json      GENERATED from data/policy/egress-policy.json
modules/
  monitoring.bicep               Log Analytics, App Insights, Event Hubs audit sink
  foundry.bicep                  Foundry account, project, three model deployments
  rai-egress-policy.bicep        PREVIEW outbound network egress controls
  search.bicep                   Azure AI Search
  fhir.bicep                     Health Data Services FHIR R4 service
  key-vault.bicep                Key Vault (deliberately almost empty)
  apim.bicep                     API Management, backends, loggers, policies
  rbac.bicep                     least-privilege role assignments
apim/policies/
  global.xml                     all-APIs scope: correlation id, CORS, IP filter
  mcp-server.xml                 MCP server scope: the full governance chain
  fhir-api.xml                   FHIR API scope: read-only and member scoping
```

## Deploy

```powershell
az login
az account set --subscription "<subscription>"
az group create --name rg-payer-agents-dev --location eastus2

python scripts/export_egress_policy.py
az deployment group create `
  --resource-group rg-payer-agents-dev `
  --template-file infra/main.bicep `
  --parameters infra/main.bicepparam
```

```bash
az login
az account set --subscription "<subscription>"
az group create --name rg-payer-agents-dev --location eastus2

python scripts/export_egress_policy.py
az deployment group create \
  --resource-group rg-payer-agents-dev \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

Validate without deploying:

```bash
az deployment group what-if \
  --resource-group rg-payer-agents-dev \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

## What the template does not do

Stated plainly, because an architecture document that omits its own gaps is a
brochure.

**It does not create the MCP server.** In API Management an MCP server is built from
an existing API by selecting which operations become tools. That selection is a
governance decision -- which capabilities an agent is allowed to have -- and
expressing it as a template parameter invites someone to widen it in a parameter file
nobody reviews. Create it in the portal under **APIs → MCP Servers → Create MCP
server → Expose an API as an MCP server**, select the read operations only, then
paste `apim/policies/mcp-server.xml` into **MCP → Policies**.

**It does not define the search index schemas.** Index definitions change far more
often than infrastructure. They belong to the ingestion job, not to a deployment that
also owns the FHIR service.

**It does not configure private endpoints or the virtual network.** Public network
access is disabled on the data services, which is the half of the story that belongs
in an application template. The network itself -- address space, DNS zones, firewall,
peering -- belongs to the platform team's landing zone, and a template that invents
its own address space is a template that cannot be deployed into a real enterprise.
`docs/ARCHITECTURE.md` describes the expected topology and which private DNS zones
each service needs.

**It does not deploy an agent.** `docs/PREVIEW-STATUS.md` covers hosting an Agent
Framework agent on Foundry and what is preview about it today.

## Before you trust the API versions

Every `apiVersion` here was chosen against documentation current when this was
written, and resource provider versions move. Check them before a real deployment:

```bash
az provider show --namespace Microsoft.CognitiveServices \
  --query "resourceTypes[?resourceType=='accounts'].apiVersions[0:5]" -o tsv
```

The one to watch is `Microsoft.CognitiveServices/accounts/raiPolicies@2026-05-15-preview`.
Preview API versions are withdrawn, not deprecated.

## Rollout order for the egress policy

1. Deploy with `egressMode = 'Audit'`.
2. Exercise every path the agent uses: retrieval, model inference, tool calls,
   telemetry.
3. Query Application Insights for the `Network egress decision` records and confirm
   that nothing legitimate appears as would-deny.
4. Redeploy with `egressMode = 'Enforced'`.
5. Re-run the probe and confirm a denied destination now returns a proxy 403.

Skipping step 3 is how an egress policy takes a production agent offline on a Friday.
