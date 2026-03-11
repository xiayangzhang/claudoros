"""Entry point for Claudoros."""
from .app import ClaudoroApp


def main() -> None:
    app = ClaudoroApp()
    app.run()


if __name__ == "__main__":
    main()
