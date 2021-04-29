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
        lang: typing.Optional[str] = None,
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

        self.chunk_sizes = dict()

        # Queue of WAV audio chunks to process (plus site_id)
        self.wav_queues = dict()

        self.first_audios = dict()
        self.audio_buffers = dict()

        self.engine_procs = dict()
        self.detectors = dict()
        self.detection_threads = dict()

        self.lang = lang

        self.last_audio_site_id: str = "default"
        self.model_id = self.model_path.name
        self.log_predictions = log_predictions

        for site_id in self.site_ids:
          self.audio_buffers[site_id] = bytes()
          self.first_audios[site_id] = True
          self.wav_queues[site_id] = queue.Queue()
          self.engine_procs[site_id] = None
          self.chunk_sizes[site_id] = chunk_size
          self.detectors[site_id] = None
          # Start threads
          self.detection_threads[site_id] = threading.Thread(target=self.detection_thread_proc, daemon=True, args=(site_id,)).start()

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

    # def engine_thread_proc(self):
    #     """Read predictions from precise-engine."""
    #     assert (
    #         self.engine_procs and self.engine_proc.stdout and self.detector
    #     ), "Precise engine is not started"

    #     for line in self.engine_proc.stdout:
    #         line = line.decode().strip()
    #         if line:
    #             if self.log_predictions:
    #                 _LOGGER.debug("Prediction: %s", line)

    #             try:
    #                 if self.detector.update(float(line)):
    #                     asyncio.run_coroutine_threadsafe(
    #                         self.publish_all(self.handle_detection()), self.loop
    #                     )
    #             except ValueError:
    #                 _LOGGER.exception("engine_proc")

    def load_engine(self, site_id, block=True):
        """Load Precise engine and model.

        if block is True, wait until an empty chunk is predicted before
        returning.
        """

        engine_cmd = [str(self.engine_path), str(self.model_path), str(self.chunk_sizes[site_id])]

        _LOGGER.debug(engine_cmd)
        self.engine_procs[site_id] = subprocess.Popen(
            engine_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

        self.detectors[site_id] = TriggerDetector(
            self.chunk_sizes[site_id],
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
            empty_chunk = b"\0" * self.chunk_sizes[site_id]
            self.engine_procs[site_id].stdin.write(empty_chunk)
            self.engine_procs[site_id].stdin.flush()
            self.engine_procs[site_id].stdout.readline()

    def stop_runner(self, site_id):
        """Stop Precise runner."""
        if self.engine_procs[site_id]:
            self.engine_procs[site_id].terminate()
            self.engine_procs[site_id].wait()
            self.engine_procs[site_id] = None

        if self.detection_threads[site_id]:
            self.wav_queues[site_id].put((None, None))
            self.detection_threads[site_id].join()
            self.detection_threads[site_id] = None

        self.detectors[site_id] = None

    # -------------------------------------------------------------------------

    async def handle_audio_frame(
        self, wav_bytes: bytes, site_id: str = "default"
    ) -> None:
        """Process a single audio frame"""
        self.wav_queues[site_id].put((wav_bytes, site_id))

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
                    lang=self.lang,
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

    def detection_thread_proc(self, site_id):
        """Handle WAV audio chunks."""
        try:
            while True:
                wav_bytes, site_id = self.wav_queues[site_id].get()
                if wav_bytes is None:
                    # Shutdown signal
                    break

                self.last_audio_site_id = site_id

                # Handle audio frames
                if self.first_audios[site_id]:
                    _LOGGER.debug("Receiving audio %s", site_id)
                    self.first_audio = False

                if not self.engine_procs[site_id]:
                    self.load_engine(site_id)

                assert (
                    self.engine_procs[site_id] and self.engine_procs[site_id].stdin
                ), "Precise engine not loaded"

                # Extract/convert audio data
                audio_data = self.maybe_convert_wav(wav_bytes)

                # Add to persistent buffer
                self.audio_buffers[site_id] += audio_data

                # Process in chunks.
                # Any remaining audio data will be kept in buffer.
                while len(self.audio_buffers[site_id]) >= self.chunk_sizes[site_id]:
                    chunk = self.audio_buffers[site_id][: self.chunk_sizes[site_id]]
                    self.audio_buffers[site_id] = self.audio_buffers[site_id][self.chunk_sizes[site_id] :]

                    if chunk:
                        # Send to precise-engine
                        # NOTE: The flush() is critical to this working.
                        self.engine_procs[site_id].stdin.write(chunk)
                        self.engine_procs[site_id].stdin.flush()

                        # Get prediction
                        line = self.engine_procs[site_id].stdout.readline()
                        line = line.decode().strip()

                        if line:
                            if self.log_predictions:
                                _LOGGER.debug("Prediction: %s", line)

                            try:
                                if self.detectors[site_id].update(float(line)):
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
                    self.wav_queues[site_id].put((wav_bytes, site_id))
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
                self.first_audios[site_id] = True
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
