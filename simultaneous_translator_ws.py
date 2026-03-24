# ────────────────────────────────────────────────────────────────────
# simultaneous_translator_ws.py
# Minimal Real-time Speech Translation via WebSocket
# *** No Azure Speech SDK required ***
#
# This is a minimal, console-only demonstration of how to connect
# to the Azure Speech Translation WebSocket API and perform
# continuous, bidirectional Spanish ↔ English translation.
#
# It is intentionally kept simple (~250 lines) to serve as a
# learning reference for the underlying wire protocol.  For the
# full-featured version (Streamlit UI, TTS, Entra ID, multi-
# language) see simultaneous_translator_ws_app.py.
#
# Features:
#   • Continuous speech recognition with automatic language detection
#   • Translates Spanish ↔ English (detects which one you speak
#     and shows the translation to the other language)
#   • Console-only output — no web UI
#   • API-key authentication only
#   • Automatic reconnection with exponential backoff
#
# Streaming behaviour:
#   The server sends translations incrementally: every interim
#   hypothesis (SpeechHypothesis) already includes a Translations[]
#   array with partial translations that update in real time as the
#   user speaks.  This app only DISPLAYS the final translation
#   (SpeechPhrase) for clarity, but the interim translations are
#   available in the same wrapper["Translations"] field and could
#   be shown for a "live subtitles" experience.
#
# Prerequisites:
#   pip install websocket-client sounddevice numpy python-dotenv
#
# Usage:
#   1. Set SPEECH_KEY and SPEECH_REGION in .env
#   2. python simultaneous_translator_ws.py
#   3. Speak into your microphone — Ctrl+C to stop
# ────────────────────────────────────────────────────────────────────

import os
import sys
import json
import uuid
import struct
import time
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
import websocket                       # pip install websocket-client
import sounddevice as sd               # pip install sounddevice
import numpy as np                     # pip install numpy

# ═══════════════════════════════════════════════════════════════════
#  CONFIGURATION
#
#  We only need two environment variables for API-key auth:
#    SPEECH_KEY    – Azure Speech / AI Services API key
#    SPEECH_REGION – Azure region (e.g. "westeurope", "eastus")
# ═══════════════════════════════════════════════════════════════════
load_dotenv(override=True)

SPEECH_KEY    = os.getenv("SPEECH_KEY")
SPEECH_REGION = os.getenv("SPEECH_REGION")

if not SPEECH_KEY or not SPEECH_REGION:
    print("❌  Missing SPEECH_KEY or SPEECH_REGION in .env")
    sys.exit(1)

# ── Audio capture constants ──
# The Azure Speech service expects 16 kHz, 16-bit, mono PCM audio.
SAMPLE_RATE      = 16000       # 16 kHz sample rate (required by the service)
CHANNELS         = 1           # Mono audio
BITS_PER_SAMPLE  = 16          # 16-bit signed integer samples
FRAMES_PER_CHUNK = 1600        # 1600 samples = 100 ms of audio per chunk

# ── Language configuration ──
# TARGET_LANGUAGES: the set of language codes the service will translate TO.
# AUTO_DETECT_LOCALES: the BCP-47 locales the service should look for in the
# incoming audio.  With DetectContinuous mode the service re-evaluates the
# spoken language on every utterance, so the speaker can freely switch
# between Spanish and English mid-conversation.
TARGET_LANGUAGES    = ["es", "en"]
AUTO_DETECT_LOCALES = ["es-ES", "en-US"]


# ═══════════════════════════════════════════════════════════════════
#  AZURE SPEECH WEBSOCKET PROTOCOL HELPERS
#
#  The Azure Speech service uses a custom sub-protocol over WebSocket.
#  There are two frame types:
#
#  TEXT FRAMES  (for control messages):
#    Path: <message-type>\r\n
#    X-RequestId: <uuid>\r\n
#    X-Timestamp: <iso8601>\r\n
#    Content-Type: application/json; charset=utf-8\r\n
#    \r\n
#    <JSON body>
#
#  BINARY FRAMES  (for audio data):
#    [uint16 big-endian: header_length][header bytes][audio bytes]
#    The header has the same Path/RequestId/Timestamp/ContentType
#    format, but is encoded as raw bytes prepended to the PCM data.
# ═══════════════════════════════════════════════════════════════════

