# Rhasspy Wake Mycroft Precise Hermes

[![Continous Integration](https://github.com/rhasspy/rhasspy-wake-precise-hermes/workflows/Tests/badge.svg)](https://github.com/rhasspy/rhasspy-wake-precise-hermes/actions)
[![GitHub license](https://img.shields.io/github/license/rhasspy/rhasspy-wake-precise-hermes.svg)](https://github.com/rhasspy/rhasspy-wake-precise-hermes/blob/master/LICENSE)

Implements `hermes/hotword` functionality from [Hermes protocol](https://docs.snips.ai/reference/hermes) using [Mycroft Precise](https://github.com/MycroftAI/mycroft-precise).

## Requirements

* Python 3.7
* [Mycroft Precise](https://github.com/MycroftAI/mycroft-precise)
    * Expects `precise-engine` in `$PATH`

## Installation

```bash
$ git clone https://github.com/rhasspy/rhasspy-wake-precise-hermes
$ cd rhasspy-wake-precise-hermes
$ ./configure
$ make
$ make install
```

## Running

```bash
$ bin/rhasspy-wake-precise-hermes <ARGS>
```

## Command-Line Options

```
usage: rhasspy-wake-precise-hermes [-h] [--model MODEL] [--engine ENGINE]
                                   [--model-dir MODEL_DIR]
                                   [--sensitivity SENSITIVITY]
                                   [--trigger-level TRIGGER_LEVEL]
                                   [--wakeword-id WAKEWORD_ID]
                                   [--log-predictions]
                                   [--udp-audio UDP_AUDIO UDP_AUDIO UDP_AUDIO]
                                   [--host HOST] [--port PORT]
                                   [--username USERNAME] [--password PASSWORD]
                                   [--tls] [--tls-ca-certs TLS_CA_CERTS]
                                   [--tls-certfile TLS_CERTFILE]
                                   [--tls-keyfile TLS_KEYFILE]
                                   [--tls-cert-reqs {CERT_REQUIRED,CERT_OPTIONAL,CERT_NONE}]
                                   [--tls-version TLS_VERSION]
                                   [--tls-ciphers TLS_CIPHERS]
                                   [--site-id SITE_ID] [--debug]
                                   [--log-format LOG_FORMAT]

optional arguments:
  -h, --help            show this help message and exit
  --model MODEL         Precise model file to use (.pb)
  --engine ENGINE       Path to precise-engine executable
  --model-dir MODEL_DIR
                        Directories with Precise models
  --sensitivity SENSITIVITY
                        Model sensitivity (default: 0.5)
  --trigger-level TRIGGER_LEVEL
                        Activation threshold before prediction (default: 3)
  --wakeword-id WAKEWORD_ID
                        Wakeword ID for model (default: use file name)
  --log-predictions     Log prediction probabilities for each audio chunk
                        (very verbose)
  --udp-audio UDP_AUDIO UDP_AUDIO UDP_AUDIO
                        Host/port/siteId for UDP audio input
  --host HOST           MQTT host (default: localhost)
  --port PORT           MQTT port (default: 1883)
  --username USERNAME   MQTT username
  --password PASSWORD   MQTT password
  --tls                 Enable MQTT TLS
  --tls-ca-certs TLS_CA_CERTS
                        MQTT TLS Certificate Authority certificate files
  --tls-certfile TLS_CERTFILE
                        MQTT TLS certificate file (PEM)
  --tls-keyfile TLS_KEYFILE
                        MQTT TLS key file (PEM)
  --tls-cert-reqs {CERT_REQUIRED,CERT_OPTIONAL,CERT_NONE}
                        MQTT TLS certificate requirements (default:
                        CERT_REQUIRED)
  --tls-version TLS_VERSION
                        MQTT TLS version (default: highest)
  --tls-ciphers TLS_CIPHERS
                        MQTT TLS ciphers to use
  --site-id SITE_ID     Hermes site id(s) to listen for (default: all)
  --debug               Print DEBUG messages to the console
  --log-format LOG_FORMAT
                        Python logger format
```
