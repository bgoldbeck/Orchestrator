import os

os.environ.setdefault("ORCHESTRATOR_NO_USAGE_FETCH", "1")  # tests never call the Claude usage endpoint
