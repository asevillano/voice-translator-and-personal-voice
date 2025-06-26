"""  
Speech-to-Speech Translator   –   Streamlit UI  
----------------------------------------------  
Requirements:  
    pip install streamlit azure-cognitiveservices-speech python-dotenv  
  
Variables de entorno:  
    SPEECH_KEY          => clave del recurso Speech  
    SPEECH_REGION       => región del recurso Speech  
    SPEAKER_PROFILE_ID  => id del perfil de voz neuronal (opcional)  
"""  
  
from __future__ import annotations  
  
import os  
import json  
import time  
from pathlib import Path  
  
import streamlit as st  
from dotenv import load_dotenv  
import azure.cognitiveservices.speech as speech_sdk  
  
  
# ───────────────────────────────────────────────────────────────────────────────  
# Target anguages
# key    -> ISO code of the target language for translation
# locale -> locale code used as a candidate for automatic detection  
# name   -> display name  
# ───────────────────────────────────────────────────────────────────────────────  
LANGUAGES: dict[str, tuple[str, str]] = {  
    "bg": ("bg-BG", "Bulgarian"),  
    "hr": ("hr-HR", "Croatian"),  
    "cs": ("cs-CZ", "Czech"),  
    "da": ("da-DK", "Danish"),  
    "nl": ("nl-NL", "Dutch"),  
    "en": ("en-US", "English"),  
    "et": ("et-EE", "Estonian"),  
    "fi": ("fi-FI", "Finnish"),  
    "fr": ("fr-FR", "French"),  
    "de": ("de-DE", "German"),  
    "el": ("el-GR", "Greek"),  
    "hu": ("hu-HU", "Hungarian"),  
    "ga": ("ga-IE", "Irish"),  
    "it": ("it-IT", "Italian"),  
    "lv": ("lv-LV", "Latvian"),  
    "lt": ("lt-LT", "Lithuanian"),  
    "mt": ("mt-MT", "Maltese"),  
    "pl": ("pl-PL", "Polish"),  
    "pt": ("pt-PT", "Portuguese"),  
    "ro": ("ro-RO", "Romanian"),  
    "sk": ("sk-SK", "Slovak"),  
    "sl": ("sl-SI", "Slovenian"),  
    "es": ("es-ES", "Spanish"),  
    "sv": ("sv-SE", "Swedish"),  
}  
  
# Source Language. None = automatic detection 
ORIGIN_LANGUAGE: str | None = None     # e.g., "en-US" to force English
AUTO_DETECT_LOCALES: list[str] = ["en-US", "es-ES", "fr-FR", "it-IT"]
  
# ═══════════════════════════════════════════════════════════════════════════════  
#  Utils  
# ═══════════════════════════════════════════════════════════════════════════════  
@st.cache_resource(show_spinner=False)  
def get_recognizer() -> speech_sdk.translation.TranslationRecognizer:  
    """  
    Returns a recognizer prepared for translation. 
    It is cached between Streamlit reloads with @st.cache_resource.
    """  
    load_dotenv(override=True)  
  
    speech_key   = os.getenv("SPEECH_KEY")  
    speech_region = os.getenv("SPEECH_REGION")  
  
    if not speech_key or not speech_region:  
        st.error("You must specify SPEECH_KEY and SPEECH_REGION in environment variables or in a .env file.")  
        st.stop()  
  
    # Translation configuration
    translation_config = speech_sdk.translation.SpeechTranslationConfig(  
        subscription=speech_key,  
        region=speech_region,  
    )  
  
    # Recognition language (automatic or fixed)
    if ORIGIN_LANGUAGE is not None:  
        translation_config.speech_recognition_language = ORIGIN_LANGUAGE  
  
    # Target languages
    for lang_code in LANGUAGES.keys():  
        translation_config.add_target_language(lang_code)  
  
    # Default microphone  
    audio_config = speech_sdk.AudioConfig(use_default_microphone=True)  
  
    # Recognizer configuration  
    if ORIGIN_LANGUAGE is None:  
        auto_cfg = speech_sdk.languageconfig.AutoDetectSourceLanguageConfig(  
            languages=AUTO_DETECT_LOCALES  
        )  
        recognizer = speech_sdk.translation.TranslationRecognizer(  
            translation_config=translation_config,  
            auto_detect_source_language_config=auto_cfg,  
            audio_config=audio_config,  
        )  
    else:  
        recognizer = speech_sdk.translation.TranslationRecognizer(  
            translation_config=translation_config,  
            audio_config=audio_config,  
        )  
  
    return recognizer  
  
  
@st.cache_resource(show_spinner=False)  
def get_speech_config() -> speech_sdk.SpeechConfig:  
    # SpeechConfig for synthesis (also cached between reloads)
    load_dotenv(override=True)  
    speech_key   = os.getenv("SPEECH_KEY")  
    speech_region = os.getenv("SPEECH_REGION")  
    return speech_sdk.SpeechConfig(subscription=speech_key, region=speech_region)  
  
  
