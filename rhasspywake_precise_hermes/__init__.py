"""Hermes MQTT server for Rhasspy wakeword with Mycroft Precise"""
import asyncio
import io
import logging
import queue
import socket
import subprocess
import threading
import typing
import wave
from dataclasses import dataclass, field
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


@dataclass
class SiteInfo:
    """Self-contained information for a single site"""

    site_id: str
    enabled: bool = True
    disabled_reasons: typing.Set[str] = field(default_factory=set)
    detection_thread: typing.Optional[threading.Thread] = None
    audio_buffer: bytes = bytes()
    first_audio: bool = True
    engine_proc: typing.Optional[subprocess.Popen] = None
    detector: typing.Optional[TriggerDetector] = None

    # Queue of (bytes, is_raw)
    wav_queue: "queue.Queue[typing.Tuple[bytes, bool]]" = field(
        default_factory=queue.Queue
    )


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
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        chunk_size: int = 2048,
        udp_audio: typing.Optional[typing.List[typing.Tuple[str, int, str]]] = None,
        udp_chunk_size: int = 2048,
        udp_raw_audio: typing.Optional[typing.Iterable[str]] = None,
        udp_forward_mqtt: typing.Optional[typing.Iterable[str]] = None,
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

        # Required audio format
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels

        self.chunk_size = chunk_size
        self.site_info: typing.Dict[str, SiteInfo] = {}

        self.lang = lang

        self.last_audio_site_id: str = "default"
        self.model_id = self.model_path.name
        self.log_predictions = log_predictions

        # Create site information for known sites
        for site_id in self.site_ids:
            site_info = SiteInfo(site_id=site_id)

            # Create and start detection thread
            site_info.detection_thread = threading.Thread(
                target=self.detection_thread_proc, daemon=True, args=(site_info,)
            )
            site_info.detection_thread.start()

            self.site_info[site_id] = site_info

        # Listen for raw audio on UDP too
        self.udp_chunk_size = udp_chunk_size

        # Site ids where UDP audio is raw 16Khz, 16-bit mono PCM chunks instead
        # of WAV chunks.
        self.udp_raw_audio = set(udp_raw_audio or [])

        # Site ids where UDP audio should be forward to MQTT after detection.
        self.udp_forward_mqtt = set(udp_forward_mqtt or [])

        if udp_audio:
            for udp_host, udp_port, udp_site_id in udp_audio:
                threading.Thread(
                    target=self.udp_thread_proc,
                    args=(udp_host, udp_port, udp_site_id),
                    daemon=True,
                ).start()

    # -------------------------------------------------------------------------

    def load_engine(self, site_info: SiteInfo, block: bool = True):
        """Load Precise engine and model.

        if block is True, wait until an empty chunk is predicted before
        returning.
        """

        engine_cmd = [str(self.engine_path), str(self.model_path), str(self.chunk_size)]

        _LOGGER.debug(engine_cmd)
        site_info.engine_proc = subprocess.Popen(
            engine_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

        assert (
            (site_info.engine_proc is not None)
            and (site_info.engine_proc.stdin is not None)
            and (site_info.engine_proc.stdout is not None)
        ), "Precise engine not loaded"

        site_info.detector = TriggerDetector(
            self.chunk_size,
            sensitivity=self.sensitivity,
            trigger_level=self.trigger_level,
        )

        _LOGGER.debug(
            "%s: loaded Mycroft Precise (model=%s, sensitivity=%s, trigger_level=%s)",
            site_info.site_id,
            self.model_path,
            self.sensitivity,
            self.trigger_level,
        )

        if block:
            # Send empty chunk and wait for a prediction
            _LOGGER.debug("%s: waiting for Precise to start...", site_info.site_id)
            empty_chunk = b"\0" * self.chunk_size
            site_info.engine_proc.stdin.write(empty_chunk)
            site_info.engine_proc.stdin.flush()
            site_info.engine_proc.stdout.readline()
            _LOGGER.debug("%s: started", site_info.site_id)

    def stop_runners(self):
        """Stop Precise runners."""
        _LOGGER.debug("Stopping precise runners...")

        for site_info in self.site_info.values():
            if site_info.engine_proc is not None:
                site_info.engine_proc.terminate()
                site_info.engine_proc.wait()
                site_info.engine_proc = None

            if site_info.detection_thread is not None:
                site_info.wav_queue.put((None, None))
                site_info.detection_thread.join()
                site_info.detection_thread = None

            site_info.detector = None

        _LOGGER.debug("Stopped")

    # -------------------------------------------------------------------------

    async def handle_audio_frame(
        self, wav_bytes: bytes, site_id: str = "default"
    ) -> None:
        """Process a single audio frame"""
        site_info = self.site_info.get(site_id)
        if site_info is None:
            # Create information for new site
            site_info = SiteInfo(site_id=site_id)
            site_info.detection_thread = threading.Thread(
                target=self.detection_thread_proc, daemon=True, args=(site_info,)
            )

            site_info.detection_thread.start()
            self.site_info[site_id] = site_info

        site_info.wav_queue.put((wav_bytes, False))

    async def handle_detection(
        self, site_info: SiteInfo
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
                    site_id=site_info.site_id,
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
                error=str(e), context=str(self.model_path), site_id=site_info.site_id
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

    def detection_thread_proc(self, site_info: SiteInfo):
        """Handle WAV audio chunks for one site id."""
        try:
            while True:
                wav_bytes, is_raw = site_info.wav_queue.get()
                if wav_bytes is None:
                    # Shutdown signal
                    break

                # Handle audio frames
                if site_info.first_audio:
                    _LOGGER.debug("%s: receiving audio", site_info.site_id)
                    site_info.first_audio = False

                if site_info.engine_proc is None:
                    self.load_engine(site_info)

                assert (
                    (site_info.engine_proc is not None)
                    and (site_info.engine_proc.stdin is not None)
                    and (site_info.engine_proc.stdout is not None)
                    and (site_info.detector is not None)
                ), "Precise engine/detector not loaded"

                # Extract/convert audio data

                if is_raw:
                    # Raw audio chunks
                    audio_data = wav_bytes
                else:
                    # WAV chunks
                    audio_data = self.maybe_convert_wav(wav_bytes)

                # Add to persistent buffer
                site_info.audio_buffer += audio_data

                # Process in chunks.
                # Any remaining audio data will be kept in buffer.
                while len(site_info.audio_buffer) >= self.chunk_size:
                    chunk = site_info.audio_buffer[: self.chunk_size]
                    site_info.audio_buffer = site_info.audio_buffer[self.chunk_size :]

                    if chunk:
                        # Send to precise-engine
                        # NOTE: The flush() is critical to this working.
                        site_info.engine_proc.stdin.write(chunk)
                        site_info.engine_proc.stdin.flush()

                        # Get prediction
                        line = site_info.engine_proc.stdout.readline()
                        line = line.decode().strip()

                        if line:
                            if self.log_predictions:
                                _LOGGER.debug("Prediction: %s", line)

                            try:
                                assert self.loop is not None

                                if site_info.detector.update(float(line)):
                                    asyncio.run_coroutine_threadsafe(
                                        self.publish_all(
                                            self.handle_detection(site_info)
                                        ),
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
            site_info = self.site_info[site_id]
            is_raw_audio = site_id in self.udp_raw_audio
            forward_to_mqtt = site_id in self.udp_forward_mqtt

            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.bind((host, port))
            _LOGGER.debug(
                "Listening for audio on UDP %s:%s (siteId=%s, raw=%s)",
                host,
                port,
                site_id,
                is_raw_audio,
            )

            chunk_size = self.udp_chunk_size
            if is_raw_audio:
                chunk_size += WAV_HEADER_BYTES

            while True:
                wav_bytes, _ = udp_socket.recvfrom(chunk_size)

                if site_info.enabled:
                    site_info.wav_queue.put((wav_bytes, is_raw_audio))
                elif forward_to_mqtt:
                    # When the wake word service is disabled, ASR should be active
                    if is_raw_audio:
                        # Re-package as WAV chunk and publish to MQTT
                        with io.BytesIO() as wav_buffer:
                            wav_file: wave.Wave_write = wave.open(wav_buffer, "wb")
                            with wav_file:
                                wav_file.setframerate(self.sample_rate)
                                wav_file.setsampwidth(self.sample_width)
                                wav_file.setnchannels(self.channels)
                                wav_file.writeframes(wav_bytes)

                            publish_wav_bytes = wav_buffer.getvalue()
                    else:
                        # Use WAV chunk as-is
                        publish_wav_bytes = wav_bytes

                    self.publish(
                        AudioFrame(wav_bytes=publish_wav_bytes),
                        site_id=site_info.site_id,
                    )
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
        site_info = self.site_info.get(site_id) if site_id else None

        if isinstance(message, HotwordToggleOn):
            if site_info:
                if message.reason == HotwordToggleReason.UNKNOWN:
                    # Always enable on unknown
                    site_info.disabled_reasons.clear()
                else:
                    site_info.disabled_reasons.discard(message.reason)

                if site_info.disabled_reasons:
                    _LOGGER.debug("Still disabled: %s", site_info.disabled_reasons)
                else:
                    site_info.enabled = True
                    site_info.first_audio = True

                    _LOGGER.debug("Enabled")
        elif isinstance(message, HotwordToggleOff):
            if site_info:
                site_info.enabled = False
                site_info.disabled_reasons.add(message.reason)
                _LOGGER.debug("Disabled")
        elif isinstance(message, AudioFrame):
            if site_info and site_info.enabled:
                await self.handle_audio_frame(
                    message.wav_bytes, site_id=site_info.site_id
                )
        elif isinstance(message, GetHotwords):
            async for hotword_result in self.handle_get_hotwords(message):
                yield hotword_result
        else:
            _LOGGER.warning("Unexpected message: %s", message)
