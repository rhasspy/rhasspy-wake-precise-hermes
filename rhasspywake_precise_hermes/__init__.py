"""Hermes MQTT server for Rhasspy wakeword with Mycroft Precise"""
import asyncio
import logging
import queue
import socket
import subprocess
import threading
import typing
from pathlib import Path

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

from .precise import TriggerDetector

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
        udp_audio: typing.Optional[typing.List[typing.Tuple[str, int, str]]] = None,
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

        self.first_audio: bool = True
        self.audio_buffer = bytes()

        self.engine_proc: typing.Optional[subprocess.Popen] = None
        self.detector: typing.Optional[TriggerDetector] = None

        self.last_audio_site_id: str = "default"
        self.model_id = self.model_path.name
        self.log_predictions = log_predictions

        # Start threads
        self.detection_thread = threading.Thread(
            target=self.detection_thread_proc, daemon=True
        )
        self.detection_thread.start()

        # Listen for raw audio on UDP too
        self.udp_chunk_size = udp_chunk_size

        if udp_audio:
            for udp_host, udp_port, udp_site_id in udp_audio:
                threading.Thread(
                    target=self.udp_thread_proc,
                    args=(udp_host, udp_port, udp_site_id),
                    daemon=True,
                ).start()

    # -------------------------------------------------------------------------

    def engine_thread_proc(self):
        """Read predictions from precise-engine."""
        assert (
            self.engine_proc and self.engine_proc.stdout and self.detector
        ), "Precise engine is not started"

        for line in self.engine_proc.stdout:
            line = line.decode().strip()
            if line:
                if self.log_predictions:
                    _LOGGER.debug("Prediction: %s", line)

                try:
                    if self.detector.update(float(line)):
                        asyncio.run_coroutine_threadsafe(
                            self.publish_all(self.handle_detection()), self.loop
                        )
                except ValueError:
                    _LOGGER.exception("engine_proc")

    def load_engine(self, block=True):
        """Load Precise engine and model.

        if block is True, wait until an empty chunk is predicted before
        returning.
        """

        engine_cmd = [str(self.engine_path), str(self.model_path), str(self.chunk_size)]

        _LOGGER.debug(engine_cmd)
        self.engine_proc = subprocess.Popen(
            engine_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

        self.detector = TriggerDetector(
            self.chunk_size,
            sensitivity=self.sensitivity,
            trigger_level=self.trigger_level,
        )

        _LOGGER.debug(
            "Loaded Mycroft Precise (model=%s, sensitivity=%s, trigger_level=%s)",
            self.model_path,
            self.sensitivity,
            self.trigger_level,
        )

        if block:
            # Send empty chunk and wait for a prediction
            _LOGGER.debug("Waiting for Precise to start...")
            empty_chunk = b"\0" * self.chunk_size
            self.engine_proc.stdin.write(empty_chunk)
            self.engine_proc.stdin.flush()
            self.engine_proc.stdout.readline()

    def stop_runner(self):
        """Stop Precise runner."""
        if self.engine_proc:
            self.engine_proc.terminate()
            self.engine_proc.wait()
            self.engine_proc = None

        if self.detection_thread:
            self.wav_queue.put((None, None))
            self.detection_thread.join()
            self.detection_thread = None

        self.detector = None

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
                if wav_bytes is None:
                    # Shutdown signal
                    break

                self.last_audio_site_id = site_id

                # Handle audio frames
                if self.first_audio:
                    _LOGGER.debug("Receiving audio")
                    self.first_audio = False

                if not self.engine_proc:
                    self.load_engine()

                assert (
                    self.engine_proc and self.engine_proc.stdin
                ), "Precise engine not loaded"

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
                        # Send to precise-engine
                        # NOTE: The flush() is critical to this working.
                        self.engine_proc.stdin.write(chunk)
                        self.engine_proc.stdin.flush()

                        # Get prediction
                        line = self.engine_proc.stdout.readline()
                        line = line.decode().strip()

                        if line:
                            if self.log_predictions:
                                _LOGGER.debug("Prediction: %s", line)

                            try:
                                if self.detector.update(float(line)):
                                    asyncio.run_coroutine_threadsafe(
                                        self.publish_all(self.handle_detection()),
                                        self.loop,
                                    )
                            except ValueError:
                                _LOGGER.exception("prediction")
        except Exception:
            _LOGGER.exception("detection_thread_proc")

    # -------------------------------------------------------------------------

    def udp_thread_proc(self, host: str, port: int, site_id: str):
        """Handle WAV chunks from UDP socket."""
        try:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.bind((host, port))
            _LOGGER.debug("Listening for audio on UDP %s:%s", host, port)

            while True:
                wav_bytes, _ = udp_socket.recvfrom(
                    self.udp_chunk_size + WAV_HEADER_BYTES
                )

                if self.enabled:
                    self.wav_queue.put((wav_bytes, site_id))
        except Exception:
            _LOGGER.exception("udp_thread_proc")

    # -------------------------------------------------------------------------

    async def on_message_blocking(
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
