# How to Use Azure Speech Translation via WebSocket (No SDK)

This guide explains how to perform **real-time speech translation** using Azure's WebSocket-based Speech Translation API **without the Azure Speech SDK**. All you need is a WebSocket client library (e.g., `websocket-client` for Python) and standard HTTP requests.

> **Why no SDK?** Some corporate environments block API keys and require Entra ID authentication. The SDK handles this internally, but if you need full control over the wire protocol — or want to run in a minimal environment — the WebSocket approach gives you that.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Overview](#2-architecture-overview)
3. [Authentication](#3-authentication)
4. [WebSocket Endpoint](#4-websocket-endpoint)
5. [Speech Protocol Messages](#5-speech-protocol-messages)
6. [Option A: Fixed Source Language](#6-option-a-fixed-source-language)
7. [Option B: Auto-Detect Source Language](#7-option-b-auto-detect-source-language)
8. [Sending Audio](#8-sending-audio)
9. [Handling Server Responses](#9-handling-server-responses)
10. [TTS for Translated Text](#10-tts-for-translated-text)
11. [Complete Python Example](#11-complete-python-example)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

| Requirement | Details |
|---|---|
| **Azure Resource** | An Azure AI Services (multi-service) or Speech-only resource |
| **Region** | e.g., `westeurope`, `eastus` |
| **Auth** | API Key **or** Entra ID (Microsoft Entra / Azure AD) |
| **Python packages** | `websocket-client`, `requests`, `sounddevice`, `numpy` |

```bash
pip install websocket-client requests sounddevice numpy python-dotenv
# For Entra ID auth:
pip install azure-identity
```

### Environment variables (`.env`)

The application uses a `.env` file to store credentials and configuration. The auth mode is determined automatically: if `SPEECH_KEY` is set (uncommented), API Key auth is used; if `SPEECH_KEY` is absent or commented out, Entra ID auth is used.

#### Option 1: API Key Authentication

This is the simplest setup. You only need two variables:

```dotenv
# ── API Key Auth ──
SPEECH_KEY=your-api-key-here
SPEECH_REGION=westeurope
```

| Variable | Required | Description | How to get it |
|---|---|---|---|
| `SPEECH_KEY` | ✅ | The API key for your Azure AI Services or Speech resource. Either Key 1 or Key 2 works. | Azure Portal → your resource → **Keys and Endpoint** → Key 1 |
| `SPEECH_REGION` | ✅ | The Azure region where your resource is deployed (e.g., `westeurope`, `eastus`, `southeastasia`). | Azure Portal → your resource → **Keys and Endpoint** → Location/Region |

#### Option 2: Entra ID (Microsoft Entra) Authentication

Use this when API keys are blocked by corporate policy or you need identity-based access control. Comment out (or remove) `SPEECH_KEY` and set three additional variables:

```dotenv
# ── Entra ID Auth ── (SPEECH_KEY must be commented out or absent)
#SPEECH_KEY=
SPEECH_REGION=westeurope
SPEECH_ENDPOINT=https://your-resource.cognitiveservices.azure.com
SPEECH_RESOURCE_ID=/subscriptions/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/resourceGroups/my-rg/providers/Microsoft.CognitiveServices/accounts/my-resource
```

| Variable | Required | Description | How to get it |
|---|---|---|---|
| `SPEECH_KEY` | ❌ Must be **absent or commented out** | When not set, the app switches to Entra ID auth automatically. | — |
| `SPEECH_REGION` | ✅ | The Azure region (same as API Key mode). | Azure Portal → your resource → **Keys and Endpoint** → Location/Region |
| `SPEECH_ENDPOINT` | ✅ | The **custom subdomain** endpoint of your resource. Must start with `https://` and use the format `https://{resource-name}.cognitiveservices.azure.com`. | Azure Portal → your resource → **Keys and Endpoint** → Endpoint |
| `SPEECH_RESOURCE_ID` | ✅ | The full Azure Resource Manager ID. Used to build the compound auth token for WebSocket connections (`aad#RESOURCE_ID#TOKEN`). | Run: `az cognitiveservices account show --name <resource-name> --resource-group <rg-name> --query id -o tsv` |

#### Entra ID prerequisites

1. **Custom domain must be enabled** on your Azure AI Services / Speech resource. Resources created via the portal typically have this by default (`https://{name}.cognitiveservices.azure.com`).

2. **Role assignment:** The identity (user, service principal, or managed identity) running the app must have the **Cognitive Services Speech User** role (or **Cognitive Services User** for multi-service resources) on the resource:

   ```bash
   az role assignment create \
     --assignee <user-or-sp-object-id> \
     --role "Cognitive Services Speech User" \
     --scope <SPEECH_RESOURCE_ID>
   ```

3. **`azure-identity` package** must be installed:

   ```bash
   pip install azure-identity
   ```

   The app uses `DefaultAzureCredential`, which automatically tries (in order): environment variables, managed identity, Azure CLI, VS Code, etc.

#### Complete `.env` example (Entra ID mode)

```dotenv
# Authentication: comment out SPEECH_KEY to use Entra ID
SPEECH_REGION=<your-speech-service-region>
SPEECH_ENDPOINT=<https://<your-ai-service>.cognitiveservices.azure.com>
SPEECH_RESOURCE_ID=/subscriptions/21c56cef-700f-45c4-85e6-d1adc1d1983d/resourceGroups/self-training/providers/Microsoft.CognitiveServices/accounts/aiservicesasc

# Optional: Personal Voice (for TTS with cloned voice)
SPEAKER_PROFILE_ID=<personal_voice_id>
PROJECT_ID=<personal-voice-project>
CONSENT_ID=<personal-voice-consent-id>
PERSONAL_VOICE_ID=<personal-voice-id>
CONSENT_FILE_PATH=consentimiento.wav
VOICE_TALENT_NAME=<name-of-talent>
COMPANY_NAME=<your-company>
```

#### How each variable is used internally

```
┌─────────────────────┐
│      .env file       │
├─────────────────────┤
│ SPEECH_KEY          │──► API Key auth header: Ocp-Apim-Subscription-Key
│ SPEECH_REGION       │──► WebSocket host: {region}.stt.speech.microsoft.com
│                     │──► TTS host:       {region}.tts.speech.microsoft.com
│ SPEECH_ENDPOINT     │──► issueToken:     {endpoint}/sts/v1.0/issueToken
│ SPEECH_RESOURCE_ID  │──► WS compound:    aad#{resource_id}#{entra_token}
└─────────────────────┘
```

---

## 2. Architecture Overview

```
┌───────────┐     WebSocket (wss://)      ┌──────────────────────┐
│ Microphone├────── PCM audio ────────────►│ Azure Speech Service │
│           │◄───── translation.response ──┤  (Universal V2)      │
└───────────┘                              └──────────────────────┘
                                                    │
                                           ┌────────▼──────────┐
                                           │  Translated Text   │
                                           └────────┬──────────┘
                                                    │
                                           ┌────────▼──────────┐
                                           │  TTS REST API      │
                                           │  (regional endpoint)│
                                           └────────┬──────────┘
                                                    │
                                           ┌────────▼──────────┐
                                           │  Speaker output    │
                                           └───────────────────┘
```

The flow:
1. **Open WebSocket** → send `speech.config` + `speech.context`
2. **Stream audio** → 16 kHz, 16-bit, mono PCM in binary frames
3. **Receive** → `translation.response` messages with translated text
4. **Synthesize** → POST translated text to TTS REST API

---

## 3. Authentication

### Option A: API Key

```python
ws_headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
tts_headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
```

### Option B: Entra ID (Microsoft Entra)

For the **WebSocket** (regional endpoint), use the compound token format:

```python
from azure.identity import DefaultAzureCredential

credential = DefaultAzureCredential()
entra_token = credential.get_token("https://cognitiveservices.azure.com/.default").token

# Compound token for regional WebSocket
compound = f"aad#{SPEECH_RESOURCE_ID}#{entra_token}"
ws_headers = {"Authorization": f"Bearer {compound}"}
```

For **TTS REST** (regional endpoint), exchange the Entra token for a short-lived speech token:

```python
resp = requests.post(
    f"{SPEECH_ENDPOINT}/sts/v1.0/issueToken",
    headers={"Authorization": f"Bearer {entra_token}", "Content-Length": "0"},
)
speech_token = resp.text  # Valid for 10 minutes

tts_headers = {"Authorization": f"Bearer {speech_token}"}
```

> **Important:** The TTS REST endpoint must be the **regional** host (`{region}.tts.speech.microsoft.com`), not the custom subdomain. The custom subdomain returns 404 for TTS paths.

---

## 4. WebSocket Endpoint

```
wss://{region}.stt.speech.microsoft.com/stt/speech/universal/v2?to={targets}&scenario=conversation
```

| Parameter | Description | Example |
|---|---|---|
| `to` | Comma-separated target language codes (ISO 639-1) | `es,fr,de,ja` |
| `scenario` | Recognition scenario | `conversation` |
| `from` | **(Optional)** Fixed source language BCP-47 locale | `en-US` |

- **Fixed language:** include `from=en-US` in the query string
- **Auto-detect:** omit `from` and specify a `languageId` block in `speech.context`

### URL Path

The path **must** be `/stt/speech/universal/v2` (with the `/stt/` prefix). Using `/speech/universal/v2` without the prefix will connect but **will not return translations**.

> **Discovery note:** This was found by capturing the SDK's diagnostic log. The C++ SDK uses host `{region}.stt.speech.microsoft.com` with path `/stt/speech/universal/v2`.

---

## 5. Speech Protocol Messages

All messages follow the Azure Speech Protocol framing:

### Text messages (WebSocket text frames)

```
Path: {path}\r\n
X-RequestId: {request_id}\r\n
X-Timestamp: {iso8601_utc}\r\n
Content-Type: application/json; charset=utf-8\r\n
\r\n
{json_body}
```

### Binary messages (WebSocket binary frames)

```
[uint16-BE: header_length][header_bytes][pcm_audio_bytes]
```

Header (NOT terminated by `\r\n\r\n`):

```
Path: audio\r\n
X-RequestId: {request_id}\r\n
X-Timestamp: {iso8601_utc}\r\n
Content-Type: audio/x-wav\r\n
```

### Python helpers

```python
import json, struct, uuid
from datetime import datetime, timezone

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def text_msg(path, request_id, body):
    b = json.dumps(body) if isinstance(body, dict) else body
    return (
        f"Path: {path}\r\n"
        f"X-RequestId: {request_id}\r\n"
        f"X-Timestamp: {_ts()}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"\r\n"
        f"{b}"
    )

def audio_msg(request_id, pcm_bytes):
    hdr = (
        f"Path: audio\r\n"
        f"X-RequestId: {request_id}\r\n"
        f"X-Timestamp: {_ts()}\r\n"
        f"Content-Type: audio/x-wav\r\n"
    ).encode("utf-8")
    return struct.pack(">H", len(hdr)) + hdr + pcm_bytes
```

---

## 6. Option A: Fixed Source Language

When you **know** the source language in advance, include `from=` in the URL and omit `languageId` from `speech.context`.

### WebSocket URL

```
wss://westeurope.stt.speech.microsoft.com/stt/speech/universal/v2
    ?from=en-US
    &to=es,fr,de
    &scenario=conversation
```

### speech.config

```json
{
  "context": {
    "system": { "version": "1.47.0", "name": "SpeechSDK", "build": "Python-WebSocket" },
    "os":     { "name": "Windows", "version": "10", "platform": "Windows" },
    "audio":  { "source": { "type": "Microphones", "samplerate": "16000",
                             "bitspersample": "16", "channelcount": "1" } }
  }
}
```

### speech.context (no languageId)

```json
{
  "phraseDetection": {
    "mode": "CONVERSATION",
    "onSuccess": { "action": "Translate" },
    "onInterim": { "action": "Translate" }
  },
  "translation": {
    "targetLanguages": ["es", "fr", "de"],
    "output": {
      "includePassThroughResults": true,
      "interimResults": { "mode": "Always" }
    },
    "onSuccess":     { "action": "None" },
    "onPassthrough": { "action": "None" }
  },
  "phraseOutput": {
    "interimResults": { "resultType": "None" },
    "phraseResults":  { "resultType": "None" }
  },
  "audio": { "streams": { "1": null } }
}
```

### Key points for fixed language mode

- `from=en-US` in the URL tells the server which language to expect.
- No `languageId` block is needed in `speech.context`.
- `phraseOutput` can use `"None"/"None"` (translations arrive via `translation.response`, not `speech.phrase`).

---

## 7. Option B: Auto-Detect Source Language

When you want the service to **automatically detect** which language is being spoken from a closed list of candidates, omit `from` from the URL and add a `languageId` block to `speech.context`.

### WebSocket URL (no `from`)

```
wss://westeurope.stt.speech.microsoft.com/stt/speech/universal/v2
    ?to=es,fr,de,en,ja
    &scenario=conversation
```

### speech.context (with languageId)

```json
{
  "phraseDetection": {
    "mode": "CONVERSATION",
    "onSuccess": { "action": "Translate" },
    "onInterim": { "action": "Translate" }
  },
  "translation": {
    "targetLanguages": ["es", "fr", "de", "en", "ja"],
    "output": {
      "includePassThroughResults": true,
      "interimResults": { "mode": "Always" }
    },
    "onSuccess":     { "action": "None" },
    "onPassthrough": { "action": "None" }
  },
  "languageId": {
    "languages": ["en-US", "es-ES", "fr-FR", "de-DE", "ja-JP"],
    "onSuccess": { "action": "Recognize" },
    "onUnknown": { "action": "None" },
    "mode": "DetectContinuous",
    "Priority": "PrioritizeLatency"
  },
  "phraseOutput": {
    "interimResults": { "resultType": "None" },
    "phraseResults":  { "resultType": "None" }
  },
  "audio": { "streams": { "1": null } }
}
```

### `languageId` fields explained

| Field | Value | Description |
|---|---|---|
| `languages` | `["en-US", "es-ES", ...]` | **BCP-47 locales** (closed list of candidates). The server will only detect from this list. Max ~10 languages recommended. |
| `mode` | `"DetectContinuous"` | Detect language changes throughout the conversation. Use `"DetectAtAudioStart"` to detect only at the beginning. |
| `Priority` | `"PrioritizeLatency"` | Optimize for low latency (vs. accuracy). Note: capital `P` — matches the SDK wire format. |
| `onSuccess.action` | `"Recognize"` | After detecting the language, proceed with recognition (and translation). |
| `onUnknown.action` | `"None"` | If the language can't be determined, do nothing (don't reject the audio). |

### Key difference: `phraseOutput` is `None/None`

When `languageId` is present, the SDK sets `phraseOutput` to `None/None`:

```json
"phraseOutput": {
    "interimResults": { "resultType": "None" },
    "phraseResults":  { "resultType": "None" }
}
```

This tells the server that phrase results should come through the **translation pipeline** (`translation.response`) rather than the plain recognition pipeline (`speech.phrase`).

---

## 8. Sending Audio

### Audio format

- **Sample rate:** 16,000 Hz
- **Bit depth:** 16-bit signed integer (PCM)
- **Channels:** 1 (mono)

### First chunk: WAV header

The very first binary message must include a 44-byte RIFF/WAV header prepended to the PCM data:

```python
import struct

def wav_header():
    """44-byte RIFF/WAV header for streaming 16 kHz / 16-bit / mono PCM."""
    sample_rate = 16000
    channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0, b"WAVE",               # RIFF header (size=0 for streaming)
        b"fmt ", 16, 1, channels,           # fmt chunk
        sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", 0,                         # data chunk (size=0 for streaming)
    )
```

### Streaming loop

```python
import sounddevice as sd
import websocket

first_chunk = True

def audio_callback(indata, frames, time_info, status):
    global first_chunk
    pcm = indata.tobytes()

    if first_chunk:
        # Prepend WAV header to first chunk
        msg = audio_msg(request_id, wav_header() + pcm)
        first_chunk = False
    else:
        msg = audio_msg(request_id, pcm)

    ws.send(msg, opcode=websocket.ABNF.OPCODE_BINARY)

# Open microphone stream
stream = sd.InputStream(
    samplerate=16000,
    channels=1,
    dtype="int16",
    blocksize=1600,          # 100 ms chunks
    callback=audio_callback,
)
stream.start()
```

### End of audio

Send an empty audio message to signal end-of-stream:

```python
ws.send(audio_msg(request_id, b""), opcode=websocket.ABNF.OPCODE_BINARY)
```

---

## 9. Handling Server Responses

### Message paths

| Path | Type | Description |
|---|---|---|
| `turn.start` | Control | Server ready to receive audio |
| `speech.startDetected` | Control | Speech onset detected |
| `speech.hypothesis` | Interim | Partial recognition (no translations) |
| `translation.response` | **Main** | Contains recognition + translations |
| `speech.endDetected` | Control | Speech offset detected |
| `turn.end` | Control | Turn complete |

### `translation.response` — the key message

This is where translations arrive. The JSON structure (discovered via SDK diagnostic logging):

```json
{
  "Extensions": ["TranslationSourceRef", "SpeechPhrase"],
  "TranslationSourceRef": {
    "Id": "...",
    "ReferenceType": "SpeechPhrase"
  },
  "SpeechPhrase": {
    "Id": "7841f1cc...",
    "RecognitionStatus": "Success",
    "DisplayText": "Umbrella.",
    "Offset": 17200000,
    "Duration": 3200000,
    "PrimaryLanguage": {
      "Language": "en-US",
      "Confidence": "Low"
    },
    "Channel": 0
  },
  "TranslationStatus": "Success",
  "Translations": [
    { "Id": "...", "DisplayText": "Paraguas.",    "Language": "es" },
    { "Id": "...", "DisplayText": "Parapluie.",   "Language": "fr" },
    { "Id": "...", "DisplayText": "Regenschirm.", "Language": "de" }
  ]
}
```

### Critical: field locations

| Data | Location | Note |
|---|---|---|
| Recognized text | `SpeechPhrase.DisplayText` | |
| Recognition status | `SpeechPhrase.RecognitionStatus` | `"Success"`, `"NoMatch"`, etc. |
| Detected language | `SpeechPhrase.PrimaryLanguage.Language` | BCP-47 locale |
| Translations | **Root level** `Translations[]` | **NOT** inside `SpeechPhrase` |
| Each translation | `Translations[].DisplayText` | Field is `DisplayText`, not `Text` |
| Translation language | `Translations[].Language` | ISO 639-1 code |
| Translation status | **Root level** `TranslationStatus` | `"Success"` or error |

> ⚠️ **Common pitfall:** The `Translations` array is at the **root level** of the response, **not** nested inside `SpeechPhrase`. This is a frequent source of "translation missing" bugs.

### Hypothesis messages (interim / partial)

For `translation.response` messages that contain `SpeechHypothesis` instead of `SpeechPhrase`, the same root-level `Translations` structure applies, but these are partial results that will be superseded by the final phrase.

### Python handler

```python
def on_message(ws, message):
    if isinstance(message, bytes):
        return  # Binary = server-synthesized audio (if voice feature enabled)

    path, body = parse_msg(message)

    if path == "translation.response":
        data = json.loads(body)
        phrase = data.get("SpeechPhrase", {})

        if phrase.get("RecognitionStatus") != "Success":
            return

        source_text = phrase.get("DisplayText", "")
        source_lang = phrase.get("PrimaryLanguage", {}).get("Language", "")
        translations = data.get("Translations", [])  # ROOT level!

        print(f"Recognized ({source_lang}): {source_text}")
        for t in translations:
            print(f"  → {t['Language']}: {t['DisplayText']}")

    elif path == "speech.hypothesis":
        data = json.loads(body)
        print(f"Partial: {data.get('Text', '')}")
```

---

## 10. TTS for Translated Text

Once you have the translated text, synthesize it via the TTS REST API:

```
POST https://{region}.tts.speech.microsoft.com/cognitiveservices/v1
```

### Headers

```python
headers = {
    **auth_headers,   # Ocp-Apim-Subscription-Key or Authorization: Bearer
    "Content-Type": "application/ssml+xml",
    "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
    "User-Agent": "SpeechTranslator/1.0",
}
```

### SSML body

```xml
<speak version='1.0' xml:lang='es-ES'
       xmlns='http://www.w3.org/2001/10/synthesis'>
  <voice name='es-ES-ElviraNeural'>Paraguas.</voice>
</speak>
```

### Stream to speakers

```python
import sounddevice as sd
import numpy as np

resp = requests.post(tts_url, headers=headers, data=ssml.encode("utf-8"), stream=True)

stream = sd.OutputStream(samplerate=24000, channels=1, dtype="int16", blocksize=2400)
stream.start()

for chunk in resp.iter_content(chunk_size=9600):
    if chunk:
        # Ensure int16 alignment
        usable = len(chunk) - (len(chunk) % 2)
        if usable:
            samples = np.frombuffer(chunk[:usable], dtype=np.int16)
            stream.write(samples.reshape(-1, 1))

stream.stop()
stream.close()
```

> **Entra ID + TTS:** The regional TTS endpoint does not accept Entra ID tokens directly. You must first exchange the Entra token via `POST {custom_endpoint}/sts/v1.0/issueToken` to get a 10-minute speech auth token.

---

## 11. Complete Python Example

### Minimal fixed-language translator

```python
"""Minimal speech translator: English → Spanish via WebSocket."""
import json, uuid, struct, threading
from datetime import datetime, timezone
import websocket
import sounddevice as sd
import numpy as np

REGION = "westeurope"
API_KEY = "your-key"
REQUEST_ID = uuid.uuid4().hex

WS_URL = (
    f"wss://{REGION}.stt.speech.microsoft.com/stt/speech/universal/v2"
    f"?from=en-US&to=es&scenario=conversation"
)

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def text_msg(path, body):
    b = json.dumps(body) if isinstance(body, dict) else body
    return (f"Path: {path}\r\nX-RequestId: {REQUEST_ID}\r\n"
            f"X-Timestamp: {_ts()}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n\r\n{b}")

def audio_msg(pcm):
    hdr = (f"Path: audio\r\nX-RequestId: {REQUEST_ID}\r\n"
           f"X-Timestamp: {_ts()}\r\nContent-Type: audio/x-wav\r\n").encode()
    return struct.pack(">H", len(hdr)) + hdr + pcm

def wav_header():
    return struct.pack("<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0, b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16, b"data", 0)

first_chunk = [True]

def audio_cb(indata, frames, time_info, status):
    pcm = indata.tobytes()
    if first_chunk[0]:
        ws.send(audio_msg(wav_header() + pcm), opcode=websocket.ABNF.OPCODE_BINARY)
        first_chunk[0] = False
    else:
        ws.send(audio_msg(pcm), opcode=websocket.ABNF.OPCODE_BINARY)

def on_open(ws_app):
    ws_app.send(text_msg("speech.config", {
        "context": {"system": {"version": "1.0.0"},
                    "os": {"name": "Windows", "version": "10", "platform": "Windows"}}
    }))
    ws_app.send(text_msg("speech.context", {
        "phraseDetection": {"mode": "CONVERSATION",
                            "onSuccess": {"action": "Translate"},
                            "onInterim": {"action": "Translate"}},
        "translation": {"targetLanguages": ["es"],
                        "output": {"includePassThroughResults": True,
                                   "interimResults": {"mode": "Always"}},
                        "onSuccess": {"action": "None"},
                        "onPassthrough": {"action": "None"}},
        "phraseOutput": {"interimResults": {"resultType": "None"},
                         "phraseResults": {"resultType": "None"}},
        "audio": {"streams": {"1": None}},
    }))
    # Start microphone
    sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                   blocksize=1600, callback=audio_cb).start()

def on_message(ws_app, message):
    if isinstance(message, bytes):
        return
    hdr, _, body = message.partition("\r\n\r\n")
    path = ""
    for ln in hdr.split("\r\n"):
        if ln.lower().startswith("path:"):
            path = ln.split(":", 1)[1].strip()

    if path == "translation.response":
        data = json.loads(body)
        phrase = data.get("SpeechPhrase", {})
        if phrase.get("RecognitionStatus") == "Success":
            text = phrase.get("DisplayText", "")
            translations = data.get("Translations", [])
            for t in translations:
                print(f"[{t['Language']}] {t['DisplayText']}")

ws = websocket.WebSocketApp(
    WS_URL,
    header={"Ocp-Apim-Subscription-Key": API_KEY,
            "X-ConnectionId": uuid.uuid4().hex},
    on_open=on_open,
    on_message=on_message,
    on_error=lambda ws, e: print(f"Error: {e}"),
)
ws.run_forever(ping_interval=20)
```

---

## 12. Troubleshooting

### Common issues and solutions

| Symptom | Cause | Solution |
|---|---|---|
| **404 on WebSocket connect** | Wrong host or path | Use `{region}.stt.speech.microsoft.com` with path `/stt/speech/universal/v2` |
| **Speech recognized but no translations** | Missing path prefix `/stt/` | URL must be `/stt/speech/universal/v2`, not `/speech/universal/v2` |
| **Speech recognized but `Translations` empty** | Missing `includePassThroughResults` | Add `"includePassThroughResults": true` to `translation.output` |
| **Speech recognized but `Translations` empty** | Missing `onPassthrough` | Add `"onPassthrough": {"action": "None"}` to `translation` |
| **`Could not deserialize speech context`** | Invalid `speech.context` JSON | Match the exact field names and structure shown above |
| **`standalone Lid, no PhraseDetection.Mode other than None`** | `languageId` present without `translation` context | Always include the full `translation` block when using `languageId` |
| **Translation block appears as "MISSING"** | Looking for translations inside `SpeechPhrase` | Translations are at the **root level** of `translation.response`, not inside `SpeechPhrase` |
| **TTS returns 404 with Entra ID** | Using custom subdomain for TTS | Always use the **regional** TTS endpoint (`{region}.tts.speech.microsoft.com`) |
| **TTS returns 401 with Entra ID** | Entra token not accepted by regional endpoint | Exchange via `POST {endpoint}/sts/v1.0/issueToken` first |
| **`phraseDetection.mode: Conversation` errors** | Wrong casing | Use uppercase `"CONVERSATION"` (matches SDK wire format) |

### Debugging technique: SDK diagnostic sniffer

The most reliable way to discover the exact wire format is to run the real SDK with diagnostic logging and inspect the traffic:

```python
import azure.cognitiveservices.speech as speechsdk

# Enable BEFORE creating any SDK objects
speechsdk.diagnostics.logging.FileLogger.start("sdk_traffic.log")

config = speechsdk.translation.SpeechTranslationConfig(
    subscription="your-key", region="westeurope")
config.speech_recognition_language = "en-US"
config.add_target_language("es")

auto_detect = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
    languages=["en-US", "es-ES"])

recognizer = speechsdk.translation.TranslationRecognizer(
    translation_config=config,
    auto_detect_source_language_config=auto_detect)

recognizer.start_continuous_recognition()
input("Press Enter to stop...")
recognizer.stop_continuous_recognition()

speechsdk.diagnostics.logging.FileLogger.stop()
```

Then search the log for:
- `speech.context=` — the exact JSON the SDK sends
- `connectionUrl=` — the exact WebSocket URL
- `translation.response` — the server response format
- `RESULT-Json` — the parsed translation result

---

## Summary: Option A vs Option B

| | **Option A: Fixed Language** | **Option B: Auto-Detect** |
|---|---|---|
| **URL `from` param** | `from=en-US` | *(omitted)* |
| **`languageId` in context** | *(omitted)* | Required — with `languages`, `mode`, `Priority` |
| **`languageId.mode`** | N/A | `DetectAtAudioStart` or `DetectContinuous` |
| **`phraseOutput`** | `None/None` | `None/None` |
| **Detected language** | Always the fixed `from` value | In `SpeechPhrase.PrimaryLanguage.Language` |
| **Latency** | Slightly lower | Slightly higher (language detection step) |
| **Use case** | Known source language | Multilingual conversations |

### Critical fields checklist

Regardless of which option you choose, **always include**:

- [x] `phraseDetection.mode` = `"CONVERSATION"` (uppercase)
- [x] `phraseDetection.onSuccess.action` = `"Translate"`
- [x] `phraseDetection.onInterim.action` = `"Translate"`
- [x] `translation.targetLanguages` = list of target language codes
- [x] `translation.output.includePassThroughResults` = `true`
- [x] `translation.output.interimResults.mode` = `"Always"`
- [x] `translation.onSuccess.action` = `"None"`
- [x] `translation.onPassthrough.action` = `"None"`
- [x] `phraseOutput.interimResults.resultType` = `"None"`
- [x] `phraseOutput.phraseResults.resultType` = `"None"`
- [x] `audio.streams.1` = `null`