def translate_once(recognizer: speech_sdk.translation.TranslationRecognizer,  
                   target_lang: str) -> tuple[str, str, str]:  
    """  
    Captures voice from the microphone, detects the language, transcribes, and translates. 
    Returns:  
        detected_lang (e.g. 'en-US'),  
        result of recognition,  
        translation into the target language  
    """  
    result = recognizer.recognize_once_async().get()  
  
    if result.reason == speech_sdk.ResultReason.TranslatedSpeech:  
        detected_lang = (  
            speech_sdk.AutoDetectSourceLanguageResult(result).language  
            if ORIGIN_LANGUAGE is None else ORIGIN_LANGUAGE  
        )  
        translated_text = result.translations.get(target_lang, "")  
        return detected_lang, result, translated_text  
  
    elif result.reason == speech_sdk.ResultReason.NoMatch:  
        raise RuntimeError("No voice detected.")  
    else:  
        raise RuntimeError(result.cancellation_details.error_details)  
  
  
def synthesize(text: str, target_lang: str) -> None:  
    """  
    Synthesizes the text using Personal Voice (if available) and plays it through the default speaker.
    Additionally, it stores the audio in memory and sends it to the browser.  
    """  
    if not text:  
        st.warning("Empty text: nothing to synthesize.")  
        return  
  
    speech_config = get_speech_config()  
    speaker_profile_id = os.getenv("SPEAKER_PROFILE_ID") or ""  
  
    locale = LANGUAGES[target_lang][0]  
  
    ssml = f"""  
    <speak version='1.0' xml:lang='en-US'  
           xmlns='http://www.w3.org/2001/10/synthesis'  
           xmlns:mstts='http://www.w3.org/2001/mstts'>  
        <voice name='DragonLatestNeural'>  
            <mstts:ttsembedding speakerProfileId='{speaker_profile_id}'/>  
            <mstts:express-as style='Prompt'>  
                <lang xml:lang='{locale}'> {text} </lang>  
            </mstts:express-as>  
        </voice>  
    </speak>  
    """  
  
    # Configuration to obtain audio in memory in addition to playback through the speaker.
    audio_config = speech_sdk.audio.AudioOutputConfig(use_default_speaker=True)  
    synthesizer = speech_sdk.SpeechSynthesizer(  
        speech_config=speech_config,  
        audio_config=audio_config  
    )  
  
    result = synthesizer.speak_ssml_async(ssml).get()  
  
    if result.reason != speech_sdk.ResultReason.SynthesizingAudioCompleted:  
        raise RuntimeError("Error during synthesis: " + str(result.reason))  
  
    # Mostramos un reproductor en Streamlit  
    audio_bytes = result.audio_data  
    st.audio(audio_bytes, format="audio/wav")  
  
  
# ═══════════════════════════════════════════════════════════════════════════════  
#  Streamlit  Interface
# ═══════════════════════════════════════════════════════════════════════════════  
st.set_page_config("Speech-to-Speech Translator", "🗣️", layout="wide")  

st.markdown(  
    """  
    <style>  
    /* Applies to every st.text_area in the application */  
    div[data-testid="stTextArea"] textarea {  
        background-color: #ffffff !important;   /* white background */  
        color:            #000000 !important;   /* black text       */  
        font-family:      monospace;            /* optional         */
        line-height:      1.35;  
    }  
    </style>  
    """,  
    unsafe_allow_html=True  
)

st.title("🗣️ Speech-to-Speech Translator")  
st.caption("Azure Speech Translation  •  Automatic language detection  •  Personal Voice")
  
st.write(  
    "Click **Start** and speak into your microphone. "  
    "The system will transcribe, translate, and play back your sentence in the target language, and translate to every language in the list."  
)  
  
# Target language selection for translation and speech synthesis
codes_names = [f"{code} - {name}" for code, (_, name) in LANGUAGES.items()]  
selection = st.selectbox("Target language", codes_names, index=codes_names.index("en - English"))  
target_code = selection.split(" - ")[0]  
  
# Main button  
if st.button("🎙️ Start recording"):  
    recognizer = get_recognizer()  
  
    with st.spinner("Listening..."):  
        try:  
            start_time = time.time()  
            det_lang, result, translated = translate_once(recognizer, target_code)  
            elapsed = time.time() - start_time  
        except Exception as ex:  
            st.error(f"❌ {ex}")  
            st.stop()  
  
    # Results
    col1, col2 = st.columns([2, 3], gap="large")
    with col1:  
        st.subheader("🔎 Recognized text")  
        st.text_area(
            label="Recognized text",  
            value=result.text or "—",
            height=180,
            disabled=True, 
            label_visibility="collapsed",
        ) 

        if ORIGIN_LANGUAGE is None:
            json_result = json.loads(result.json) 
            confidence = json_result.get("SpeechPhrase", {}).get("PrimaryLanguage", {}).get("Confidence", None)
            st.write(f"Detected language: **{det_lang}** with confidence **{confidence}**")  
  
    with col2:  
        st.subheader(f"💬 Translations")
        translations = f"{LANGUAGES[target_code][1]}: {translated}"
        for lang in result.translations:
            translations = translations + f'\n- {LANGUAGES.get(lang, ("", ""))[1]}: \t{result.translations[lang]}' 
        st.code(f"{translations}" or "—", language="text")  

    #st.write(f"⏱️ Tiempo de proceso: {elapsed:.2f} s")  
  
    # Synthesis  
    with st.spinner("Synthesizing…"):  
        try:  
            synthesize(translated, target_code)  
        except Exception as ex:  
            st.error(f"❌ Synthesis error: {ex}")  
  
st.markdown("---")  
st.caption("© 2025 - Demo developed with Streamlit and Azure Speech Service")  