"""Hermes MQTT server for Rhasspy wakeword with Mycroft Precise"""
import asyncio
import logging
import queue
import socket
import threading
import typing
from pathlib import Path

from precise_runner import PreciseEngine, PreciseRunner, ReadWriteStream
from rhasspyhermes.audioserver import AudioFrame
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient, TopicArgs
from rhasspyhermes.wake import (
    GetHotwords,
    Hotword,
    HotwordDetected,
    HotwordError,
    Hotwords,
    HotwordToggleOff,
    HotwordToggleOn,
    HotwordToggleReason,
)

WAV_HEADER_BYTES = 44
_LOGGER = logging.getLogger("rhasspywake_precise_hermes")

# -----------------------------------------------------------------------------


class WakeHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy wakeword with Mycroft Precise."""

    def __init__(
        self,
        client,
        model_path: Path,
        engine_path: Path,
        sensitivity: float = 0.5,
        trigger_level: int = 3,
        wakeword_id: str = "",
        model_dirs: typing.Optional[typing.List[Path]] = None,
        site_ids: typing.Optional[typing.List[str]] = None,
        enabled: bool = True,
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        chunk_size: int = 2048,
        udp_audio_port: typing.Optional[int] = None,
        udp_chunk_size: int = 2048,
        log_predictions: bool = False,
    ):
        super().__init__(
            "rhasspywake_precise_hermes",
            client,
            sample_rate=sample_rate,
            sample_width=sample_width,
            channels=channels,
            site_ids=site_ids,
        )

        self.subscribe(AudioFrame, HotwordToggleOn, HotwordToggleOff, GetHotwords)

        self.model_path = model_path
        self.engine_path = engine_path
        self.sensitivity = sensitivity
        self.trigger_level = trigger_level

        self.wakeword_id = wakeword_id
        self.model_dirs = model_dirs or []

        self.enabled = enabled
        self.disabled_reasons: typing.Set[str] = set()

        # Required audio format
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels

        self.chunk_size = chunk_size

        # Queue of WAV audio chunks to process (plus site_id)
        self.wav_queue: queue.Queue = queue.Queue()

        # Listen for raw audio on UDP too
        self.udp_audio_port = udp_audio_port
        self.udp_chunk_size = udp_chunk_size

        # site_id used for detections from UDP
        self.udp_site_id = self.site_id

        self.first_audio: bool = True
        self.audio_buffer = bytes()

        self.engine: typing.Optional[PreciseEngine] = None
        self.engine_stream: typing.Optional[ReadWriteStream] = None
        self.runner: typing.Optional[PreciseRunner] = None
        self.last_audio_site_id: str = "default"
        self.model_id = self.model_path.name
        self.log_predictions = log_predictions

        # Start threads
        threading.Thread(target=self.detection_thread_proc, daemon=True).start()

        if self.udp_audio_port is not None:
            threading.Thread(target=self.udp_thread_proc, daemon=True).start()

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

        def on_activation():
            asyncio.run_coroutine_threadsafe(
                self.publish_all(self.handle_detection()), self.loop
            )

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
                on_activation=on_activation,
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

    async def handle_audio_frame(
        self, wav_bytes: bytes, site_id: str = "default"
    ) -> None:
        """Process a single audio frame"""
        self.wav_queue.put((wav_bytes, site_id))

    async def handle_detection(
        self,
    ) -> typing.AsyncIterable[
        typing.Union[typing.Tuple[HotwordDetected, TopicArgs], HotwordError]
    ]:
        """Handle a successful hotword detection"""
        try:
            wakeword_id = self.wakeword_id
            if not wakeword_id:
                # Use file name
                wakeword_id = self.model_path.stem

            yield (
                HotwordDetected(
                    site_id=self.last_audio_site_id,
                    model_id=self.model_id,
                    current_sensitivity=self.sensitivity,
                    model_version="",
                    model_type="personal",
                ),
                {"wakeword_id": wakeword_id},
            )
        except Exception as e:
            _LOGGER.exception("handle_detection")
            yield HotwordError(
                error=str(e),
                context=str(self.model_path),
                site_id=self.last_audio_site_id,
            )

    async def handle_get_hotwords(
        self, get_hotwords: GetHotwords
    ) -> typing.AsyncIterable[typing.Union[Hotwords, HotwordError]]:
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
                        model_id=model_path.name,
                        model_words=model_words,
                        model_type="personal",
                    )
                )

            yield Hotwords(
                models=hotword_models, id=get_hotwords.id, site_id=get_hotwords.site_id
            )

        except Exception as e:
            _LOGGER.exception("handle_get_hotwords")
            yield HotwordError(
                error=str(e), context=str(get_hotwords), site_id=get_hotwords.site_id
            )

    def detection_thread_proc(self):
        """Handle WAV audio chunks."""
        try:
            while True:
                wav_bytes, site_id = self.wav_queue.get()
                self.last_audio_site_id = site_id

                # Handle audio frames
                if self.first_audio:
                    _LOGGER.debug("Receiving audio")
                    self.first_audio = False

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

                if self.enabled:
                    self.wav_queue.put((wav_bytes, self.udp_site_id))
        except Exception:
            _LOGGER.exception("udp_thread_proc")

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        site_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        """Received message from MQTT broker."""
        # Check enable/disable messages
        if isinstance(message, HotwordToggleOn):
            if message.reason == HotwordToggleReason.UNKNOWN:
                # Always enable on unknown
                self.disabled_reasons.clear()
            else:
                self.disabled_reasons.discard(message.reason)

            if self.disabled_reasons:
                _LOGGER.debug("Still disabled: %s", self.disabled_reasons)
            else:
                self.enabled = True
                self.first_audio = True
                _LOGGER.debug("Enabled")
        elif isinstance(message, HotwordToggleOff):
            self.enabled = False
            self.disabled_reasons.add(message.reason)
            _LOGGER.debug("Disabled")
        elif isinstance(message, AudioFrame):
            if self.enabled:
                assert site_id, "Missing site_id"
                await self.handle_audio_frame(message.wav_bytes, site_id=site_id)
        elif isinstance(message, GetHotwords):
            async for hotword_result in self.handle_get_hotwords(message):
                yield hotword_result
        else:
            _LOGGER.warning("Unexpected message: %s", message)
