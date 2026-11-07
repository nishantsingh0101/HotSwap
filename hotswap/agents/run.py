"""Entry point: python -m hotswap.agents.run builder|standby|verifier|scrubber"""
import sys

from hotswap.agents.base import run_agent


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "builder":
        from hotswap.agents.builder import Builder as cls
    elif role == "standby":
        from hotswap.agents.builder import Standby as cls
    elif role == "verifier":
        from hotswap.agents.verifier import Verifier as cls
    elif role == "scrubber":
        from hotswap.agents.scrubber import Scrubber as cls
    else:
        raise SystemExit("usage: python -m hotswap.agents.run builder|standby|verifier|scrubber")
    run_agent(cls)


if __name__ == "__main__":
    main()
