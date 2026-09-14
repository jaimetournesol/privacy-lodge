"""Voice helpers: local wake-word screening (Whisper tiny), ElevenLabs TTS, OpenAI STT."""
import io
import os
import re
import threading
import time

from .util import log

_wake_model = None
_wake_lock = threading.Lock()
WAKE_MODEL = os.environ.get("AGENTNODE_WAKE_MODEL", "base")   # tiny misses onsets; base is ~0.4 s per 2 s window


def wake_patterns(word: str) -> list[str]:
    """Normalized (letters only) fragments Whisper tiny tends to produce for the wake word."""
    w = re.sub(r"[^a-z]", "", word.lower())
    pats = {w}
    if w == "tvpc":
        pats |= {"vpc", "vpisi", "vpici", "vpiece", "vpeace", "vbc", "tbpc", "dvpc", "eepeecee", "veepc", "tvpz"}
    if w == "conductor":
        pats |= {"conducto", "conducter", "kondukt", "conduc", "condoctor", "conductr"}
    return sorted(pats, key=len, reverse=True)


def wake_patterns_fuzzy(word: str) -> list[str]:
    """Near misses the screener produces when the wake word is masked (TV, distance). A fuzzy hit only opens the mic
    tentatively: the accurate transcription of the whole sentence has to contain the wake word, or it is dropped."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if w == "conductor":
        return [r"\b(?:doctor|dr\.?|\w*ductor\w*|conduct\w*|connector|conducta\w*|candukt\w*|\w*dukt\w*)\b"]
    return []


def load_wake_model():
    global _wake_model
    from . import speech_client
    if speech_client.configured():
        try:
            _wake_model = bool(speech_client.request('/healthz').get('ok'))
        except Exception as e:
            log('speech worker unavailable:', e)
            _wake_model = False
        return _wake_model
    with _wake_lock:
        if _wake_model is None:
            try:
                from faster_whisper import WhisperModel
                t0 = time.time()
                _wake_model = WhisperModel(WAKE_MODEL, device="cpu", compute_type="int8", cpu_threads=2)
                log(f"wake model {WAKE_MODEL} loaded in {time.time() - t0:.1f}s")
            except Exception as e:
                log("wake model unavailable:", e)
                _wake_model = False
    return _wake_model


def wake_available() -> bool:
    return bool(_wake_model)


_confirm_model = None


def confirm_text(wav_bytes: bytes, lang: str | None) -> str:
    """Second opinion for tentative wakes: the local 'small' model on the captured segment (loaded on first use)."""
    global _confirm_model
    from . import speech_client
    if speech_client.configured():
        return speech_client.request('/transcribe/confirm',wav_bytes,lang).get('text','')
    with _wake_lock:
        if _confirm_model is None:
            try:
                from faster_whisper import WhisperModel
                _confirm_model = WhisperModel(os.environ.get("AGENTNODE_CONFIRM_MODEL", "small"), device="cpu", compute_type="int8", cpu_threads=2)
            except Exception as e:
                log("confirm model unavailable:", e)
                _confirm_model = False
    if not _confirm_model:
        return ""
    segs, _ = _confirm_model.transcribe(io.BytesIO(wav_bytes), language=(lang.split("-")[0] if lang else None), beam_size=1,
                                        vad_filter=False, condition_on_previous_text=False)
    return " ".join(sg.text for sg in segs).strip()


def wake_screen(wav_bytes: bytes, lang: str | None, patterns: list[str], prompt: str | None = None) -> dict:
    from . import speech_client
    if speech_client.configured():
        global _wake_model
        text=speech_client.request('/transcribe/wake',wav_bytes,lang,prompt).get('text','')
        _wake_model=True
        norm=re.sub(r'[^a-z0-9]','',text.lower())
        return {'available':True,'text':text,'wake':any(p in norm for p in patterns),'norm':norm}
    m = load_wake_model()
    if not m:
        return {"available": False, "text": "", "wake": False}
    segs, _ = m.transcribe(io.BytesIO(wav_bytes), language=(lang.split("-")[0] if lang else None), beam_size=1,
                           vad_filter=False, condition_on_previous_text=False, initial_prompt=prompt)
    text = " ".join(sg.text for sg in segs).strip()
    norm = re.sub(r"[^a-z0-9]", "", text.lower())
    return {"available": True, "text": text, "wake": any(p in norm for p in patterns), "norm": norm}


def tts_elevenlabs(key: str, voice: str, text: str) -> bytes:
    import requests
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                      headers={"xi-api-key": key, "Content-Type": "application/json"},
                      json={"text": text, "model_id": os.environ.get("AGENTNODE_TTS_MODEL", "eleven_turbo_v2_5"),
                            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.2, "use_speaker_boost": True}}, timeout=30)
    r.raise_for_status()
    return r.content


def tts_voices(key: str) -> list[dict]:
    import requests
    r = requests.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}, timeout=15)
    r.raise_for_status()
    out = []
    for v in r.json().get("voices", []):
        l = v.get("labels") or {}
        out.append({"id": v["voice_id"], "name": v["name"].split(" - ")[0], "desc": " · ".join(x for x in (l.get("accent"), l.get("gender"), l.get("description") or l.get("descriptive")) if x)})
    return out


def stt_openai(key: str, model: str, audio: bytes, ctype: str, lang: str | None, prompt: str) -> str:
    import requests
    ext = "webm" if "webm" in ctype else "ogg" if "ogg" in ctype else "mp4" if "mp4" in ctype else "wav"
    data = {"model": model}
    if prompt:
        data["prompt"] = prompt
    if lang:
        data["language"] = lang.split("-")[0]
    r = requests.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                      files={"file": (f"speech.{ext}", audio, ctype)}, data=data, timeout=60)
    r.raise_for_status()
    return r.json().get("text", "")
