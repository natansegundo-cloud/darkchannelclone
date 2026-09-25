"""Contrato Voice Pacing V2 preservado em um módulo compartilhado."""

from __future__ import annotations

import io
import math
import re
import sys
import unicodedata
import wave
from array import array
from typing import Any
from xml.sax.saxutils import escape

from . import ProviderError


BEATS_DATA: list[dict[str, Any]] = [
    {
        "beat_id": "B001",
        "title": "A notícia do aumento",
        "raw_text": "Você recebe a mensagem: seu salário aumentou.",
        "ssml_body": 'Você recebe a mensagem:<break time="160ms"/> seu salário aumentou.',
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B002",
        "title": "A folga dura pouco",
        "raw_text": "Por algumas semanas, finalmente sobra. Três meses depois, você está outra vez conferindo o saldo antes de gastar vinte reais.",
        "ssml_body": 'Por algumas semanas,<break time="120ms"/> finalmente sobra.<break time="340ms"/> Três meses depois,<break time="420ms"/> você está outra vez conferindo o saldo antes de gastar vinte reais.',
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B003",
        "title": "A contradição",
        "raw_text": "O aumento era real. Então por que a folga sumiu?",
        "ssml_body": 'O aumento era real.<break time="480ms"/> Então por que a folga sumiu?',
        "pause_after_ms": 450,
    },
    {
        "beat_id": "B004",
        "title": "Sem compra absurda",
        "raw_text": "Você não precisa ter feito nenhuma compra absurda.",
        "ssml_body": "Você não precisa ter feito nenhuma compra absurda.",
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B005",
        "title": "O mapa das três forças",
        "raw_text": "Mesmo assim, três coisas podem mudar junto com a renda: o que parece normal, com quem você se compara e quantas despesas passam a contar com aquele dinheiro. Quando as três se movem ao mesmo tempo, um aumento real pode ficar quase invisível.",
        "ssml_body": 'Mesmo assim,<break time="120ms"/> três coisas podem mudar junto com a renda:<break time="220ms"/> o que parece normal,<break time="140ms"/> com quem você se compara<break time="120ms"/> e quantas despesas passam a contar com aquele dinheiro.<break time="360ms"/> Quando as três se movem ao mesmo tempo,<break time="180ms"/> um aumento real pode ficar quase invisível.',
        "pause_after_ms": 450,
    },
    {
        "beat_id": "B006",
        "title": "A primeira peça",
        "raw_text": "A primeira peça é uma habilidade útil do cérebro que, neste caso, parece uma pequena traição: adaptação.",
        "ssml_body": 'A primeira peça é uma habilidade útil do cérebro que,<break time="120ms"/> neste caso,<break time="140ms"/> parece uma pequena traição:<break time="500ms"/> adaptação.',
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B007",
        "title": "O celular deixa de ser novo",
        "raw_text": "Pense no primeiro dia usando um celular novo. A tela parece absurda. A câmera impressiona. Até abrir um aplicativo dá uma satisfação ridícula. Um mês depois, ele não parece novo. Parece apenas seu celular.",
        "ssml_body": "Pense no primeiro dia usando um celular novo. A tela parece absurda. A câmera impressiona. Até abrir um aplicativo dá uma satisfação ridícula. Um mês depois, ele não parece novo. Parece apenas seu celular.",
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B008",
        "title": "O desgaste da mudança positiva",
        "raw_text": "Uma mudança positiva pode passar pelo mesmo processo. Em um estudo longitudinal sobre adaptação, pesquisadores acompanharam como o bem-estar ganho depois de mudanças positivas se desgastava. O estudo não media salários: acompanhava mudanças positivas em 481 estudantes. Duas rotas apareceram: a emoção nova perdia força e a aspiração subia.",
        "ssml_body": "Uma mudança positiva pode passar pelo mesmo processo. Em um estudo longitudinal sobre adaptação, pesquisadores acompanharam como o bem-estar ganho depois de mudanças positivas se desgastava. O estudo não media salários: acompanhava mudanças positivas em 481 estudantes. Duas rotas apareceram: a emoção nova perdia força e a aspiração subia.",
        "pause_after_ms": 450,
    },
    {
        "beat_id": "B009",
        "title": "O novo ponto de partida",
        "raw_text": "Isso significa que o aumento não precisa diminuir para parecer menor. Basta ele deixar de ser uma conquista e virar o novo ponto de partida.",
        "ssml_body": "Isso significa que o aumento não precisa diminuir para parecer menor. Basta ele deixar de ser uma conquista e virar o novo ponto de partida.",
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B010",
        "title": "O luxo vira normal",
        "raw_text": "Ontem, pedir comida era exceção. Hoje, é terça-feira. Ontem, o aplicativo de transporte era emergência. Hoje, chuva já parece motivo suficiente. O luxo não precisa continuar parecendo luxo. Quando entra na rotina, ganha outro nome: normal.",
        "ssml_body": "Ontem, pedir comida era exceção. Hoje, é terça-feira. Ontem, o aplicativo de transporte era emergência. Hoje, chuva já parece motivo suficiente. O luxo não precisa continuar parecendo luxo. Quando entra na rotina, ganha outro nome: normal.",
        "pause_after_ms": 400,
    },
    {
        "beat_id": "B011",
        "title": "A expectativa alcança a renda",
        "raw_text": "E aqui está a primeira recompensa dessa história: seu cérebro não pergunta apenas quanto eu tenho? Ele também pergunta quanto isso é diferente do que eu já esperava ter? Quando a expectativa alcança a renda, parte da sensação de avanço desaparece.",
        "ssml_body": "E aqui está a primeira recompensa dessa história: seu cérebro não pergunta apenas quanto eu tenho? Ele também pergunta quanto isso é diferente do que eu já esperava ter? Quando a expectativa alcança a renda, parte da sensação de avanço desaparece.",
        "pause_after_ms": 450,
    },
    {
        "beat_id": "B012",
        "title": "Segurança material é real",
        "raw_text": "Isso não quer dizer que uma renda maior não possa melhorar a vida. Ela pode ampliar segurança e escolhas, sobretudo quando reduz privações. Pagar moradia, comida, saúde e ter margem para imprevistos não é uma ilusão psicológica.",
        "ssml_body": "Isso não quer dizer que uma renda maior não possa melhorar a vida. Ela pode ampliar segurança e escolhas, sobretudo quando reduz privações. Pagar moradia, comida, saúde e ter margem para imprevistos não é uma ilusão psicológica.",
        "pause_after_ms": 450,
    },
    {
        "beat_id": "B013",
        "title": "A associação média",
        "raw_text": "Uma reanálise publicada em 2023 encontrou uma associação positiva entre renda e bem-estar emocional na média. Mas o efeito era pequeno, variava entre grupos e não confirmava aquela história popular de que existe um número mágico depois do qual dinheiro para de fazer diferença para todo mundo.",
        "ssml_body": "Uma reanálise publicada em 2023 encontrou uma associação positiva entre renda e bem-estar emocional na média. Mas o efeito era pequeno, variava entre grupos e não confirmava aquela história popular de que existe um número mágico depois do qual dinheiro para de fazer diferença para todo mundo.",
        "pause_after_ms": 400,
    },
]


