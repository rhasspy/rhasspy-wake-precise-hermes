"""Hermes MQTT service for Rhasspy wakeword with Mycroft Precise"""
import argparse
import json
import logging
import os
import sys
import typing
from pathlib import Path

import attr
import paho.mqtt.client as mqtt

from . import WakeHermesMqtt

_DIR = Path(__file__).parent
_LOGGER = logging.getLogger("rhasspywake_precise_hermes")


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspywake_precise_hermes")
    parser.add_argument("--model", help="Precise model file to use (.pb)")
    parser.add_argument("--engine", help="Path to precise-engine executable")
    parser.add_argument(
        "--model-dir",
        action="append",
        default=[],
        help="Directories with Precise models",
    )
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=0.5,
        help="Model sensitivity (default: 0.5)",
    )
    parser.add_argument(
        "--trigger-level",
        type=int,
        default=3,
        help="Activation threshold before prediction (default: 3)",
    )
    parser.add_argument(
        "--wakewordId",
        default="default",
        help="Wakeword ID for model (default: default)",
    )
    parser.add_argument(
        "--log-predictions",
        action="store_true",
        help="Log prediction probabilities for each audio chunk (very verbose)",
    )
    parser.add_argument(
        "--udp-audio-port", type=int, help="Also listen for WAV audio on UDP"
    )
    parser.add_argument(
        "--host", default="localhost", help="MQTT host (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=1883, help="MQTT port (default: 1883)"
    )
    parser.add_argument(
        "--siteId",
        action="append",
        help="Hermes siteId(s) to listen for (default: all)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG messages to the console"
    )
    parser.add_argument(
        "--log-format",
        default="[%(levelname)s:%(asctime)s] %(name)s: %(message)s",
        help="Python logger format",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format=args.log_format)
    else:
        logging.basicConfig(level=logging.INFO, format=args.log_format)

    _LOGGER.debug(args)
    hermes: typing.Optional[WakeHermesMqtt] = None

    try:
        if args.model_dir:
            args.model_dir = [Path(d) for d in args.model_dir]

        # Use embedded models too
        args.model_dir.append(_DIR / "models")

        if not args.model:
            # Use default embedded model
            args.model = _DIR / "models" / "hey-mycroft-2.pb"
        else:
            maybe_model = Path(args.model)
            if not maybe_model.is_file():
                # Resolve against model dirs
                for model_dir in args.model_dir:
                    maybe_model = model_dir / args.model
                    if maybe_model.is_file():
                        break

            args.model = maybe_model

        if not args.engine:
            # Use embedded engine
            args.engine = _DIR / "precise-engine" / "precise-engine"

        # Listen for messages
        client = mqtt.Client()
        hermes = WakeHermesMqtt(
            client,
            args.model,
            args.engine,
            sensitivity=args.sensitivity,
            trigger_level=args.trigger_level,
            wakeword_id=args.wakewordId,
            model_dirs=args.model_dir,
            log_predictions=args.log_predictions,
            udp_audio_port=args.udp_audio_port,
            siteIds=args.siteId,
        )

        hermes.load_engine()

        def on_disconnect(client, userdata, flags, rc):
            try:
                # Automatically reconnect
                _LOGGER.info("Disconnected. Trying to reconnect...")
                client.reconnect()
            except Exception:
                logging.exception("on_disconnect")

        # Connect
        client.on_connect = hermes.on_connect
        client.on_disconnect = on_disconnect
        client.on_message = hermes.on_message

        _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
        client.connect(args.host, args.port)

        client.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.debug("Shutting down")
        if hermes:
            hermes.stop_runner()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
