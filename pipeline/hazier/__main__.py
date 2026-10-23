"""Entry point: `python -m hazier` runs the whole pipeline and writes forecast.json.

See export.py for the steps, the fallback rules and the exit codes.
"""

import sys

from hazier.export import main

if __name__ == "__main__":
    sys.exit(main())
