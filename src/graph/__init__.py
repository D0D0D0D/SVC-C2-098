"""AgentCore Platform v1.0"""

# Re-export retained for older direct imports. AgentRegistry now uses the
# root-level dotted class path in config/agent.yaml.
from src.graph.graph import SlaRcaRebuttalBriefAgent

__all__ = ["SlaRcaRebuttalBriefAgent"]