def beats_for(selection: str | None = None) -> list[dict[str, Any]]:
    if not selection:
        return [dict(beat) for beat in BEATS_DATA]
    wanted = [item.strip().upper() for item in selection.split(",") if item.strip()]
    mapping = {beat["beat_id"]: beat for beat in BEATS_DATA}
    missing = [beat_id for beat_id in wanted if beat_id not in mapping]
    if missing:
        raise ProviderError(f"Beat(s) não encontrado(s): {', '.join(missing)}")
    return [dict(mapping[beat_id]) for beat_id in wanted]


def normalize_word(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char for char in plain if char.isalnum())


def read_pcm_wav(data: bytes) -> tuple[int, array]:
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            compression = handle.getcomptype()
            frames = handle.readframes(handle.getnframes())
    except wave.Error as exc:
        raise ProviderError(f"Azure não retornou WAV PCM válido: {exc}") from exc
    if channels != 1 or sample_width != 2 or compression != "NONE":
        raise ProviderError(
            "Formato Azure inesperado; esperado PCM mono de 16 bits. "
            f"Recebido: channels={channels}, width={sample_width}, compression={compression}."
        )
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    return sample_rate, samples


def write_pcm_wav(path: Any, sample_rate: int, samples: array) -> None:
    payload = array("h", samples)
    if sys.byteorder != "little":
        payload.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(payload.tobytes())


