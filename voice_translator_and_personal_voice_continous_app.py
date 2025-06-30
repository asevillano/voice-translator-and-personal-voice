# ------------------------------------------------------------  
# app.py – Streamlit UI for Azure Speech real-time translation  
# Newest items on top • single toggle button • displays detected language  
# ------------------------------------------------------------  
import os  
import time  
import json  
import threading  
from dotenv import load_dotenv  
  
import streamlit as st  
from streamlit_autorefresh import st_autorefresh  
  
# --------------- Streamlit version–independent rerun ----------  
def do_rerun():  
    if hasattr(st, "rerun"):  
        st.rerun()          # Streamlit ≥ 1.29  
    else:  
        st.experimental_rerun()  
  
# ---------------------  Azure Speech SDK  ---------------------  
try:  
    import azure.cognitiveservices.speech as speechsdk  
except ImportError:  
    st.error("Install Azure SDK:  pip install azure-cognitiveservices-speech")  
    st.stop()  
  
# ---------------------  Credentials / endpoint  ---------------  
load_dotenv(override=True)  
speech_key    = os.getenv("SPEECH_KEY")  
speech_region = os.getenv("SPEECH_REGION")  
  
if not (speech_key and speech_region):  
    st.error("Missing SPEECH_KEY and/or SPEECH_REGION environment variables.")  
    st.stop()  
  
speech_endpoint = f"https://{speech_region}.api.cognitive.microsoft.com"  
  
# ------------------------- Languages --------------------------  
LANGUAGES = {  
    "bg": ("bg-BG", "Bulgarian"),   "hr": ("hr-HR", "Croatian"),  
    "cs": ("cs-CZ", "Czech"),       "da": ("da-D",  "Danish"),  
    "nl": ("nl-NL", "Dutch"),       "en": ("en-US", "English"),  
    "et": ("et-EE", "Estonian"),    "fi": ("fi-FI", "Finnish"),  
    "fr": ("fr-FR", "French"),      "de": ("de-DE", "German"),  
    "el": ("el-GR", "Greek"),       "hu": ("hu-HU", "Hungarian"),  
    "ga": ("ga-IE", "Irish"),       "it": ("it-IT", "Italian"),  
    "lv": ("lv-LV", "Latvian"),     "lt": ("lt-LT", "Lithuanian"),  
    "mt": ("mt-MT", "Maltese"),     "pl": ("pl-PL", "Polish"),  
    "pt": ("pt-PT", "Portuguese"),  "ro": ("ro-RO", "Romanian"),  
    "sk": ("sk-S",  "Slovak"),      "sl": ("sl-SI", "Slovenian"),  
    "es": ("es-ES", "Spanish"),     "sv": ("sv-SE", "Swedish"),  
}  
  
ORIGIN_LANGUAGE     = None                       # None → auto-detect  
AUTO_DETECT_LOCALES = ["en-US", "es-ES", "fr-FR", "it-IT"]  
  
# ------------------- recognizer builder -----------------------  
def build_recognizer() -> speechsdk.translation.TranslationRecognizer:  
    # Create a translation configuration
    translation_cfg = speechsdk.translation.SpeechTranslationConfig(  
        subscription=speech_key,  
        endpoint=speech_endpoint,  
    )  
    # Set the tarjet languages
    for code in LANGUAGES:  
        translation_cfg.add_target_language(code)  
  
    # Set the audio from default microphone
    audio_cfg = speechsdk.audio.AudioConfig(use_default_microphone=True)  
  
    if ORIGIN_LANGUAGE is None:
        # Auto-detect source language
        auto_cfg = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(  
            languages=AUTO_DETECT_LOCALES  
        )
        # Set the language detection mode to continuous
        translation_cfg.set_property(property_id=speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode, value='Continuous')
        return speechsdk.translation.TranslationRecognizer(  
            translation_config=translation_cfg,  
            auto_detect_source_language_config=auto_cfg,  
            audio_config=audio_cfg,  
        )  
    else:  
        translation_cfg.speech_recognition_language = ORIGIN_LANGUAGE  
        return speechsdk.translation.TranslationRecognizer(  
            translation_config=translation_cfg,  
            audio_config=audio_cfg,  
        )  
  
