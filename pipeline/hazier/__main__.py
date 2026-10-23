"""Entry point: `python -m hazier` runs the whole pipeline and writes forecast.json."""

import sys


def main() -> int:
    # TODO: fetch sources, run the trajectory model, compute risk, export forecast.json.
    print("hazier: pipeline not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
