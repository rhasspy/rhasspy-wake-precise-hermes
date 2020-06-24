"""Hermes MQTT service for Rhasspy wakeword with Mycroft Precise"""
import argparse
import asyncio
import logging
import os
import shutil
import typing
from pathlib import Path

import paho.mqtt.client as mqtt
import rhasspyhermes.cli as hermes_cli

from . import WakeHermesMqtt

_DIR = Path(__file__).parent
_LOGGER = logging.getLogger("rhasspywake_precise_hermes")

# -----------------------------------------------------------------------------


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspy-wake-precise-hermes")
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
        "--wakeword-id",
        default="",
        help="Wakeword ID for model (default: use file name)",
    )
    parser.add_argument(
        "--log-predictions",
        action="store_true",
        help="Log prediction probabilities for each audio chunk (very verbose)",
    )
    parser.add_argument(
        "--udp-audio",
        nargs=3,
        action="append",
        help="Host/port/siteId for UDP audio input",
    )

    hermes_cli.add_hermes_args(parser)
    args = parser.parse_args()

    hermes_cli.setup_logging(args)
    _LOGGER.debug(args)
    hermes: typing.Optional[WakeHermesMqtt] = None

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
        # Check for environment variable
        if "PRECISE_ENGINE_DIR" in os.environ:
            args.engine = Path(os.environ["PRECISE_ENGINE_DIR"]) / "precise-engine"
        else:
            # Look in PATH
            maybe_engine = shutil.which("precise-engine")
            if maybe_engine:
                # Use in PATH
                args.engine = Path(maybe_engine)
            else:
                # Use embedded engine
                args.engine = _DIR / "precise-engine" / "precise-engine"

    _LOGGER.debug("Using engine at %s", str(args.engine))

    udp_audio = []
    if args.udp_audio:
        udp_audio = [
            (host, int(port), site_id) for host, port, site_id in args.udp_audio
        ]

    # Listen for messages
    client = mqtt.Client()
    hermes = WakeHermesMqtt(
        client,
        args.model,
        args.engine,
        sensitivity=args.sensitivity,
        trigger_level=args.trigger_level,
        wakeword_id=args.wakeword_id,
        model_dirs=args.model_dir,
        log_predictions=args.log_predictions,
        udp_audio=udp_audio,
        site_ids=args.site_id,
    )

    hermes.load_engine()

    _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
    hermes_cli.connect(client, args)
    client.loop_start()

    try:
        # Run event loop
        asyncio.run(hermes.handle_messages_async())
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.debug("Shutting down")
        client.loop_stop()
        if hermes:
            hermes.stop_runner()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
