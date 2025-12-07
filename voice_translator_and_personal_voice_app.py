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
from utils import *

# Source Language. None = automatic detection 
AUTO_DETECT_LOCALES: list[str] = ["en-US", "es-ES", "fr-FR", "it-IT"]
source_language = "es-ES"
synthetize_translation = True  # Whether to synthesize the translation or not

# Set LANGUAGES TO EUROPEAN COMMISSION LANGUAGES
LANGUAGES = LANGUAGES_EU_COMMISION

# Voice options for TTS
VOICE_OPTIONS = {
    "Ximena (Spanish Female)": "es-ES-Ximena:DragonHDLatestNeural",
    "Tristan (Spanish Male)": "es-ES-Tristan:DragonHDLatestNeural",
    "Personal Voice": "personal"
}

load_dotenv(override=True)  

speech_key   = os.getenv("SPEECH_KEY")  
speech_region = os.getenv("SPEECH_REGION")  

if not speech_key or not speech_region:  
    st.error("You must specify SPEECH_KEY and SPEECH_REGION in environment variables or in a .env file.")  
    st.stop() 

# ═══════════════════════════════════════════════════════════════════════════════  
#  Cached configs for optimal performance
# ═══════════════════════════════════════════════════════════════════════════════
@st.cache_resource(show_spinner=False)
def get_speech_config() -> speech_sdk.SpeechConfig:
    """SpeechConfig for synthesis (cached between reloads)"""
    return speech_sdk.SpeechConfig(subscription=speech_key, region=speech_region)

@st.cache_resource(show_spinner=False)
def get_audio_input_config() -> speech_sdk.AudioConfig:
    """AudioConfig for microphone (cached for reuse)"""
    return speech_sdk.AudioConfig(use_default_microphone=True)

@st.cache_resource(show_spinner=False)
def get_audio_output_config() -> speech_sdk.audio.AudioOutputConfig:
    """AudioOutputConfig for speaker (cached for reuse)"""
    return speech_sdk.audio.AudioOutputConfig(use_default_speaker=True)

# ═══════════════════════════════════════════════════════════════════════════════  
#  Utils  
# ═══════════════════════════════════════════════════════════════════════════════ 
# Returns a recognizer prepared for translation
def build_recognizer(detect_language: bool, source_language: str) -> speech_sdk.translation.TranslationRecognizer:  
    """
    Builds a recognizer prepared for translation using cached configs.
    Note: Cannot cache recognizer itself as it maintains state.
    """
    # Create new translation config (lightweight operation)
    translation_config = speech_sdk.translation.SpeechTranslationConfig(
        subscription=speech_key,
        region=speech_region,
    )
    
    # Add all target languages
    for lang_code in LANGUAGES.keys():
        translation_config.add_target_language(lang_code)
    
    # Set recognition language (automatic or fixed)
    if not detect_language:
        translation_config.speech_recognition_language = source_language
    
    # Use cached audio config for microphone (this is the expensive part)
    audio_config = get_audio_input_config()
    
    # Recognizer configuration
    if detect_language:  # Automatic language detection
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
  

# ═══════════════════════════════════════════════════════════════════════════════  
#  Translation and Recognition of one phrase
# ═══════════════════════════════════════════════════════════════════════════════  
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
        detected_lang = speech_sdk.AutoDetectSourceLanguageResult(result).language  
        translated_text = result.translations.get(target_lang, "")  
        return detected_lang, result, translated_text  
  
    elif result.reason == speech_sdk.ResultReason.NoMatch:  
        raise RuntimeError("No voice detected.")  
    else:  
        raise RuntimeError(result.cancellation_details.error_details)  
  
# ═══════════════════════════════════════════════════════════════════════════════  
#  Voice Synthesis with Personal Voice
# ═══════════════════════════════════════════════════════════════════════════════
@st.cache_resource(show_spinner=False)
def get_synthesizer() -> speech_sdk.SpeechSynthesizer:
    """Create and cache a reusable synthesizer instance"""
    speech_config = get_speech_config()
    audio_config = get_audio_output_config()
    return speech_sdk.SpeechSynthesizer(
        speech_config=speech_config,
        audio_config=audio_config
    )

