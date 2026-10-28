"""Capture a dialogue crop using the installed native capture source."""

from vntts.dialog_capture import capture_dialog


def main() -> None:
    _, output = capture_dialog()
    print(output)


if __name__ == "__main__":
    main()
