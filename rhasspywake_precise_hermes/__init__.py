"""Hermes MQTT server for Rhasspy wakeword with Mycroft Precise"""
import io
import json
import logging
import queue
import socket
import subprocess
import threading
import typing
import wave
from pathlib import Path

import attr
from precise_runner import PreciseEngine, PreciseRunner, ReadWriteStream
from rhasspyhermes.audioserver import AudioFrame
from rhasspyhermes.base import Message
from rhasspyhermes.wake import (
    GetHotwords,
    Hotword,
    HotwordDetected,
    HotwordError,
    Hotwords,
    HotwordToggleOff,
    HotwordToggleOn,
)

WAV_HEADER_BYTES = 44
_LOGGER = logging.getLogger("rhasspywake_precise_hermes")

# -----------------------------------------------------------------------------


class WakeHermesMqtt:
    """Hermes MQTT server for Rhasspy wakeword with Mycroft Precise."""

    def __init__(
        self,
        client,
        model_path: Path,
        engine_path: Path,
        sensitivity: float = 0.5,
        trigger_level: int = 3,
        wakeword_id: str = "default",
        model_dirs: typing.Optional[typing.List[Path]] = None,
        siteIds: typing.Optional[typing.List[str]] = None,
        enabled: bool = True,
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        chunk_size: int = 2048,
        udp_audio_port: typing.Optional[int] = None,
        udp_chunk_size: int = 2048,
        log_predictions: bool = False,
    ):
        self.client = client

        self.model_path = model_path
        self.engine_path = engine_path
        self.sensitivity = sensitivity
        self.trigger_level = trigger_level

        self.wakeword_id = wakeword_id
        self.model_dirs = model_dirs or []

        self.siteIds = siteIds or []
        self.enabled = enabled

        # Required audio format
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels

        self.chunk_size = chunk_size

        # Queue of WAV audio chunks to process (plus siteId)
        self.wav_queue: queue.Queue = queue.Queue()

        # Listen for raw audio on UDP too
        self.udp_audio_port = udp_audio_port
        self.udp_chunk_size = udp_chunk_size

        # siteId used for detections from UDP
        self.udp_siteId = "default" if not self.siteIds else self.siteIds[0]

        # Topics to listen for WAV chunks on
        self.audioframe_topics: typing.List[str] = []
        for siteId in self.siteIds:
            self.audioframe_topics.append(AudioFrame.topic(siteId=siteId))

        self.first_audio: bool = True

        self.audio_buffer = bytes()

        self.engine: typing.Optional[PreciseEngine] = None
        self.engine_stream: typing.Optional[ReadWriteStream] = None
        self.runner: typing.Optional[PreciseRunner] = None
        self.last_audio_siteId: str = "default"
        self.modelId = self.model_path.name
        self.log_predictions = log_predictions

    # -------------------------------------------------------------------------

    def load_engine(self):
        """Load Precise engine and model."""
        if self.engine is None:
            _LOGGER.debug("Loading Precise engine at %s", self.engine_path)
            self.engine = PreciseEngine(
                self.engine_path, self.model_path, chunk_size=self.chunk_size
            )

            assert self.engine is not None
            self.engine_stream = ReadWriteStream()

        if self.log_predictions:

            def on_prediction(prob: float):
                _LOGGER.debug("Prediction: %s", prob)

        else:

            def on_prediction(prob: float):
                pass

        if self.runner is None:
            self.runner = PreciseRunner(
                self.engine,
                stream=self.engine_stream,
                sensitivity=self.sensitivity,
                trigger_level=self.trigger_level,
                on_activation=self.handle_detection,
                on_prediction=on_prediction,
            )

            assert self.runner is not None
            self.runner.start()

        _LOGGER.debug(
            "Loaded Mycroft Precise (model=%s, sensitivity=%s, trigger_level=%s)",
            self.model_path,
            self.sensitivity,
            self.trigger_level,
        )

    def stop_runner(self):
        """Stop Precise runner."""
        if self.runner:
            self.runner.stop()
            self.runner = None

    # -------------------------------------------------------------------------

    def handle_audio_frame(self, wav_bytes: bytes, siteId: str = "default"):
        """Process a single audio frame"""
        self.wav_queue.put((wav_bytes, siteId))

    def handle_detection(self):
        """Handle a successful hotword detection"""
        try:
            self.publish(
                HotwordDetected(
                    siteId=self.last_audio_siteId,
                    modelId=self.modelId,
                    currentSensitivity=self.sensitivity,
                    modelVersion="",
                    modelType="personal",
                )
            )
        except Exception as e:
            _LOGGER.exception("handle_detection")
            self.publish(
                HotwordError(
                    error=str(e),
                    context=str(self.model_path),
                    siteId=self.last_audio_siteId,
                )
            )

    def handle_get_hotwords(
        self, get_hotwords: GetHotwords
    ) -> typing.Union[Hotwords, HotwordError]:
        """Report available hotwords"""
        try:
            if self.model_dirs:
                # Add all models from model dirs
                model_paths = []
                for model_dir in self.model_dirs:
                    if not model_dir.is_dir():
                        _LOGGER.warning("Model directory missing: %s", str(model_dir))
                        continue

                    for model_file in model_dir.iterdir():
                        if model_file.is_file() and (model_file.suffix == ".pb"):
                            model_paths.append(model_file)
            else:
                # Add current model
                model_paths = [self.model_path]

            hotword_models: typing.List[Hotword] = []
            for model_path in model_paths:
                model_words = " ".join(model_path.with_suffix("").name.split("_"))

                hotword_models.append(
                    Hotword(
                        modelId=model_path.name,
                        modelWords=model_words,
                        modelType="personal",
                    )
                )

            return Hotwords(
                models={m.modelId: m for m in hotword_models},
                id=get_hotwords.id,
                siteId=get_hotwords.siteId,
            )

        except Exception as e:
            _LOGGER.exception("handle_get_hotwords")
            return HotwordError(
                error=str(e), context=str(get_hotwords), siteId=get_hotwords.siteId
            )

    def detection_thread_proc(self):
        """Handle WAV audio chunks."""
        try:
            while True:
                wav_bytes, siteId = self.wav_queue.get()
                self.last_audio_siteId = siteId

                if not self.engine:
                    self.load_engine()

                # Extract/convert audio data
                audio_data = self.maybe_convert_wav(wav_bytes)

                # Add to persistent buffer
                self.audio_buffer += audio_data

                # Process in chunks.
                # Any remaining audio data will be kept in buffer.
                while len(self.audio_buffer) >= self.chunk_size:
                    chunk = self.audio_buffer[: self.chunk_size]
                    self.audio_buffer = self.audio_buffer[self.chunk_size :]

                    if chunk:
                        self.engine_stream.write(chunk)
        except Exception:
            _LOGGER.exception("detection_thread_proc")

    # -------------------------------------------------------------------------

    def udp_thread_proc(self):
        """Handle WAV chunks from UDP socket."""
        try:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.bind(("127.0.0.1", self.udp_audio_port))
            _LOGGER.debug("Listening for audio on UDP port %s", self.udp_audio_port)

            while True:
                wav_bytes, _ = udp_socket.recvfrom(
                    self.udp_chunk_size + WAV_HEADER_BYTES
                )
                self.wav_queue.put((wav_bytes, self.udp_siteId))
        except Exception:
            _LOGGER.exception("udp_thread_proc")

    # -------------------------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc):
        """Connected to MQTT broker."""
        try:
            # Start threads
            threading.Thread(target=self.detection_thread_proc, daemon=True).start()

            if self.udp_audio_port is not None:
                threading.Thread(target=self.udp_thread_proc, daemon=True).start()

            topics = [
                HotwordToggleOn.topic(),
                HotwordToggleOff.topic(),
                GetHotwords.topic(),
            ]

            if self.audioframe_topics:
                # Specific siteIds
                topics.extend(self.audioframe_topics)
            else:
                # All siteIds
                topics.append(AudioFrame.topic(siteId="+"))

            for topic in topics:
                self.client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)
        except Exception:
            _LOGGER.exception("on_connect")

    def on_message(self, client, userdata, msg):
        """Received message from MQTT broker."""
        try:
            if not msg.topic.endswith("/audioFrame"):
                _LOGGER.debug("Received %s byte(s) on %s", len(msg.payload), msg.topic)

            # Check enable/disable messages
            if msg.topic == HotwordToggleOn.topic():
                json_payload = json.loads(msg.payload or "{}")
                if self._check_siteId(json_payload):
                    self.enabled = True
                    self.first_audio = True
                    _LOGGER.debug("Enabled")
            elif msg.topic == HotwordToggleOff.topic():
                json_payload = json.loads(msg.payload or "{}")
                if self._check_siteId(json_payload):
                    self.enabled = False
                    _LOGGER.debug("Disabled")
            elif self.enabled and AudioFrame.is_topic(msg.topic):
                # Handle audio frames
                if (not self.audioframe_topics) or (
                    msg.topic in self.audioframe_topics
                ):
                    if self.first_audio:
                        _LOGGER.debug("Receiving audio")
                        self.first_audio = False

                    siteId = AudioFrame.get_siteId(msg.topic)
                    self.handle_audio_frame(msg.payload, siteId=siteId)
            elif msg.topic == GetHotwords.topic():
                json_payload = json.loads(msg.payload or "{}")
                if self._check_siteId(json_payload):
                    self.publish(
                        self.handle_get_hotwords(Hotwords.from_dict(json_payload))
                    )
        except Exception:
            _LOGGER.exception("on_message")
            _LOGGER.error("%s %s", msg.topic, msg.payload)

    def publish(self, message: Message, **topic_args):
        """Publish a Hermes message to MQTT."""
        try:
            _LOGGER.debug("-> %s", message)
            topic = message.topic(**topic_args)
            payload = json.dumps(attr.asdict(message))
            _LOGGER.debug("Publishing %s char(s) to %s", len(payload), topic)
            self.client.publish(topic, payload)
        except Exception:
            _LOGGER.exception("on_message")

    # -------------------------------------------------------------------------

    def _check_siteId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        if self.siteIds:
            return json_payload.get("siteId", "default") in self.siteIds

        # All sites
        return True

    # -------------------------------------------------------------------------

    def _convert_wav(self, wav_bytes: bytes) -> bytes:
        """Converts WAV data to required format with sox. Return raw audio."""
        return subprocess.run(
            [
                "sox",
                "-t",
                "wav",
                "-",
                "-r",
                str(self.sample_rate),
                "-e",
                "signed-integer",
                "-b",
                str(self.sample_width * 8),
                "-c",
                str(self.channels),
                "-t",
                "raw",
                "-",
            ],
            check=True,
            stdout=subprocess.PIPE,
            input=wav_bytes,
        ).stdout

    def maybe_convert_wav(self, wav_bytes: bytes) -> bytes:
        """Converts WAV data to required format if necessary. Returns raw audio."""
        with io.BytesIO(wav_bytes) as wav_io:
            with wave.open(wav_io, "rb") as wav_file:
                if (
                    (wav_file.getframerate() != self.sample_rate)
                    or (wav_file.getsampwidth() != self.sample_width)
                    or (wav_file.getnchannels() != self.channels)
                ):
                    # Return converted wav
                    return self._convert_wav(wav_bytes)

                # Return original audio
                return wav_file.readframes(wav_file.getnframes())
