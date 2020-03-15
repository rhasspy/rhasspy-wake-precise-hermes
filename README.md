# Rhasspy Wake Mycroft Precise Hermes

[![GitHub license](https://img.shields.io/github/license/rhasspy/rhasspy-wake-precise-hermes.svg)](https://github.com/rhasspy/rhasspy-wake-precise-hermes/blob/master/LICENSE)

Implements `hermes/hotword` functionality from [Hermes protocol](https://docs.snips.ai/reference/hermes) using [Mycroft Precise](https://github.com/MycroftAI/mycroft-precise).

## Running With Docker

```bash
docker run -it rhasspy/rhasspy-wake-precise-hermes:<VERSION> <ARGS>
```

## Building From Source

Clone the repository and create the virtual environment:

```bash
git clone https://github.com/rhasspy/rhasspy-wake-precise-hermes.git
cd rhasspy-wake-precise-hermes
make venv
```

Run the `bin/rhasspy-wake-precise-hermes` script to access the command-line interface:

```bash
bin/rhasspy-wake-precise-hermes --help
```

## Building the Debian Package

Follow the instructions to build from source, then run:

```bash
source .venv/bin/activate
make debian
```

If successful, you'll find a `.deb` file in the `dist` directory that can be installed with `apt`.

## Building the Docker Image

Follow the instructions to build from source, then run:

```bash
source .venv/bin/activate
make docker
```

This will create a Docker image tagged `rhasspy/rhasspy-wake-precise-hermes:<VERSION>` where `VERSION` comes from the file of the same name in the source root directory.

NOTE: If you add things to the Docker image, make sure to whitelist them in `.dockerignore`.

## Command-Line Options

