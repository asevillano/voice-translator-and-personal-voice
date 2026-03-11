# ────────────────────────────────────────────────────────────────
# simultaneous_translator_ws_app.py
# Real-time Speech-to-Speech Translation via WebSocket + REST
# *** No Azure Speech SDK required ***
#
# Architecture:
#   • Microphone capture:    sounddevice (PortAudio)
#   • Speech Translation:    WebSocket to Azure Speech Translation API
#   • Text-to-Speech:        REST API with streaming playback
#   • UI:                    Streamlit
#
# Supports dual auth: API key  or  Entra ID (custom subdomain)
# ────────────────────────────────────────────────────────────────
import os
import sys
import time
import json
import uuid
import struct
import threading
import io
import wave
from datetime import datetime, timezone

from dotenv import load_dotenv
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import websocket                       # pip install websocket-client
import requests
import sounddevice as sd
import numpy as np

from utils import LANGUAGES, get_language_name

# ---- pycaw for microphone mute/unmute (Windows) ----
try:
    from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
    from pycaw.pycaw import AudioUtilities
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False

# ---- Streamlit version–independent rerun ----
def do_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()

# ═══════════════════════════════════════════════════════════════
#  CREDENTIALS / ENDPOINTS
# ═══════════════════════════════════════════════════════════════
load_dotenv(override=True)
speech_key    = os.getenv("SPEECH_KEY")
speech_region = os.getenv("SPEECH_REGION")
if not speech_region:
    st.error("Missing SPEECH_REGION environment variable.")
    st.stop()
speech_endpoint = os.getenv("SPEECH_ENDPOINT")  # custom subdomain for Entra ID

# Azure AD token-based auth (used when SPEECH_KEY is not set)
_credential = None
def get_speech_token() -> str:
    """Get an Azure AD token (cached/refreshed by azure-identity)."""
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential()
    return _credential.get_token("https://cognitiveservices.azure.com/.default").token

use_token_auth = not speech_key
if use_token_auth and not speech_endpoint:
    st.error(
        "Entra ID auth requires SPEECH_ENDPOINT with custom subdomain "
        "(e.g. https://myresource.cognitiveservices.azure.com). Set it in .env"
    )
    st.stop()

if "auth_logged" not in st.session_state:
    st.session_state.auth_logged = True
    mode = "Entra ID (token)" if use_token_auth else "API key"
    print(f"[AUTH] Mode: {mode}  •  WebSocket + REST (no SDK)")

# Endpoint resolution
# WebSocket always uses the REGIONAL host (custom subdomain doesn't expose WS paths).
# For Entra ID auth on regional endpoints, the documented approach is the compound token:
#   Authorization: Bearer aad#RESOURCE_ID#ENTRA_TOKEN
# TTS REST uses the REGIONAL endpoint; for Entra ID we exchange the Entra
# token for a short-lived speech auth token via the issueToken endpoint.
speech_resource_id = os.getenv("SPEECH_RESOURCE_ID")

WS_HOST  = f"{speech_region}.stt.speech.microsoft.com"
TTS_BASE = f"https://{speech_region}.tts.speech.microsoft.com"   # always regional

if use_token_auth:
    if not speech_resource_id:
        st.error(
            "Entra ID auth (no SDK) requires SPEECH_RESOURCE_ID in .env.\n"
            "Get it with: az cognitiveservices account show --name <name> "
            "--resource-group <rg> --query id -o tsv"
        )
        st.stop()

# ── Cached speech auth token (for TTS with Entra ID) ──
_speech_token_cache = {"token": None, "expires": 0.0}

def _get_speech_auth_token() -> str:
    """Exchange the Entra ID token for a short-lived speech auth token.

    The issueToken endpoint on the custom domain accepts an Entra ID
    Bearer token and returns a 10-minute speech auth token that works
    with the *regional* TTS endpoint.
    """
    now = time.time()
    if _speech_token_cache["token"] and now < _speech_token_cache["expires"]:
        return _speech_token_cache["token"]

    token_url = f"{speech_endpoint.rstrip('/')}/sts/v1.0/issueToken"
    entra_token = get_speech_token()
    resp = requests.post(
        token_url,
        headers={"Authorization": f"Bearer {entra_token}",
                 "Content-Length": "0"},
        timeout=10,
    )
    if resp.status_code == 200:
        _speech_token_cache["token"]   = resp.text
        _speech_token_cache["expires"] = now + 540   # refresh at 9 min (valid 10)
        print(f"[AUTH] ✅ issueToken OK (len={len(resp.text)})")
        return resp.text

    print(f"[AUTH] ⚠️ issueToken HTTP {resp.status_code}: {resp.text[:200]}")
    # Fallback: try the Entra token directly (works for Speech-only resources)
    return entra_token

