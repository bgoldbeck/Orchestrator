r"""Launch Orchestrator: .venv\Scripts\python run.py"""
from orchestrator import startup

startup.mark("process")
from orchestrator.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