def build_ssml(beat: dict[str, Any], *, voice: str, language: str, rate: str) -> bytes:
    return (
        f'<speak version="1.0" xml:lang="{escape(language)}">'
        f'<voice name="{escape(voice)}">'
        f'<prosody rate="{escape(rate)}">{beat["ssml_body"]}</prosody>'
        "</voice></speak>"
    ).encode("utf-8")


def detect_speech_bounds(samples: array, sample_rate: int) -> tuple[int, int]:
    if not samples:
        raise ProviderError("Chunk Azure vazio.")
    peak = max(abs(sample) for sample in samples)
    if peak < 64:
        raise ProviderError("Chunk Azure sem sinal de voz detectável.")
    threshold = max(96, round(peak * 0.018))
    active = [index for index, sample in enumerate(samples) if abs(sample) >= threshold]
    if not active:
        raise ProviderError("Não foi possível detectar os limites de fala no WAV Azure.")
    padding = round(sample_rate * 0.025)
    return max(0, active[0] - padding), min(len(samples), active[-1] + padding + 1)


def token_weight(token: str) -> float:
    base = max(2, len(normalize_word(token)))
    if re.search(r"[.!?…][\"')\]]*$", token):
        return base + 3.0
    if re.search(r"[,;:][\"')\]]*$", token):
        return base + 1.4
    return float(base)


def align_words(
    text: str,
    samples: array,
    sample_rate: int,
    *,
    offset_seconds: float,
    global_index: int,
) -> tuple[list[dict[str, Any]], float, float]:
    words_text = re.findall(r"\S+", text)
    if not words_text:
        raise ProviderError("Beat sem palavras para alinhamento.")
    speech_first, speech_last = detect_speech_bounds(samples, sample_rate)
    speech_start = offset_seconds + speech_first / sample_rate
    speech_end = offset_seconds + speech_last / sample_rate
    available = speech_end - speech_start
    weights = [token_weight(token) for token in words_text]
    total_weight = sum(weights)
    cursor = speech_start
    words: list[dict[str, Any]] = []
    for beat_word_index, (token, weight) in enumerate(zip(words_text, weights), start=1):
        duration = available * weight / total_weight
        end = speech_end if beat_word_index == len(words_text) else cursor + duration
        words.append(
            {
                "index": global_index + beat_word_index - 1,
                "beat_word_index": beat_word_index,
                "text": token,
                "normalized": normalize_word(token),
                "start": round(cursor, 4),
                "end": round(end, 4),
            }
        )
        cursor = end
    return words, round(speech_start, 4), round(speech_end, 4)


def process_voice(samples: array, sample_rate: int) -> tuple[array, dict[str, Any]]:
    highpass_hz = 70.0
    compressor_threshold_dbfs = -18.0
    compressor_ratio = 2.5
    target_peak_dbfs = -1.0
    limiter_dbfs = -0.8
    rc = 1.0 / (2.0 * math.pi * highpass_hz)
    dt = 1.0 / sample_rate
    alpha = rc / (rc + dt)
    previous_input = 0.0
    previous_output = 0.0
    threshold = 32767.0 * (10.0 ** (compressor_threshold_dbfs / 20.0))
    filtered: list[float] = []
    for sample in samples:
        value = float(sample)
        highpassed = alpha * (previous_output + value - previous_input)
        previous_input = value
        previous_output = highpassed
        magnitude = abs(highpassed)
        if magnitude > threshold:
            magnitude = threshold + (magnitude - threshold) / compressor_ratio
            highpassed = math.copysign(magnitude, highpassed)
        filtered.append(highpassed)
    peak = max((abs(value) for value in filtered), default=1.0)
    target_peak = 32767.0 * (10.0 ** (target_peak_dbfs / 20.0))
    gain = min(4.0, target_peak / max(1.0, peak))
    limiter = 32767.0 * (10.0 ** (limiter_dbfs / 20.0))
    processed = array("h", (round(max(-limiter, min(limiter, value * gain))) for value in filtered))
    return processed, {
        "implementation": "python_stdlib_deterministic_pcm",
        "highpass_hz": highpass_hz,
        "compressor_threshold_dbfs": compressor_threshold_dbfs,
        "compressor_ratio": compressor_ratio,
        "peak_normalization_dbfs": target_peak_dbfs,
        "limiter_ceiling_dbfs": limiter_dbfs,
        "duration_preserved": True,
    }