def _auth_headers() -> dict:
    """Return auth header dict for TTS REST calls."""
    if use_token_auth:
        return {"Authorization": f"Bearer {_get_speech_auth_token()}"}
    return {"Ocp-Apim-Subscription-Key": speech_key}

def _ws_auth_headers() -> dict:
    """Return auth headers for the regional WebSocket endpoint."""
    if use_token_auth:
        # Compound token: aad#<resource_id>#<entra_token>
        compound = f"aad#{speech_resource_id}#{get_speech_token()}"
        return {"Authorization": f"Bearer {compound}"}
    return {"Ocp-Apim-Subscription-Key": speech_key}

# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════
PRIMARY_LANGUAGE = "es-ES"

VOICE_OPTIONS = {
    "Ximena (Spanish Female)": "es-ES-Ximena:DragonHDLatestNeural",
    "Tristan (Spanish Male)":  "es-ES-Tristan:DragonHDLatestNeural",
}

# Map language codes → high-quality Neural voices (used for non-Spanish TTS)
VOICE_MAP = {
    "es": "es-ES-ElviraNeural",    "en": "en-US-AvaNeural",
    "fr": "fr-FR-DeniseNeural",    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",      "pt": "pt-PT-RaquelNeural",
    "nl": "nl-NL-FennaNeural",     "ja": "ja-JP-NanamiNeural",
    "sv": "sv-SE-SofieNeural",     "da": "da-DK-ChristelNeural",
    "pl": "pl-PL-AgnieszkaNeural", "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-SA-ZariyahNeural",   "zh": "zh-CN-XiaoxiaoNeural",
    "ko": "ko-KR-SunHiNeural",    "hi": "hi-IN-SwaraNeural",
    "tr": "tr-TR-EmelNeural",      "uk": "uk-UA-PolinaNeural",
    "el": "el-GR-AthinaNeural",    "he": "he-IL-HilaNeural",
    "hu": "hu-HU-NoemiNeural",     "ro": "ro-RO-AlinaNeural",
    "cs": "cs-CZ-VlastaNeural",    "fi": "fi-FI-SelmaNeural",
    "th": "th-TH-PremwadeeNeural", "vi": "vi-VN-HoaiMyNeural",
    "bg": "bg-BG-KalinaNeural",    "ca": "ca-ES-JoanaNeural",
    "et": "et-EE-AnuNeural",       "hr": "hr-HR-GabrijelaNeural",
    "id": "id-ID-GadisNeural",     "lt": "lt-LT-OnaNeural",
    "lv": "lv-LV-EveritaNeural",   "ms": "ms-MY-YasminNeural",
    "nb": "nb-NO-PernilleNeural",  "sk": "sk-SK-ViktoriaNeural",
    "sl": "sl-SI-PetraNeural",     "ta": "ta-IN-PallaviNeural",
    "te": "te-IN-ShrutiNeural",
}

# Audio capture constants (microphone → WebSocket)
SAMPLE_RATE      = 16000
CHANNELS         = 1
BITS_PER_SAMPLE  = 16
FRAMES_PER_CHUNK = 1600          # 100 ms of audio

# TTS playback constants
TTS_SAMPLE_RATE  = 24000

# ---- global TTS state ----
tts_is_playing = False
tts_end_time   = 0.0
_mic_vol       = None
last_non_spanish_lang = "en"     # default target when Spanish is spoken

# ═══════════════════════════════════════════════════════════════
#  MICROPHONE MUTE / UNMUTE  (pycaw – Windows only)
# ═══════════════════════════════════════════════════════════════
def _get_mic_vol():
    global _mic_vol
    if not PYCAW_AVAILABLE:
        return None
    if _mic_vol is None:
        try:
            dev = AudioUtilities.GetMicrophone()
            if dev:
                iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                _mic_vol = iface.QueryInterface(IAudioEndpointVolume)
        except Exception as e:
            print(f"[MIC] Error getting interface: {e}")
    return _mic_vol

def mute_microphone() -> bool:
    v = _get_mic_vol()
    if v:
        try:
            v.SetMute(1, None)
            print("[MIC] 🔇 Muted")
            return True
        except Exception as e:
            print(f"[MIC] Error muting: {e}")
    return False

