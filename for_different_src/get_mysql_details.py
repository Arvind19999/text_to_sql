from __future__ import annotations

from get_schema_details import main as schema_main


def main() -> None:
    schema_main(default_driver="mysql")


if __name__ == "__main__":
    main()
