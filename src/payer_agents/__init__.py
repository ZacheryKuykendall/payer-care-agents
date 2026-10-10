"""Payer Care Agents: a governed multi-agent reference architecture for health plans.

Everything in this package runs in one of two modes:

* ``mock``  - no Azure, no network, deterministic. Used by the demo, the tests and CI.
* ``azure`` - the same interfaces backed by Microsoft Foundry, Azure AI Search and
  an Azure API Management-fronted MCP tool server.

The split is enforced by :mod:`payer_agents.config`; application code depends on
protocols, never on a concrete client.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
