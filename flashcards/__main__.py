"""Entry point for ``python -m flashcards``."""

from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
