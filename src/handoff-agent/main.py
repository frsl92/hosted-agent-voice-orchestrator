"""Entry point: hosts the call-center agent over the Responses protocol (port 8088)."""

import logging

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from host_agent import CallCenterAgent  # noqa: E402
from voice_server import VoiceResponsesHostServer  # noqa: E402

if __name__ == "__main__":
    VoiceResponsesHostServer(CallCenterAgent(), history_source="agent").run()
