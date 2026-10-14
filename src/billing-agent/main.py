"""Entry point: hosts the billing specialist over the Responses protocol (port 8088)."""

import logging

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from agent_framework_foundry_hosting import ResponsesHostServer  # noqa: E402

import caller_scope  # noqa: E402,F401  (relaxes the end-user requirement for agent-to-agent calls)
from agent import build_agent  # noqa: E402

if __name__ == "__main__":
    ResponsesHostServer(build_agent()).run()