def unmute_microphone() -> bool:
    v = _get_mic_vol()
    if v:
        try:
            v.SetMute(0, None)
            print("[MIC] 🔊 Unmuted")
            return True
        except Exception as e:
            print(f"[MIC] Error unmuting: {e}")
    return False

# ═══════════════════════════════════════════════════════════════
#  AZURE SPEECH PROTOCOL HELPERS
#
#  Text messages (WebSocket text frames):
#    Path: <path>\r\n
#    X-RequestId: <uuid>\r\n
#    X-Timestamp: <iso>\r\n
#    Content-Type: application/json; charset=utf-8\r\n
#    \r\n
#    <JSON body>
#
#  Binary messages (WebSocket binary frames):
#    [uint16-BE: header_length][header_bytes][body_bytes]
# ═══════════════════════════════════════════════════════════════
def _ts() -> str:
    """ISO 8601 timestamp in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _text_msg(path: str, rid: str, body) -> str:
    """Build a Speech Protocol text message (matching SDK header format)."""
    b = json.dumps(body) if isinstance(body, dict) else body
    return (
        f"Path: {path}\r\n"
        f"X-RequestId: {rid}\r\n"
        f"X-Timestamp: {_ts()}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"\r\n"
        f"{b}"
    )

def _audio_msg(rid: str, pcm: bytes) -> bytes:
    """Build a Speech Protocol binary audio message (matching SDK header format)."""
    hdr = (
        f"Path: audio\r\n"
        f"X-RequestId: {rid}\r\n"
        f"X-Timestamp: {_ts()}\r\n"
        f"Content-Type: audio/x-wav\r\n"
    ).encode("utf-8")
    return struct.pack(">H", len(hdr)) + hdr + pcm

def _parse_msg(raw: str):
    """Parse a Speech Protocol text message → (path, body_str)."""
    hdr_part, _, body = raw.partition("\r\n\r\n")
    path = ""
    for ln in hdr_part.split("\r\n"):
        if ln.lower().startswith("path:"):
            path = ln.split(":", 1)[1].strip()
    return path, body

def _wav_header() -> bytes:
    """44-byte RIFF/WAV header for streaming 16 kHz / 16-bit / mono PCM."""
    br = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8
    ba = CHANNELS * BITS_PER_SAMPLE // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0, b"WAVE",
        b"fmt ", 16, 1, CHANNELS, SAMPLE_RATE, br, ba, BITS_PER_SAMPLE,
        b"data", 0,
    )

# ═══════════════════════════════════════════════════════════════
#  SSML BUILDER
# ═══════════════════════════════════════════════════════════════
def _build_ssml(text: str, lang: str, voice: str) -> str:
    locale = LANGUAGES[lang][0] if lang in LANGUAGES else (
        f"{lang}-{lang.upper()}" if len(lang) == 2 else lang
    )
    # Personal Voice
    if voice == "personal":
        pid = os.getenv("SPEAKER_PROFILE_ID", "")
        return (
            f"<speak version='1.0' xml:lang='en-US' "
            f"xmlns='http://www.w3.org/2001/10/synthesis' "
            f"xmlns:mstts='http://www.w3.org/2001/mstts'>"
            f"<voice name='DragonLatestNeural'>"
            f"<mstts:ttsembedding speakerProfileId='{pid}'/>"
            f"<mstts:express-as style='Prompt'>"
            f"<lang xml:lang='{locale}'>{text}</lang>"
            f"</mstts:express-as></voice></speak>"
        )
    # Spanish voice (Ximena / Tristan)
    if lang == "es":
        return (
            f"<speak version='1.0' xml:lang='es-ES' "
            f"xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{voice}'>{text}</voice></speak>"
        )
    # Other language → neural voice from map
    nv = VOICE_MAP.get(lang, "en-US-AvaNeural")
    return (
        f"<speak version='1.0' xml:lang='{locale}' "
        f"xmlns='http://www.w3.org/2001/10/synthesis'>"
        f"<voice name='{nv}'>{text}</voice></speak>"
    )

# ═══════════════════════════════════════════════════════════════
#  TTS REST API – STREAMING PLAYBACK
#
#  POST /cognitiveservices/v1   →   raw-24khz-16bit-mono-pcm
#  Stream chunks to sounddevice.OutputStream for low-latency play
# ═══════════════════════════════════════════════════════════════
def synthesize_tts(text: str, lang: str, voice: str) -> bytes | None:
    """
    Call Azure TTS REST API. Streams audio to speakers as chunks arrive.
    Returns WAV bytes for the Streamlit audio widget.
    """
    global tts_is_playing, tts_end_time

    if not text:
        return None

    ssml = _build_ssml(text, lang, voice)
    url  = f"{TTS_BASE}/cognitiveservices/v1"
    hdrs = {
        **_auth_headers(),
        "Content-Type":            "application/ssml+xml",
        "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
        "User-Agent":              "SpeechTranslatorWS/1.0",
    }

    t0 = time.time()
    tts_is_playing = True
    mic_muted = mute_microphone()
    raw_pcm   = bytearray()

    try:
        print(f"[TTS] POST {url}  lang={lang}")
        resp = requests.post(
            url, headers=hdrs, data=ssml.encode("utf-8"),
            stream=True, timeout=30,
        )
        if resp.status_code != 200:
            print(f"[TTS] ❌ HTTP {resp.status_code}: {resp.text[:300]}")
            return None

        # ---- streaming playback via sounddevice ----
        stream = sd.OutputStream(
            samplerate=TTS_SAMPLE_RATE, channels=1,
            dtype="int16", blocksize=2400,       # 100 ms blocks
        )
        stream.start()
        first_byte = True
        leftover   = b""

        for chunk in resp.iter_content(chunk_size=9600):     # ~200 ms
            if not chunk:
                continue
            if first_byte:
                print(f"[TTS] ⏱️  First-byte latency: {time.time()-t0:.3f}s")
                first_byte = False

            data = leftover + chunk
            usable = len(data) - (len(data) % 2)   # int16 alignment
            if usable:
                samples = np.frombuffer(data[:usable], dtype=np.int16)
                stream.write(samples.reshape(-1, 1))  # blocking write → plays in real time
                raw_pcm.extend(data[:usable])
            leftover = data[usable:]

        # drain leftover bytes
        if leftover and len(leftover) >= 2:
            n = len(leftover) - (len(leftover) % 2)
            samples = np.frombuffer(leftover[:n], dtype=np.int16)
            stream.write(samples.reshape(-1, 1))
            raw_pcm.extend(leftover[:n])

        stream.stop()
        stream.close()
        duration = len(raw_pcm) / 2 / TTS_SAMPLE_RATE
        print(f"[TTS] ✅ Played {duration:.2f}s  (total {time.time()-t0:.3f}s)")

        # Build WAV for Streamlit st.audio() widget
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TTS_SAMPLE_RATE)
            wf.writeframes(bytes(raw_pcm))
        return buf.getvalue()

    except Exception as e:
        print(f"[TTS] ❌ {e}")
        import traceback
        traceback.print_exc()
        return None

    finally:
        if mic_muted:
            time.sleep(0.05)
            unmute_microphone()
        tts_is_playing = False
        tts_end_time = time.time()

# ═══════════════════════════════════════════════════════════════
#  RECOGNITION WORKER  (background thread)
#
#  1. Opens a WebSocket to Azure Speech Translation
#  2. Captures microphone audio via sounddevice
#  3. Sends audio → receives streaming translations
#  4. Calls TTS REST API for each final translation
# ═══════════════════════════════════════════════════════════════
def recognition_worker(
    stop_event: threading.Event,
    transcript_list: list,
    translation_list: list,
    status_dict: dict,
    ui_ping: threading.Event,
    synthesis_enabled: bool,
    voice_choice: str,
    audio_list: list,
    detected_langs_list: list,
    auto_detect_locales: list,
    selected_target_lang: str | None,
):
    global last_non_spanish_lang

    # Initialize COM for pycaw (Windows)
    if PYCAW_AVAILABLE:
        try:
            CoInitialize()
            print("[THREAD] COM initialized")
        except Exception:
            pass
    unmute_microphone()

    rid = uuid.uuid4().hex      # request id
    cid = uuid.uuid4().hex      # connection id

    # ---- Build WebSocket URL ----
    to_langs = list({k for k in LANGUAGES.keys()})
    to_csv   = ",".join(to_langs)

    # JS SDK TranslationConnectionFactory uses /stt/speech/universal/v2 on
    # stt.speech.microsoft.com with to/scenario as query params.
    # When using languageId (auto-detect), we omit 'from' — the server
    # detects the source language from the languageId block in speech.context.
    ws_url = (f"wss://{WS_HOST}/stt/speech/universal/v2"
              f"?to={to_csv}&scenario=conversation")

    ws_hdrs = {**_ws_auth_headers(), "X-ConnectionId": cid}
    print(f"[WS] Connecting: {ws_url}")

    # Synchronisation primitives
    ws_ready      = threading.Event()
    ws_error_flag = threading.Event()
    audio_started = [False]

    # ─────────── WebSocket callbacks ───────────
    def on_open(ws):
        print("[WS] ✅ Connected")

        # 1) speech.config – matching SDK format exactly
        cfg = {
            "context": {
                "system": {"version": "1.47.0", "name": "SpeechSDK", "build": "Python-WebSocket"},
                "os": {"name": "Windows", "version": "10", "platform": "Windows"},
                "audio": {"source": {"type": "Microphones", "samplerate": "16000",
                                     "bitspersample": "16", "channelcount": "1"}},
            }
        }
        ws.send(_text_msg("speech.config", rid, cfg))

        # 2) speech.context – EXACTLY matching SDK C++ log output
        #    Key fields from SDK sniffer:
        #    - phraseDetection.mode = "CONVERSATION" (uppercase!)
        #    - translation.output.includePassThroughResults = true
        #    - translation.onPassthrough.action = "None"
        #    - phraseOutput = None/None (when languageId is present)
        #    - audio.streams.1 = null
        ctx = {
            "phraseDetection": {
                "mode": "CONVERSATION",
                "onSuccess": {"action": "Translate"},
                "onInterim": {"action": "Translate"},
            },
            "translation": {
                "targetLanguages": to_langs,
                "output": {
                    "includePassThroughResults": True,
                    "interimResults": {"mode": "Always"},
                },
                "onSuccess": {"action": "None"},
                "onPassthrough": {"action": "None"},
            },
            "languageId": {
                "languages": auto_detect_locales,
                "onSuccess": {"action": "Recognize"},
                "onUnknown": {"action": "None"},
                "mode": "DetectContinuous",
                "Priority": "PrioritizeLatency",
            },
            "phraseOutput": {
                "interimResults": {"resultType": "None"},
                "phraseResults": {"resultType": "None"},
            },
            "audio": {"streams": {"1": None}},
        }
        ws.send(_text_msg("speech.context", rid, ctx))
        print(f"[WS] speech.context JSON: {json.dumps(ctx, indent=2)}")
        print("[WS] Sent speech.config + speech.context")
        ws_ready.set()

    def on_message(ws, message):
        """Handle text or binary messages from the server."""
        if isinstance(message, bytes):
            # Binary = synthesized audio from server (if text-to-speech feature enabled)
            return
        path, body = _parse_msg(message)

        if path in ("turn.start", "turn.end",
                     "speech.startDetected", "speech.endDetected"):
            print(f"[WS] {path}")
            return

        if path == "speech.hypothesis":
            try:
                d  = json.loads(body)
                pl = d.get("PrimaryLanguage", {})
                print(f"[WS] 🎤 Hypothesis ({pl.get('Language','?')}): "
                      f"{d.get('Text','')}")
            except Exception:
                pass
            return

        if path == "speech.phrase":
            # On V2 universal with translation context, translations arrive
            # via translation.response, NOT speech.phrase. Log for debug.
            try:
                d = json.loads(body)
                print(f"[WS] 🔍 speech.phrase (no translation expected here): "
                      f"status={d.get('RecognitionStatus')} text={d.get('DisplayText','')[:50]}")
            except Exception:
                pass
            return

        if path == "translation.phrase":
            try:
                wrapper = json.loads(body)
                _handle_translation_response(wrapper)
            except Exception as e:
                print(f"[WS] ❌ translation.phrase parse error: {e}")
            return

        if path == "translation.response":
            # SDK log shows format:
            # { "Extensions":[...], "SpeechPhrase":{...}, "TranslationStatus":"Success",
            #   "Translations":[{"DisplayText":"...","Language":"es"}, ...] }
            # Translations are at ROOT level, not inside SpeechPhrase!
            try:
                wrapper = json.loads(body)
                exts = wrapper.get("Extensions", [])
                if "SpeechPhrase" in exts or "SpeechPhrase" in wrapper:
                    # Final phrase with translations
                    _handle_translation_response(wrapper)
                elif "SpeechHypothesis" in exts or "SpeechHypothesis" in wrapper:
                    # Interim hypothesis with translations
                    hyp = wrapper.get("SpeechHypothesis", {})
                    pl  = hyp.get("PrimaryLanguage", {})
                    trs = wrapper.get("Translations", [])
                    tr_preview = "; ".join(
                        f"{t.get('Language','?')}:{t.get('DisplayText','')[:30]}"
                        for t in trs[:3]
                    )
                    print(f"[WS] 🎤 TransHyp ({pl.get('Language','?')}): "
                          f"{hyp.get('Text','')}  → {tr_preview}")
            except Exception as e:
                print(f"[WS] ❌ translation.response parse error: {e}")
            return

        if path == "translation.hypothesis":
            # Alternate path name for hypotheses
            try:
                wrapper = json.loads(body)
                hyp = wrapper.get("SpeechHypothesis", wrapper)
                pl  = hyp.get("PrimaryLanguage", {})
                trs = wrapper.get("Translations", [])
                tr_preview = "; ".join(
                    f"{t.get('Language','?')}:{t.get('DisplayText','')[:30]}"
                    for t in trs[:3]
                )
                print(f"[WS] 🎤 TransHyp ({pl.get('Language','?')}): "
                      f"{hyp.get('Text','')}  → {tr_preview}")
            except Exception:
                pass
            return

        # Unknown / debug
        print(f"[WS] {path}: {body[:200]}")

    # ─────────── Handle final recognition ───────────
    def _handle_translation_response(wrapper: dict):
        """Handle a translation.response message from the server.

        SDK log reveals the wire format:
        {
          "Extensions": ["TranslationSourceRef", "SpeechPhrase"],
          "SpeechPhrase": {
            "Id": "...", "RecognitionStatus": "Success",
            "DisplayText": "Umbrella.", "PrimaryLanguage": {"Language": "en-US", "Confidence": "Low"}
          },
          "TranslationStatus": "Success",
          "Translations": [
            {"Id": "...", "DisplayText": "Paraguas.", "Language": "es"},
            {"Id": "...", "DisplayText": "Parapluie.", "Language": "fr"}
          ]
        }
        """
        global last_non_spanish_lang

        if _echo_guard():
            return

        phrase = wrapper.get("SpeechPhrase", {})
        print(f"[WS] 🔍 wrapper keys: {list(wrapper.keys())}")

        status = phrase.get("RecognitionStatus", "")
        if status != "Success":
            print(f"[WS] RecognitionStatus: {status}")
            return

        text = phrase.get("DisplayText", "") or phrase.get("Text", "")
        if not text:
            return

        # Detected language
        pl         = phrase.get("PrimaryLanguage", {})
        det_lang   = pl.get("Language", "unknown")
        confidence = pl.get("Confidence", "Unknown")
        print(f"[WS] 🎯 Recognized ({det_lang}, {confidence}): {text}")

        # Translation status and translations are at ROOT level
        tr_status = wrapper.get("TranslationStatus", "")
        translations = wrapper.get("Translations", [])
        print(f"[WS] 🔍 TranslationStatus: {tr_status}, "
              f"Translations count: {len(translations)}")

        status_dict["language"]   = det_lang
        status_dict["confidence"] = confidence

        # Determine translation target
        is_spanish = det_lang.startswith("es")
        if not is_spanish:
            last_non_spanish_lang = det_lang.split("-")[0]
            status_dict["last_non_spanish"] = last_non_spanish_lang
            target_lang = "es"
        else:
            target_lang = selected_target_lang or last_non_spanish_lang

        # Find translation in result – field is "DisplayText" not "Text"
        tr_text = None
        for t in translations:
            if t.get("Language") == target_lang:
                tr_text = t.get("DisplayText") or t.get("Text")
                break

        if tr_text:
            print(f"[WS] 📝 Translation → ({target_lang}): {tr_text}")
        else:
            avail = [f"{t.get('Language')}:{t.get('DisplayText','')[:30]}"
                     for t in translations]
            print(f"[WS] ⚠️ No translation for '{target_lang}'. "
                  f"Available: {avail}")

        # Update shared lists
        transcript_list.insert(0, text)
        translation_list.insert(0, {target_lang: tr_text} if tr_text else {})
        detected_langs_list.insert(0, det_lang)

        # Synthesise translation → streaming playback
        if synthesis_enabled and tr_text:
            syn_lang = "es" if not is_spanish else target_lang
            try:
                wav = synthesize_tts(tr_text, syn_lang, voice_choice)
                audio_list.insert(0, wav)
            except Exception as e:
                print(f"[TTS] ❌ {e}")
                audio_list.insert(0, None)
        elif synthesis_enabled:
            audio_list.insert(0, None)     # placeholder so indices match

        ui_ping.set()

    def _echo_guard() -> bool:
        """Discard text recognised while TTS is playing (echo)."""
        if tts_is_playing:
            print("[WS] ⚠️ Discarded (TTS playing)")
            return True
        dt = time.time() - tts_end_time
        window = 0.1 if PYCAW_AVAILABLE else 0.5
        if tts_end_time > 0 and dt < window:
            print(f"[WS] ⚠️ Discarded (TTS ended {dt:.3f}s ago)")
            return True
        return False

    def on_error(ws, err):
        print(f"[WS] ❌ Error: {err}")
        ws_error_flag.set()

    def on_close(ws, code, msg):
        print(f"[WS] 🔴 Closed  code={code}  msg={msg}")

    # ─────────── Connect WebSocket ───────────
    ws = websocket.WebSocketApp(
        ws_url,
        header=ws_hdrs,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    ws_thread = threading.Thread(
        target=ws.run_forever, daemon=True,
        kwargs={"ping_interval": 20, "ping_timeout": 10},
    )
    ws_thread.start()

    if not ws_ready.wait(timeout=15):
        print("[WS] ❌ Connection timeout")
        if PYCAW_AVAILABLE:
            try: CoUninitialize()
            except: pass
        return

    if ws_error_flag.is_set():
        print("[WS] ❌ Connection failed")
        if PYCAW_AVAILABLE:
            try: CoUninitialize()
            except: pass
        return

    # ─────────── Audio capture (microphone) ───────────
    print("[AUDIO] Starting microphone capture…")

    def audio_callback(indata, frames, time_info, status):
        """sounddevice callback – sends PCM to the WebSocket."""
        if stop_event.is_set() or ws_error_flag.is_set():
            return
        pcm = indata.tobytes()
        try:
            if not audio_started[0]:
                # First chunk: prepend 44-byte WAV header
                ws.send(
                    _audio_msg(rid, _wav_header() + pcm),
                    opcode=websocket.ABNF.OPCODE_BINARY,
                )
                audio_started[0] = True
                print("[AUDIO] Sent first chunk with WAV header")
            else:
                ws.send(
                    _audio_msg(rid, pcm),
                    opcode=websocket.ABNF.OPCODE_BINARY,
                )
        except Exception as e:
            if not stop_event.is_set():
                print(f"[AUDIO] ❌ Send error: {e}")

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAMES_PER_CHUNK,
            callback=audio_callback,
        ):
            print("[AUDIO] ✅ Microphone capturing")
            while not stop_event.is_set():
                time.sleep(0.1)
    except sd.PortAudioError as e:
        print(f"[AUDIO] ❌ PortAudio error (no microphone?): {e}")
    except Exception as e:
        print(f"[AUDIO] ❌ {e}")

    # ─────────── Cleanup ───────────
    print("[WS] Stopping…")
    try:
        # Send end-of-stream marker (zero-length audio)
        ws.send(_audio_msg(rid, b""), opcode=websocket.ABNF.OPCODE_BINARY)
    except Exception:
        pass

    ws.close()
    unmute_microphone()

    if PYCAW_AVAILABLE:
        try:
            CoUninitialize()
            print("[THREAD] COM uninitialized")
        except Exception:
            pass

    print("[WS] ✅ Worker stopped")


# ═══════════════════════════════════════════════════════════════
#  STREAMLIT UI
# ═══════════════════════════════════════════════════════════════
st.markdown(
    """<style>
    [data-testid="stSidebar"]{min-width:240px;max-width:240px}
    [data-testid="stSidebarContent"]{padding:1rem}
    </style>""",
    unsafe_allow_html=True,
)

st.set_page_config(page_title="Live Speech Translator (WS)", layout="wide")

if os.path.exists("microsoft.png"):
    st.image("microsoft.png", width=100)

st.title("🎙️ Real-time Speech-to-Speech Translation")
st.caption("WebSocket + REST API  •  No SDK  •  Automatic language detection  •  Streaming TTS")
st.write(
    "Click **Start** and speak into your microphone. "
    "The system will transcribe, translate, and synthesise your speech in real time."
)

# ---- one-time session state initialisation ----
if "transcript" not in st.session_state:
    st.session_state.transcript     = []
    st.session_state.translation    = []
    st.session_state.audio          = []
    st.session_state.detected_langs = []
    st.session_state.status         = {"language": None, "confidence": None}
    st.session_state.thread         = None
    st.session_state.stop_event     = None
    st.session_state.ui_ping        = threading.Event()

is_recording = (
    st.session_state.thread is not None
    and st.session_state.thread.is_alive()
)

# ---- sidebar ----
with st.sidebar:
    st.markdown("### Language Detection Settings")

    lang_options = ["None (All languages)"] + [
        f"{code} - {name}" for code, (_, name) in LANGUAGES.items()
    ]
    selected_option = st.selectbox(
        "Select the languages to identify:",
        lang_options, index=0, disabled=is_recording,
        help="Select 'None' to detect all languages, or restrict to a specific language + Spanish",
    )

    if selected_option == "None (All languages)":
        selected_language = None
        auto_detect_locales = [v[0] for v in LANGUAGES.values()]
    else:
        selected_language = selected_option.split(" - ")[0]
        loc = LANGUAGES[selected_language][0]
        auto_detect_locales = (
            [loc, PRIMARY_LANGUAGE] if loc != PRIMARY_LANGUAGE else [PRIMARY_LANGUAGE]
        )

    st.caption(f"🔍 Detection locales: {', '.join(auto_detect_locales)}")

    st.markdown("---")
    st.markdown("### Synthesis Settings")
    synthesis_enabled = st.checkbox(
        "Enable TTS synthesis", True, disabled=is_recording,
        help="Synthesise translations with text-to-speech (streaming playback)",
    )

    if synthesis_enabled:
        voice_display_name = st.selectbox(
            "Select voice",
            list(VOICE_OPTIONS.keys()), index=0, disabled=is_recording,
        )
        selected_voice = VOICE_OPTIONS[voice_display_name]
    else:
        selected_voice = VOICE_OPTIONS["Ximena (Spanish Female)"]

# ---- toggle button ----
button_label = "⏹️ Stop" if is_recording else "▶️ Start"

if st.button(button_label, type="primary"):
    if is_recording:
        st.session_state.stop_event.set()
    else:
        # Start fresh
        st.session_state.transcript.clear()
        st.session_state.translation.clear()
        st.session_state.audio.clear()
        st.session_state.detected_langs.clear()
        st.session_state.status = {"language": None, "confidence": None}

        st.session_state.stop_event = threading.Event()
        st.session_state.ui_ping    = threading.Event()

        st.session_state.thread = threading.Thread(
            target=recognition_worker,
            daemon=True,
            args=(
                st.session_state.stop_event,
                st.session_state.transcript,
                st.session_state.translation,
                st.session_state.status,
                st.session_state.ui_ping,
                synthesis_enabled,
                selected_voice,
                st.session_state.audio,
                st.session_state.detected_langs,
                auto_detect_locales,
                selected_language,
            ),
        )
        st.session_state.thread.start()

    do_rerun()

# ---- detected language + confidence ----
lang = st.session_state.status.get("language")
conf = st.session_state.status.get("confidence")
if lang:
    if conf == "Unknown":
        st.markdown(f"**Detected language:** {get_language_name(lang)}")
    else:
        st.markdown(
            f"**Detected language:** {get_language_name(lang)}"
            f"  •  **Confidence:** {conf}"
        )
else:
    st.write("")

# ---- immediate refresh when new text arrives ----
if st.session_state.ui_ping.is_set():
    st.session_state.ui_ping.clear()
    do_rerun()

# Fallback 1-second heartbeat
st_autorefresh(interval=1000, key="heartbeat")

# ---- two-column live output ----
col_l, col_r = st.columns(2)

with col_l:
    st.subheader("Latest transcription")
    if st.session_state.transcript:
        total = len(st.session_state.transcript)
        dl    = st.session_state.detected_langs
        for idx, sent in enumerate(st.session_state.transcript):
            num = total - idx
            lc  = dl[idx].split("-")[0] if idx < len(dl) else ""
            st.markdown(f"**{num}. ({lc})** {sent}")
    else:
        st.info("Press 'Start' and speak…")

with col_r:
    st.subheader("Latest translations")
    if st.session_state.translation:
        total = len(st.session_state.translation)
        for idx, tdict in enumerate(st.session_state.translation):
            num = total - idx
            for lang_code, txt in tdict.items():
                st.markdown(f"**{num}. ({lang_code})** {txt}")

            if synthesis_enabled and idx < len(st.session_state.audio):
                a = st.session_state.audio[idx]
                if a:
                    st.audio(a, format="audio/wav")

            st.write("---")
    else:
        st.info("Translations will appear here.")

# ---- footer ----
st.caption(
    "Source language: auto-detected  •  "
    "Powered by Azure Speech Service (WebSocket + REST — no SDK)"
)