def _ts() -> str:
    """Return the current UTC time as an ISO 8601 timestamp.

    The Azure Speech protocol requires every message to carry an
    X-Timestamp header in this format.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text_msg(path: str, rid: str, body) -> str:
    """Build a Speech Protocol *text* message (sent as a WebSocket text frame).

    Parameters
    ----------
    path : str
        The message type, e.g. "speech.config" or "speech.context".
    rid : str
        The Request-ID (a hex UUID) that ties all messages in a session.
    body : dict | str
        The JSON payload.  Dicts are serialised automatically.

    Returns
    -------
    str
        A complete text-frame string ready to send over the WebSocket.
    """
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
    """Build a Speech Protocol *binary* audio message.

    The binary frame layout is:
      [2 bytes, big-endian] length of the text header
      [N bytes]             the text header (Path, RequestId, etc.)
      [remaining bytes]     raw PCM audio data

    Parameters
    ----------
    rid : str
        The Request-ID (must match the one used for text messages).
    pcm : bytes
        Raw PCM audio bytes (or WAV header + PCM for the first chunk).
    """
    hdr = (
        f"Path: audio\r\n"
        f"X-RequestId: {rid}\r\n"
        f"X-Timestamp: {_ts()}\r\n"
        f"Content-Type: audio/x-wav\r\n"
    ).encode("utf-8")
    # Pack header length as a 2-byte big-endian unsigned short,
    # followed by the header, then the audio payload.
    return struct.pack(">H", len(hdr)) + hdr + pcm


def _parse_msg(raw: str):
    """Parse an incoming Speech Protocol text message.

    Returns
    -------
    tuple[str, str]
        (path, body) — the message type and the JSON body string.
    """
    hdr_part, _, body = raw.partition("\r\n\r\n")
    path = ""
    for ln in hdr_part.split("\r\n"):
        if ln.lower().startswith("path:"):
            path = ln.split(":", 1)[1].strip()
    return path, body


def _wav_header() -> bytes:
    """Generate a 44-byte RIFF/WAV header for streaming PCM audio.

    The header describes 16 kHz / 16-bit / mono PCM.  The data-size
    fields are set to 0 because we are streaming (unknown total length).
    The Azure service accepts this "streaming WAV" format: it reads the
    fmt chunk to learn the audio parameters and then treats all
    subsequent bytes as raw PCM samples.
    """
    byte_rate   = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8   # 32000
    block_align = CHANNELS * BITS_PER_SAMPLE // 8                 # 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0,              # RIFF chunk (size=0 → streaming)
        b"WAVE",                 # Format identifier
        b"fmt ", 16,             # fmt sub-chunk (16 bytes for PCM)
        1,                       # AudioFormat = 1 (PCM)
        CHANNELS,                # NumChannels = 1 (mono)
        SAMPLE_RATE,             # SampleRate  = 16000
        byte_rate,               # ByteRate    = 32000
        block_align,             # BlockAlign  = 2
        BITS_PER_SAMPLE,         # BitsPerSample = 16
        b"data", 0,             # data sub-chunk (size=0 → streaming)
    )


# ═══════════════════════════════════════════════════════════════════
#  MAIN APPLICATION LOOP  (with automatic reconnection)
#
#  Flow:
#    1. Open a WebSocket to Azure Speech Translation (Universal V2)
#    2. Send speech.config  → tells the service about our client/audio
#    3. Send speech.context → configures translation, language detection
#    4. Open the microphone and stream PCM audio in binary frames
#    5. Receive translation.response messages and print to console
#    6. On disconnect → wait briefly → reconnect automatically
#    7. Ctrl+C → send end-of-stream marker → close WebSocket → exit
#
#  The reconnection loop uses exponential backoff (1s → 2s → 4s …
#  up to 30s) to avoid hammering the server.  Each reconnection
#  generates fresh request/connection IDs.
# ═══════════════════════════════════════════════════════════════════

# Maximum number of consecutive reconnection attempts before giving up.
# Set to 0 for unlimited retries (only Ctrl+C stops the app).
MAX_RECONNECT_ATTEMPTS = 0

def main():
    # ── Build the WebSocket URL (constant across reconnections) ──
    # Endpoint: wss://{region}.stt.speech.microsoft.com/stt/speech/universal/v2
    #
    # IMPORTANT: The path MUST start with /stt/ — without it, speech
    # recognition works but translation silently fails.
    #
    # Query parameters:
    #   to       = comma-separated target language codes (es,en)
    #   scenario = "conversation" (optimised for conversational speech)
    #
    # Note: we do NOT include a "from" parameter because we use
    # automatic language detection (languageId in speech.context).
    to_csv = ",".join(TARGET_LANGUAGES)
    ws_url = (
        f"wss://{SPEECH_REGION}.stt.speech.microsoft.com"
        f"/stt/speech/universal/v2"
        f"?to={to_csv}&scenario=conversation"
    )

    # ── Global stop signal ──
    # This event is shared across all reconnection attempts.
    # It is only set by Ctrl+C (KeyboardInterrupt).
    stop_event = threading.Event()

    # ── Reconnection loop ──
    # Each iteration creates a fresh WebSocket session with new IDs.
    # On disconnect (ping/pong timeout, server close, network error),
    # we wait with exponential backoff and try again.
    attempt = 0
    backoff = 1.0          # seconds, doubles on each failure (max 30s)

    try:
        while not stop_event.is_set():
            attempt += 1
            if MAX_RECONNECT_ATTEMPTS and attempt > MAX_RECONNECT_ATTEMPTS:
                print(f"\n❌  Gave up after {MAX_RECONNECT_ATTEMPTS} reconnection attempts")
                break

            if attempt > 1:
                wait = min(backoff, 30.0)
                print(f"\n🔄  Reconnecting in {wait:.0f}s… (attempt {attempt})")
                # Sleep in small steps so Ctrl+C is responsive
                for _ in range(int(wait * 10)):
                    if stop_event.is_set():
                        break
                    time.sleep(0.1)
                if stop_event.is_set():
                    break
                backoff = min(backoff * 2, 30.0)

            success = _run_session(ws_url, stop_event)

            if success:
                # Session ran for a while before dropping → reset backoff
                backoff = 1.0

    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped by user")
    finally:
        stop_event.set()
        print("👋  Bye!")


def _run_session(ws_url: str, stop_event: threading.Event) -> bool:
    """Run a single WebSocket translation session.

    Returns True if the session ran successfully for at least one
    recognition (used to decide whether to reset the backoff timer).
    Returns False if the connection failed immediately.
    """
    # Generate fresh unique IDs for this session.
    # The request-id (rid) ties every message in the session together.
    # The connection-id (cid) identifies this WebSocket connection.
    rid = uuid.uuid4().hex
    cid = uuid.uuid4().hex

    # ── Authentication headers ──
    # For API-key auth, we send the key in the Ocp-Apim-Subscription-Key
    # header.  X-ConnectionId is required by the protocol.
    ws_hdrs = {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "X-ConnectionId": cid,
    }

    # ── Per-session synchronisation ──
    # ws_ready      → set once the WebSocket on_open has sent config/context
    # ws_error_flag → set if the WebSocket encounters a fatal error
    # audio_started → mutable flag (list) so the audio callback knows
    #                 whether the first chunk (with WAV header) has been sent
    # got_result    → set when at least one successful recognition arrives
    #                 (used to decide backoff reset on reconnection)
    ws_ready      = threading.Event()
    ws_error_flag = threading.Event()
    audio_started = [False]
    got_result    = [False]

    # ───────────────────────────────────────────────────────────────
    #  WEBSOCKET CALLBACKS
    # ───────────────────────────────────────────────────────────────

    def on_open(ws):
        """Called when the WebSocket connection is established.

        We must send two mandatory text messages before streaming audio:

        1. speech.config  – Describes the client system and audio source.
           This message is required by the protocol; without it the
           server may reject audio frames.

        2. speech.context – Configures HOW the service should process
           the audio.  This is where we set up:
             • phraseDetection  → recognition mode (CONVERSATION)
             • translation      → target languages + passthrough settings
             • languageId       → automatic source language detection
             • phraseOutput     → suppresses duplicate phrase events
             • audio.streams    → required placeholder (null = default)
        """
        print("✅  WebSocket connected\n")

        # ── 1) speech.config ──
        # Tells the server about our client environment and audio format.
        # The "system.name" = "SpeechSDK" makes the server treat us like
        # a real SDK client, which is important for the response format.
        cfg = {
            "context": {
                "system": {
                    "version": "1.47.0",
                    "name": "SpeechSDK",
                    "build": "Python-WebSocket",
                },
                "os": {"name": "Windows", "version": "10", "platform": "Windows"},
                "audio": {
                    "source": {
                        "type": "Microphones",
                        "samplerate": "16000",
                        "bitspersample": "16",
                        "channelcount": "1",
                    }
                },
            }
        }
        ws.send(_text_msg("speech.config", rid, cfg))

        # ── 2) speech.context ──
        # This is the most critical message — it determines what the
        # service does with the recognised speech.
        ctx = {
            # --- phraseDetection ---
            # mode: "CONVERSATION" — must be UPPERCASE (SDK wire format).
            # onSuccess/onInterim → "Translate": tells the service to
            # run translation on both final and interim results.
            #
            # conversation.segmentation: controls how the service decides
            #   when an utterance (phrase) ends.  Modes:
            #     "Normal"   — default silence-based segmentation
            #     "Semantic" — segments on sentence-ending punctuation
            #                  (e.g. '.', '?') instead of silence alone,
            #                  reducing over/under-segmentation
            #     "Custom"   — lets you set segmentationSilenceTimeoutMs
            #                  and segmentationForcedTimeoutMs manually
            #     "Disabled" — no automatic segmentation
            #   This is the WebSocket equivalent of the SDK property:
            #     Speech_SegmentationStrategy = "Semantic"
            "phraseDetection": {
                "mode": "CONVERSATION",
                "conversation": {
                    "segmentation": {
                        "mode": "Semantic",
                    }
                },
                "onSuccess": {"action": "Translate"},
                "onInterim": {"action": "Translate"},
            },

            # --- translation ---
            # targetLanguages: which languages to translate into.
            # includePassThroughResults: CRITICAL — without this, the
            #   server omits the Translations array from responses.
            # interimResults.mode "Always": get translations even for
            #   partial/interim hypotheses (useful for live subtitles).
            # onPassthrough.action "None": required by the protocol
            #   when includePassThroughResults is true.
            "translation": {
                "targetLanguages": TARGET_LANGUAGES,
                "output": {
                    "includePassThroughResults": True,
                    "interimResults": {"mode": "Always"},
                },
                "onSuccess": {"action": "None"},
                "onPassthrough": {"action": "None"},
            },

            # --- languageId ---
            # Enables automatic source-language detection so the user
            # can speak either Spanish or English without switching.
            # mode "DetectContinuous": re-evaluates the language for
            #   every utterance (vs. "DetectAtAudioStart" = once only).
            # Priority "PrioritizeLatency": faster detection at the
            #   cost of slightly lower accuracy.
            "languageId": {
                "languages": AUTO_DETECT_LOCALES,
                "onSuccess": {"action": "Recognize"},
                "onUnknown": {"action": "None"},
                "mode": "DetectContinuous",
                "Priority": "PrioritizeLatency",
            },

            # --- phraseOutput ---
            # When languageId is present, we suppress the separate
            # speech.phrase events (set resultType to "None") because
            # all results come through translation.response instead.
            "phraseOutput": {
                "interimResults": {"resultType": "None"},
                "phraseResults": {"resultType": "None"},
            },

            # --- audio.streams ---
            # Required placeholder — setting stream "1" to null tells
            # the service to use the default audio stream.
            "audio": {"streams": {"1": None}},
        }
        ws.send(_text_msg("speech.context", rid, ctx))

        # Signal that the WebSocket is ready to receive audio.
        ws_ready.set()

    def on_message(ws, message):
        """Called for every message received from the server.

        The server sends several message types (identified by the Path
        header).  The most important for us are:

        • translation.response  (Path: translation.response)
          Contains either:
          - An interim hypothesis (Extensions includes "SpeechHypothesis")
            → We show partial text as a live "typing" indicator.
          - A final phrase    (Extensions includes "SpeechPhrase")
            → We extract the recognised text + translations.

        • Informational messages (turn.start, turn.end, speech.startDetected,
          speech.endDetected, speech.hypothesis, speech.phrase,
          translation.hypothesis) → We ignore these silently.

        Binary messages (server-side TTS audio) are also ignored since
        this minimal version does not use TTS.
        """
        # Ignore binary frames (server TTS audio, not used here)
        if isinstance(message, bytes):
            return

        path, body = _parse_msg(message)

        # ── translation.response ──
        # This is the main path for both interim and final results.
        if path == "translation.response":
            try:
                wrapper = json.loads(body)
                exts = wrapper.get("Extensions", [])

                # --- Interim hypothesis ---
                # The server sends frequent partial results as the user
                # speaks.  We show them on the same console line (with
                # carriage return \r) so they overwrite each other,
                # creating a "live typing" effect.
                #
                # NOTE ON STREAMING TRANSLATIONS:
                # Each SpeechHypothesis message ALSO carries a root-level
                # Translations[] array with the partial translation of
                # the text recognised so far.  This means the server
                # streams translations in real time — not just the
                # final result.  We intentionally show only the
                # recognised text here (for console clarity), but you
                # could extract wrapper["Translations"] to display
                # live-updating translated subtitles.  Example:
                #   trs = wrapper.get("Translations", [])
                #   for t in trs:
                #       print(f"  ({t['Language']}) {t['DisplayText']}")
                if "SpeechHypothesis" in exts or "SpeechHypothesis" in wrapper:
                    hyp = wrapper.get("SpeechHypothesis", {})
                    text = hyp.get("Text", "")
                    if text:
                        now = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{now}] 🎤  Hypothesis: {text}", end="\r")
                    return

                # --- Final phrase with translations ---
                # When the server is confident about a complete utterance,
                # it sends a response with "SpeechPhrase" in Extensions.
                # This contains the final recognised text and the full
                # set of translations.
                if "SpeechPhrase" in exts or "SpeechPhrase" in wrapper:
                    _handle_final(wrapper)
                    return

            except Exception as e:
                print(f"\n❌  Parse error: {e}")
            return

        # ── translation.phrase ──
        # An alternative path the server sometimes uses for final results.
        if path == "translation.phrase":
            try:
                _handle_final(json.loads(body))
            except Exception as e:
                print(f"\n❌  Parse error: {e}")
            return

        # ── Informational / lifecycle messages — ignore silently ──
        if path in ("turn.start", "turn.end",
                     "speech.startDetected", "speech.endDetected",
                     "speech.hypothesis", "speech.phrase",
                     "translation.hypothesis"):
            return

    def _handle_final(wrapper: dict):
        """Process a final translation.response that contains SpeechPhrase.

        The wire format (discovered by sniffing the real SDK traffic) is:

        {
          "Extensions": ["TranslationSourceRef", "SpeechPhrase"],
          "SpeechPhrase": {
            "RecognitionStatus": "Success",
            "DisplayText": "Hello, how are you?",
            "PrimaryLanguage": {
              "Language": "en-US",
              "Confidence": "High"
            }
          },
          "TranslationStatus": "Success",
          "Translations": [
            {"Language": "es", "DisplayText": "Hola, ¿cómo estás?"},
            {"Language": "en", "DisplayText": "Hello, how are you?"}
          ]
        }

        KEY INSIGHT: Translations are at the ROOT level of the JSON
        envelope, NOT inside SpeechPhrase.  The "DisplayText" field
        (not "Text") contains the formatted translation.
        """
        phrase = wrapper.get("SpeechPhrase", {})
        status = phrase.get("RecognitionStatus", "")

        # Only process successfully recognised phrases
        if status != "Success":
            return

        text = phrase.get("DisplayText", "") or phrase.get("Text", "")
        if not text:
            return

        # ── Determine the detected language ──
        # PrimaryLanguage.Language is a BCP-47 locale like "en-US" or "es-ES".
        pl       = phrase.get("PrimaryLanguage", {})
        det_lang = pl.get("Language", "unknown")

        # ── Pick the OPPOSITE language for translation ──
        # If the user spoke Spanish → show English translation
        # If the user spoke English → show Spanish translation
        is_spanish = det_lang.startswith("es")
        target = "en" if is_spanish else "es"

        # ── Find the matching translation in the Translations array ──
        translations = wrapper.get("Translations", [])
        tr_text = None
        for t in translations:
            if t.get("Language") == target:
                tr_text = t.get("DisplayText") or t.get("Text")
                break

        # Mark this session as productive (for reconnection backoff reset)
        got_result[0] = True

        # ── Print results to console ──
        now = datetime.now().strftime("%H:%M:%S")
        # Pad with spaces to overwrite any leftover hypothesis text
        print(f"  [{now}] ✅  [{det_lang}] {text}                    ")
        if tr_text:
            print(f"  [{now}] 📝  [{target}]  → {tr_text}")
        else:
            avail = [f"{t.get('Language')}:{t.get('DisplayText','')[:40]}"
                     for t in translations]
            print(f"  [{now}] ⚠️   No '{target}' translation. Available: {avail}")
        print()   # Blank line between utterances for readability

    def on_error(ws, err):
        """Called when the WebSocket encounters an error."""
        print(f"\n❌  WebSocket error: {err}")
        ws_error_flag.set()

    def on_close(ws, code, msg):
        """Called when the WebSocket connection is closed."""
        print(f"\n🔴  WebSocket closed (code={code})")
        ws_error_flag.set()   # signal the audio loop to stop

    # ───────────────────────────────────────────────────────────────
    #  CONNECT THE WEBSOCKET
    # ───────────────────────────────────────────────────────────────
    print(f"🔌  Connecting to {ws_url[:80]}…")

    ws = websocket.WebSocketApp(
        ws_url,
        header=ws_hdrs,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    # Run the WebSocket event loop in a daemon thread so the main
    # thread can handle Ctrl+C and microphone management.
    ws_thread = threading.Thread(
        target=ws.run_forever, daemon=True,
        kwargs={"ping_interval": 20, "ping_timeout": 10},
    )
    ws_thread.start()

    # Wait for on_open to finish sending config/context
    if not ws_ready.wait(timeout=15):
        print("❌  Connection timeout")
        return False
    if ws_error_flag.is_set():
        print("❌  Connection failed")
        return False

    # ───────────────────────────────────────────────────────────────
    #  MICROPHONE CAPTURE
    #
    #  We use sounddevice.InputStream with a callback.  Every time
    #  the OS delivers a block of audio (FRAMES_PER_CHUNK = 100 ms),
    #  the callback wraps it in a Speech Protocol binary frame and
    #  sends it over the WebSocket.
    #
    #  The FIRST chunk must include a 44-byte WAV header so the
    #  service knows the audio format (sample rate, bit depth, etc.).
    #  All subsequent chunks are raw PCM data.
    # ───────────────────────────────────────────────────────────────

    def audio_callback(indata, frames, time_info, status):
        """sounddevice callback — fires every 100 ms with a new audio chunk.

        Parameters
        ----------
        indata : numpy.ndarray
            Raw audio samples from the microphone (int16).
        frames : int
            Number of frames in this chunk (should be FRAMES_PER_CHUNK).
        time_info : dict
            Timing information (unused).
        status : sounddevice.CallbackFlags
            Error flags (unused).
        """
        if stop_event.is_set() or ws_error_flag.is_set():
            return

        pcm = indata.tobytes()
        try:
            if not audio_started[0]:
                # First chunk: prepend the 44-byte WAV header so the
                # Azure service knows our audio format.
                ws.send(
                    _audio_msg(rid, _wav_header() + pcm),
                    opcode=websocket.ABNF.OPCODE_BINARY,
                )
                audio_started[0] = True
            else:
                # Subsequent chunks: raw PCM only
                ws.send(
                    _audio_msg(rid, pcm),
                    opcode=websocket.ABNF.OPCODE_BINARY,
                )
        except Exception as e:
            if not stop_event.is_set():
                print(f"\n❌  Audio send error: {e}")

    print("🎤  Microphone open — speak in Spanish or English (Ctrl+C to stop)\n")

    try:
        # Open the microphone as a context manager — it starts
        # capturing immediately and calls audio_callback continuously.
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAMES_PER_CHUNK,
            callback=audio_callback,
        ):
            # Keep the main thread alive while audio streams.
            # The actual work happens in the audio callback (sending)
            # and in the WebSocket thread (receiving).
            while not stop_event.is_set() and not ws_error_flag.is_set():
                time.sleep(0.1)

    except KeyboardInterrupt:
        # Propagate Ctrl+C to the outer reconnection loop
        stop_event.set()
        raise
    except sd.PortAudioError as e:
        print(f"\n❌  No microphone available: {e}")
        stop_event.set()   # fatal — no point reconnecting

    finally:
        # ── Graceful session shutdown ──
        try:
            # Send a zero-length audio frame as an end-of-stream marker.
            ws.send(_audio_msg(rid, b""), opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception:
            pass
        ws.close()

    return got_result[0]


# ── Entry point ──
if __name__ == "__main__":
    main()
