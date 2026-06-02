# Zynd Network Wrapper
# This file registers your agent on the Zynd x402 network for discovery and monetization.
import zyndai_agent

def register_agent():
    agent = zyndai_agent.ZyndAgent(
        name="My Agent",
        description="Auto-registered to the Zynd open network."
    )
    agent.deploy()
    print("🚀 Agent is live on ZNS!")

if __name__ == "__main__":
    register_agent()