# --------------------- background worker ----------------------  
def recognition_worker(stop_event: threading.Event,  
                       transcript_list: list,  
                       translation_list: list,  
                       status_dict: dict,  
                       ui_ping: threading.Event):  
    recognizer = build_recognizer()  
  
    def on_recognized(evt: speechsdk.translation.TranslationRecognitionEventArgs):  
        if evt.result.reason != speechsdk.ResultReason.TranslatedSpeech:  
            return  
  
        # 1) Update lists (latest first)  
        transcript_list.insert(0, evt.result.text)  
        translation_list.insert(0, dict(evt.result.translations))  
  
        # 2) Extract language + confidence  
        lang_res = speechsdk.AutoDetectSourceLanguageResult(evt.result)  
        detected_lang = lang_res.language  
  
        try:  
            json_result   = json.loads(evt.result.json)  
            confidence = json_result.get("SpeechPhrase", {}).get("PrimaryLanguage", {}).get("Confidence", None)
        except Exception:  
            confidence = None  
  
        # store in shared dict  
        status_dict["language"]   = detected_lang  
        status_dict["confidence"] = confidence  
  
        ui_ping.set()                       # ask UI to refresh  
  
    recognizer.recognized.connect(on_recognized)  
    recognizer.start_continuous_recognition()  
  
    while not stop_event.is_set():  
        time.sleep(0.1)  
  
    recognizer.stop_continuous_recognition()  
  
# --------------------------- Streamlit UI ---------------------  
st.set_page_config(page_title="Live Speech Translator", layout="wide")  
st.image("microsoft.png", width=100)
st.title("🎙️ Real-time Speech-to-Text & Translation")  
st.caption("Azure Speech Translation  •  Automatic language detection")

st.write(  
    "Click **Start** and speak into your microphone. "  
    "The system will transcribe and translate your sentences in the target languages."
)

# ---- one-time session state initialisation ----  
if "transcript" not in st.session_state:  
    st.session_state.transcript   = []          # list[str]  
    st.session_state.translation = []           # list[dict]  
    st.session_state.status      = {"language": None, "confidence": None}  
    st.session_state.thread       = None  
    st.session_state.stop_event   = None  
    st.session_state.ui_ping      = threading.Event()  
  
# current status  
is_recording = (  
    st.session_state.thread is not None and  
    st.session_state.thread.is_alive()  
)  
  
# ------------------ single toggle button ---------------------  
button_label = "⏹️  Stop recording" if is_recording else "▶️  Start recording"  
  
if st.button(button_label, type="primary"):  
    if is_recording:  
        # stop  
        st.session_state.stop_event.set()  
    else:  
        # start fresh lists & status  
        st.session_state.transcript.clear()  
        st.session_state.translation.clear()  
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
            ),  
        )  
        st.session_state.thread.start()  
  
    do_rerun()  
  
# ---------------- detected language + confidence --------------  
lang  = st.session_state.status.get("language")  
conf  = st.session_state.status.get("confidence")  
  
if lang:
    if conf == "Unknown":
        st.markdown(f"**Detected language:** {lang}")
    else:
        st.markdown(f"**Detected language:** {lang}  • **Confidence:** {conf}")  
else:  
    st.write("")     # empty spacer so layout stays stable  
  
# -------------- immediate refresh when new text arrives -------  
if st.session_state.ui_ping.is_set():  
    st.session_state.ui_ping.clear()  
    do_rerun()  
  
# fallback 1-second heartbeat  
st_autorefresh(interval=1000, key="heartbeat")  
  
# ---------------- two-column live output ----------------------  
col_l, col_r = st.columns(2)  
  
with col_l:  
    st.subheader("Latest transcription")  
    if st.session_state.transcript:  
        total = len(st.session_state.transcript)  
        for idx, sent in enumerate(st.session_state.transcript):  
            num = total - idx          # newest gets highest number  
            st.markdown(f"**{num}.** {sent}")  
    else:  
        st.info("Press “Start recording” and speak…")  
  
with col_r:  
    st.subheader("Latest translations")  
    if st.session_state.translation:  
        total = len(st.session_state.translation)  
        for idx, tdict in enumerate(st.session_state.translation):  
            num = total - idx  
            st.markdown(f"**Sentence {num}**")  
            for lang_code, txt in tdict.items():  
                st.write(f"{LANGUAGES.get(lang_code, ("", ""))[1]}: {txt}")  
            st.write("---")  
    else:  
        st.info("Translations will appear here.")  
  
# --------------------------- footer ---------------------------  
src_info = "auto-detected" if ORIGIN_LANGUAGE is None else ORIGIN_LANGUAGE  
st.caption(f"Source language: {src_info} • Powered by Azure Speech Service")  