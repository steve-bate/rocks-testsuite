# rocks-testsuite

ActivityPub test suite resurrected from original [Guile](https://www.gnu.org/software/guile/) code and rewritten in Python.

Original code: https://gitlab.com/dustyweb/pubstrate/-/blob/master/pubstrate/aptestsuite.scm

**This project is for experimentation and demonstration only. Do not use it to submit test results!**

## Install

This project uses the [poetry](https://python-poetry.org/docs/#installation) dependency management and packaging tool. If you don't have it, you'll need to install that before the next steps.

```
git clone https://github.com/steve-bate/rocks-testsuite
cd rocks-testsuite
poetry install
```

## Usage

```
poetry run rocks  # or use poetry shell
```

The server does not use SSL so if you need that you'll need to set up a reverse proxy or a secure tunnel.

## Implementation Notes

The web application uses web sockets and a small Javascript program to send information to the browser and receive form submissions results.

The original code has some type of checkpointing support and back button simulation, but that's not implemented at the moment in this version.

The application is mostly emulating the original application. There are many areas for potential improvement.

## License

GNU General Public License, version 3 or later (because that's how the original code is licensed).
