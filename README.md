# voice-translator-and-personal-voice

Integrate the Azure Speech service to translate voice with synthesis using Personal Voice.

## Prerequisites

- Python 3.10+
- An Azure Speech resource (or Azure AI Services resource)

## Installation

```bash
pip install -r requirements.txt
```

## Authentication

The application supports **two authentication methods** (dual mode). Configure your `.env` file using `.env-template` as a reference.

### Option A: API Key (traditional)

Set `SPEECH_KEY` and `SPEECH_REGION` in your `.env`:

```dotenv
SPEECH_KEY=your-speech-service-apikey
SPEECH_REGION=westeurope
```

### Option B: Azure Entra ID (recommended for corporate environments)

When `SPEECH_KEY` is **not set** (or commented out), the app automatically switches to Azure AD token authentication using `DefaultAzureCredential` from the `azure-identity` package.

**Requirements:**

1. Your Azure AI Services / Speech resource must have a **custom subdomain** (e.g. `https://myresource.cognitiveservices.azure.com`). Regional endpoints (`https://westeurope.api.cognitive.microsoft.com`) do **not** support Entra ID.
2. Your user or service principal must have the **"Cognitive Services Speech User"** role assigned on the resource (via Azure Portal > Resource > Access control (IAM) > Add role assignment).
3. You must be logged in: `az login`

```dotenv
# SPEECH_KEY commented out → Entra ID is used
#SPEECH_KEY=...
SPEECH_REGION=westeurope
SPEECH_ENDPOINT=https://myresource.cognitiveservices.azure.com
```

| Variable | API Key mode | Entra ID mode |
|---|---|---|
| `SPEECH_KEY` | ✅ Required | ❌ Not set |
| `SPEECH_REGION` | ✅ Required | ✅ Required |
| `SPEECH_ENDPOINT` | Optional | ✅ Required (custom subdomain) |

## Personal Voice Setup

Create your Personal Voice with `create_personal_voice.py`, configuring these values in `.env`:

- `PROJECT_ID`: a string to identify the project
- `CONSENT_ID`: a string to identify the consent
- `PERSONAL_VOICE_ID`: the id of the personal voice to create
- `CONSENT_FILE_PATH`: the path to the WAV file with the consent sentence
- `VOICE_TALENT_NAME`: the name of the talent that recorded the consent
- `COMPANY_NAME`: your company name

Annotate the `SPEAKER_PROFILE_ID` provided when the Personal Voice is created, as it is needed for synthesis.

## Running the Demos

Set the required target languages in the constant `LANGUAGES`.

| Demo | Command |
|---|---|
| **Translate one sentence** (text interface) | `python voice_translator_and_personal_voice.py` |
| **Translate one sentence** (web interface) | `streamlit run voice_translator_and_personal_voice_app.py` |
| **Continuous translation** (web interface) | `streamlit run voice_translator_and_personal_voice_continuous_app.py` |
| **Simultaneous translator** (Spanish ↔ selected languages) | `streamlit run simultaneous_translator_app.py` |
| **Simultaneous translator multi-language** (Spanish ↔ all languages) | `streamlit run "simultaneous_translator_app (MULTI-IDIOMA).py"` |

<img src="./Demo.gif" alt="Video Demo"/>