def build_ssml(text: str, target_lang: str, voice_choice: str) -> str:
    """Build SSML string based on voice choice"""
    if voice_choice == "personal":
        speaker_profile_id = os.getenv("SPEAKER_PROFILE_ID") or ""
        locale = LANGUAGES[target_lang][0]  # Locale for the target language
        return f"""
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
    else:  # Speech synthesis with a standard voice
        return f"""
        <speak version='1.0' xml:lang='{target_lang}'
            xmlns='http://www.w3.org/2001/10/synthesis'>
            <voice name='{voice_choice}'>
                {text}
            </voice>
        </speak>
        """

def synthesize(text: str, target_lang: str, voice_choice: str) -> None:
    """
    Synthesizes text using the cached synthesizer.
    Plays through the default speaker and displays in Streamlit.
    """
    if not text:
        st.warning("Empty text: nothing to synthesize.")
        return
    
    # Get cached synthesizer (reused across calls)
    synthesizer = get_synthesizer()
    
    # Build SSML
    ssml = build_ssml(text, target_lang, voice_choice)
    
    # Synthesize audio
    result = synthesizer.speak_ssml_async(ssml).get()
    
    if result.reason != speech_sdk.ResultReason.SynthesizingAudioCompleted:
        raise RuntimeError("Error during synthesis: " + str(result.reason))
    
    # Display audio player in Streamlit
    audio_bytes = result.audio_data
    st.audio(audio_bytes, format="audio/wav")  
  
  
# ═══════════════════════════════════════════════════════════════════════════════  
#  Streamlit  Interface
# ═══════════════════════════════════════════════════════════════════════════════  
st.set_page_config("Speech-to-Speech Translator", "🗣️", layout="wide")  

# CSS for sidebar width
st.markdown(
"""
    <style>
        /* Ajusta el ancho de la sidebar */
            [data-testid="stSidebar"] {
                min-width: 270px;
                max-width: 270px;
        }
        [data-testid="stSidebarContent"] {
            padding: 1rem;
        }
    </style>
    """,
    unsafe_allow_html=True
)

st.image("microsoft.png", width=100)
st.title("🗣️ Speech-to-Speech Translator")  
st.caption("Azure Speech Translation  •  Automatic language detection  •  Personal Voice")
  
st.write(  
    "Click **Start** and speak into your microphone. "  
    "The system will transcribe, translate, and play back your sentence in the target language, "
    "and translate it to every language in the list."  
)  

with st.sidebar:
    st.markdown("### Settings")
    
    detect_language = st.checkbox('Detect language', True, help=f"Automatically detect the language between {', '.join([get_language_name(code) for code in AUTO_DETECT_LOCALES])} or select one from the list.")
    if not detect_language:
        source_language = st.selectbox("Source language:", 
                                       [f"{code}" for code in AUTO_DETECT_LOCALES],
                                       index=0)  # Default to the first language (English)

    st.markdown("---")
    st.markdown("### Synthesis Settings")
    synthetize_translation = st.checkbox("Enable TTS synthesis", True, help="Synthesize translations with text-to-speech")
    
    if synthetize_translation:
        # Target language selection for translation and speech synthesis
        codes_names = [f"{code} - {name}" for code, (_, name) in LANGUAGES.items()]  
        selection = st.selectbox("Target language for synthesis", codes_names, index=codes_names.index("en - English"))  
        target_code = selection.split(" - ")[0]
        
        # Voice selection
        voice_display_name = st.selectbox(
            "Select voice",
            list(VOICE_OPTIONS.keys()),
            index=0,  # Default to Ximena
            help="Choose between Spanish female, male, or personal voice"
        )
        selected_voice = VOICE_OPTIONS[voice_display_name]
    else:
        target_code = "en"
        selected_voice = VOICE_OPTIONS["Ximena (Spanish Female)"]

# Main button  
if st.button("🎙️ Start recording"):  
    # Build recognizer with current settings (uses cached audio config)
    recognizer = build_recognizer(detect_language, source_language)
  
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
        st.markdown(f"{result.text}" or "—")

        if detect_language:
            json_result = json.loads(result.json) 
            confidence = json_result.get("SpeechPhrase", {}).get("PrimaryLanguage", {}).get("Confidence", None)
            st.write(f"**Detected language:** {get_language_name(det_lang)}  • **Confidence:** {confidence}")
  
    with col2:  
        st.subheader(f"💬 Translations")
        translations = f"{LANGUAGES[target_code][1]}: {translated}"
        for lang in result.translations:
            translations = translations + f'\n- {LANGUAGES.get(lang, ("", ""))[1]}: {result.translations[lang]}' 
        st.write(f"{translations}" or "—")

    #st.write(f"⏱️ Tiempo de proceso: {elapsed:.2f} s")  
  
    if synthetize_translation:
        # Synthesis  
        with st.spinner("Synthesizing…"):  
            try:  
                synthesize(translated, target_code, selected_voice)  
            except Exception as ex:  
                st.error(f"❌ Synthesis error: {ex}")  
  
st.markdown("---")  
st.caption("© 2025 - Demo developed with Streamlit and Azure Speech Service")  