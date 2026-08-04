import argparse

from converter.units import celsius_to_fahrenheit, miles_to_km


def main() -> None:
    parser = argparse.ArgumentParser(prog="converter")
    sub = parser.add_subparsers(dest="command", required=True)

    c2f = sub.add_parser("c2f", help="Celsius to Fahrenheit")
    c2f.add_argument("value", type=float)

    m2km = sub.add_parser("m2km", help="Miles to kilometers")
    m2km.add_argument("value", type=float)

    args = parser.parse_args()

    if args.command == "c2f":
        print(celsius_to_fahrenheit(args.value))
    elif args.command == "m2km":
        print(miles_to_km(args.value))


if __name__ == "__main__":
    main()
