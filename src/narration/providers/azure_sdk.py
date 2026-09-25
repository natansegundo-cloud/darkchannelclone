"""Provider Azure Speech SDK com eventos WordBoundary reais."""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

from . import ProviderError, ProviderResult
from .pacing import read_pcm_wav


SDK_PACKAGE = "azure-cognitiveservices-speech==1.51.2"


def _sdk() -> Any:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:
        raise ProviderError(
            "SDK Azure ausente. Instale somente azure-cognitiveservices-speech==1.51.2."
        ) from exc
    return speechsdk


def _duration_100ns(value: Any) -> int | None:
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return round(value.total_seconds() * 10_000_000)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _ssml(beat: dict[str, Any], narrator: dict[str, Any]) -> str:
    delivery = narrator.get("delivery", {})
    language = narrator.get("language", "pt-BR")
    voice = narrator.get("voice", "pt-BR-AntonioNeural")
    rate = delivery.get("rate", "0%")
    pitch = delivery.get("pitch", "0%")
    volume = delivery.get("volume", "default")
    return (
        f'<speak version="1.0" xml:lang="{escape(language)}">'
        f'<voice name="{escape(voice)}">'
        f'<prosody rate="{escape(rate)}" pitch="{escape(pitch)}" volume="{escape(volume)}">'
        f'{beat["ssml_body"]}</prosody></voice></speak>'
    )


def synthesize(
    beat: dict[str, Any],
    *,
    narrator: dict[str, Any],
    key: str,
    region: str,
) -> ProviderResult:
    speechsdk = _sdk()
    speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
    speech_config.speech_synthesis_voice_name = narrator["voice"]
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm
    )
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)
    boundaries: list[dict[str, Any]] = []

    def on_word_boundary(event: Any) -> None:
        text = str(getattr(event, "text", "") or "")
        normalized = _normalize(text)
        if not normalized:
            return
        offset = _duration_100ns(getattr(event, "audio_offset", None))
        duration = _duration_100ns(getattr(event, "duration", None))
        boundaries.append(
            {
                "text": text,
                "normalized": normalized,
                "text_offset": _optional_int(getattr(event, "text_offset", None)),
                "word_length": _optional_int(getattr(event, "word_length", None)),
                "audio_offset_100ns": offset,
                "audio_offset_ms": None if offset is None else round(offset / 10_000, 3),
                "duration_100ns": duration,
                "duration_ms": None if duration is None else round(duration / 10_000, 3),
                "boundary_type": None if getattr(event, "boundary_type", None) is None else str(event.boundary_type),
            }
        )

    signal = synthesizer.synthesis_word_boundary
    signal.connect(on_word_boundary)
    try:
        result = synthesizer.speak_ssml_async(_ssml(beat, narrator)).get()
    finally:
        signal.disconnect_all()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        details = getattr(result, "cancellation_details", None)
        reason = getattr(details, "reason", "unknown")
        error = re.sub(r"[A-Za-z0-9+/=_-]{24,}", "[redacted]", str(getattr(details, "error_details", "")))
        raise ProviderError(f"Falha na síntese Azure SDK: reason={reason}; details={error}")
    if not boundaries or any(item["audio_offset_ms"] is None for item in boundaries):
        raise ProviderError("Azure SDK não retornou WordBoundary completo.")
    synthesis_id = str(getattr(result, "result_id", "") or "").strip()
    if not synthesis_id:
        raise ProviderError("Azure SDK não retornou synthesis_id para a síntese.")
    audio_data = bytes(result.audio_data)
    sample_rate, _samples = read_pcm_wav(audio_data)
    boundaries.sort(key=lambda item: (item["audio_offset_100ns"] is None, item["audio_offset_100ns"] or 0))
    return ProviderResult(
        audio_data=audio_data,
        sample_rate=sample_rate,
        boundaries=boundaries,
        metadata={
            "timing_quality": "WORD_BOUNDARY_REAL",
            "sdk_package": SDK_PACKAGE,
            "synthesis_id": synthesis_id,
        },
    )


def _normalize(value: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char for char in plain if char.isalnum())
