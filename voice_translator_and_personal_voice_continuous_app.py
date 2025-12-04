# ------------------------------------------------------------  
# app.py – Streamlit UI for Azure Speech real-time translation  
# Newest items on top • single toggle button • displays detected language  
# ------------------------------------------------------------  
import os  
import time  
import json  
import threading
import warnings  
from dotenv import load_dotenv  
import streamlit as st  
from streamlit_autorefresh import st_autorefresh
from utils import *

# Import pycaw for microphone control on Windows
try:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False
    # Silently disable pycaw functionality if not available
  
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

# Constants
ORIGIN_LANGUAGE     = None                       # None → auto-detect  
AUTO_DETECT_LOCALES = ["en-US", "es-ES", "fr-FR", "it-IT", "de-DE", "pt-PT", "nl-NL", "pl-PL", "ru-RU", "ja-JP"]

# Voice options for TTS
VOICE_OPTIONS = {
    "Ximena (Spanish Female)": "es-ES-Ximena:DragonHDLatestNeural",
    "Tristan (Spanish Male)": "es-ES-Tristan:DragonHDLatestNeural",
    "Personal Voice": "personal"
}

# ------------------- global variables for TTS state -----------
# Flag to track if TTS is currently playing
tts_is_playing = False
# Timestamp of the end of the last TTS (to ignore audio captured during TTS)
tts_end_time = 0.0
# Cache for the microphone volume interface
_microphone_volume_interface = None

# ------------------- microphone control functions --------------
def _get_microphone_volume():
    """Gets the microphone volume interface (with cache)"""
    global _microphone_volume_interface
    
    if not PYCAW_AVAILABLE:
        return None
    
    if _microphone_volume_interface is None:
        try:
            devices = AudioUtilities.GetMicrophone()
            if devices:
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                _microphone_volume_interface = interface.QueryInterface(IAudioEndpointVolume)
        except Exception as e:
            print(f"[DEBUG] Error getting microphone interface: {e}")
            return None
    
    return _microphone_volume_interface

def mute_microphone():
    """Mutes the system microphone (Windows only with pycaw)"""
    volume = _get_microphone_volume()
    if volume:
        try:
            volume.SetMute(1, None)
            print("[DEBUG] 🔇 Microphone MUTED")
            return True
        except Exception as e:
            print(f"[DEBUG] Error muting microphone: {e}")
    return False

def unmute_microphone():
    """Unmutes the system microphone (Windows only with pycaw)"""
    volume = _get_microphone_volume()
    if volume:
        try:
            volume.SetMute(0, None)
            print("[DEBUG] 🔊 Microphone UNMUTED")
            return True
        except Exception as e:
            print(f"[DEBUG] Error unmuting microphone: {e}")
    return False

# ------------------- cached configs for performance -----------
@st.cache_resource(show_spinner=False)
def get_speech_config() -> speechsdk.SpeechConfig:
    """SpeechConfig for synthesis (cached between reloads)"""
    return speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)

@st.cache_resource(show_spinner=False)
def get_audio_output_config() -> speechsdk.audio.AudioOutputConfig:
    """AudioOutputConfig for speaker (cached for reuse)"""
    return speechsdk.audio.AudioOutputConfig(use_default_speaker=True)

@st.cache_resource(show_spinner=False)
def get_audio_input_config() -> speechsdk.audio.AudioConfig:
    """AudioConfig for microphone (cached for reuse)"""
    return speechsdk.audio.AudioConfig(use_default_microphone=True)

# ------------------- cached synthesizer -----------------------
@st.cache_resource(show_spinner=False)
def get_synthesizer() -> speechsdk.SpeechSynthesizer:
    """Create and cache a reusable synthesizer instance"""
    speech_config = get_speech_config()
    audio_config = get_audio_output_config()
    return speechsdk.SpeechSynthesizer(
        speech_config=speech_config,
        audio_config=audio_config
    )

# ------------------- optimized synthesize function ------------
def build_ssml(text: str, target_lang: str, voice_choice: str) -> str:
    """Build SSML string based on voice choice"""
    if voice_choice == "personal":
        speaker_profile_id = os.getenv("SPEAKER_PROFILE_ID") or ""
        locale = LANGUAGES[target_lang][0]
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
    else:
        return f"""
        <speak version='1.0' xml:lang='{target_lang}'
            xmlns='http://www.w3.org/2001/10/synthesis'>
            <voice name='{voice_choice}'>
                {text}
            </voice>
        </speak>
        """

def synthesize_and_play(text: str, target_lang: str, voice_choice: str, synthesizer: speechsdk.SpeechSynthesizer) -> bytes:
    """
    Synthesizes text and plays it WITHOUT pausing recognition.
    Uses global flags to mark TTS as playing, preventing transcription processing during playback.
    The synthesizer automatically plays the audio through the speaker.
    Returns audio bytes for playback in UI.
    """
    global tts_is_playing, tts_end_time
    
    if not text:
        return None
    
    audio_bytes = None
    mic_was_muted = False
    
    try:
        # Mark TTS as playing BEFORE starting
        tts_is_playing = True
        tts_start_time = time.time()
        print(f"[DEBUG] *** TTS SYNTHESIS STARTED *** tts_is_playing = True at {tts_start_time:.2f}")
        
        # Mute microphone before playing
        mic_was_muted = mute_microphone()
        
        # Build SSML and synthesize
        # speak_ssml_async plays audio through the configured audio_config and returns bytes
        ssml = build_ssml(text, target_lang, voice_choice)
        result = synthesizer.speak_ssml_async(ssml).get()
        
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            audio_bytes = result.audio_data
            print(f"[DEBUG] Synthesis completed, audio size: {len(audio_bytes)} bytes")
            
            # Wait for audio to finish playing
            # Estimate duration: 16kHz, 16-bit, mono = 32000 bytes per second
            estimated_duration = len(audio_bytes) / 32000.0
            wait_time = estimated_duration + 0.3  # Small buffer
            print(f"[DEBUG] Waiting {wait_time:.2f}s for audio playback...")
            time.sleep(wait_time)
        else:
            print(f"[DEBUG] Synthesis failed: {result.reason}")
        
        return audio_bytes
    
    except Exception as e:
        print(f"[DEBUG] Error in synthesize_and_play: {e}")
        return None
    
    finally:
        # Always unmute microphone
        if mic_was_muted:
            time.sleep(0.1)  # Small pause before unmuting
            unmute_microphone()
        
        # Mark that TTS finished AFTER completion
        tts_is_playing = False
        tts_end_time = time.time()
        duration = tts_end_time - tts_start_time
        print(f"[DEBUG] *** TTS PLAYBACK FINISHED *** tts_is_playing = False at {tts_end_time:.2f} (duration: {duration:.2f}s)")

# ------------------- recognizer builder -----------------------  
def build_recognizer(detect_lang: bool, source_lang: str = None) -> speechsdk.translation.TranslationRecognizer:  
    # Create a translation configuration
    translation_cfg = speechsdk.translation.SpeechTranslationConfig(  
        subscription=speech_key,  
        endpoint=speech_endpoint,  
    )
    
    # Set the target languages (all at once for efficiency)
    for code in LANGUAGES:  
        translation_cfg.add_target_language(code)  
  
    # Use cached audio config for microphone
    audio_cfg = get_audio_input_config()
  
    if detect_lang:
        # Auto-detect source language
        auto_cfg = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(  
            languages=AUTO_DETECT_LOCALES  
        )
        # Set the language detection mode to continuous
        translation_cfg.set_property(
            property_id=speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode, 
            value='Continuous'
        )
        return speechsdk.translation.TranslationRecognizer(  
            translation_config=translation_cfg,  
            auto_detect_source_language_config=auto_cfg,  
            audio_config=audio_cfg,  
        )  
    else:
        translation_cfg.speech_recognition_language = source_lang or "en-US"
        return speechsdk.translation.TranslationRecognizer(  
            translation_config=translation_cfg,  
            audio_config=audio_cfg,  
        )  
  
# --------------------- background worker ----------------------  
def recognition_worker(stop_event: threading.Event,  
                       transcript_list: list,  
                       translation_list: list,  
                       status_dict: dict,  
                       ui_ping: threading.Event,
                       synthesis_enabled: bool,
                       target_lang: str,
                       voice_choice: str,
                       audio_list: list,
                       detect_lang: bool,
                       source_lang: str):  
    
    # Ensure microphone is unmuted at start
    unmute_microphone()
    
    # Build recognizer once with current settings
    recognizer = build_recognizer(detect_lang, source_lang)
    
    # Create synthesizer once if synthesis is enabled (reuse it for all syntheses)
    synthesizer = get_synthesizer() if synthesis_enabled else None
  
    def should_discard_tts_echo(text):
        """
        Determines if the transcribed text should be discarded as TTS echo.
        Returns True if it should be discarded, False if valid.
        """
        global tts_is_playing, tts_end_time
        
        if not text:
            return True
        
        current_time = time.time()
        time_since_tts = current_time - tts_end_time if tts_end_time > 0 else 999
        
        # Log for debugging
        print(f"[DEBUG] Transcribed: '{text}' | tts_is_playing={tts_is_playing} | time_since_tts={time_since_tts:.2f}s")
        
        # If TTS is playing OR just finished, DISCARD
        # With pycaw, we use a shorter window (0.5s) since the microphone is muted
        time_since_tts_end = current_time - tts_end_time
        discard_window = 0.5 if PYCAW_AVAILABLE else 1.5
        
        if tts_is_playing or (tts_end_time > 0 and time_since_tts_end < discard_window):
            print(f"[DEBUG] ⚠️ DISCARDED (TTS active or recent: {time_since_tts_end:.2f}s ago) - '{text}'")
            return True
        
        return False
    
    def on_recognized(evt: speechsdk.translation.TranslationRecognitionEventArgs):  
        if evt.result.reason != speechsdk.ResultReason.TranslatedSpeech:  
            return  
        
        # Check if it should be discarded due to TTS echo
        if should_discard_tts_echo(evt.result.text):
            return
  
        # 1) Update lists (latest first)  
        transcript_list.insert(0, evt.result.text)  
        translation_list.insert(0, dict(evt.result.translations))  
  
        # 2) Extract language + confidence  
        lang_res = speechsdk.AutoDetectSourceLanguageResult(evt.result)  
        detected_lang = lang_res.language  
  
        #try:  
        json_result = json.loads(evt.result.json)  
        confidence = json_result.get("SpeechPhrase", {}).get("PrimaryLanguage", {}).get("Confidence", None)
        #except Exception:  
        #    confidence = None  
  
        # store in shared dict  
        status_dict["language"] = detected_lang  
        status_dict["confidence"] = confidence  

        # 3) Synthesize translation if enabled (WITHOUT pausing recognition)
        if synthesis_enabled and synthesizer and target_lang in evt.result.translations:
            translated_text = evt.result.translations[target_lang]
            if translated_text:
                # Launch synthesis in a separate thread
                def synthesize_in_thread():
                    try:
                        audio_bytes = synthesize_and_play(translated_text, target_lang, voice_choice, synthesizer)
                        if audio_bytes:
                            audio_list.insert(0, audio_bytes)
                            ui_ping.set()  # Notify UI after audio is ready
                    except Exception as e:
                        print(f"[DEBUG] Synthesis error: {e}")
                
                threading.Thread(target=synthesize_in_thread, daemon=True).start()
  
        ui_ping.set()  # ask UI to refresh  
  
    recognizer.recognized.connect(on_recognized)  
    recognizer.start_continuous_recognition()  
  
    while not stop_event.is_set():  
        time.sleep(0.1)  
  
    recognizer.stop_continuous_recognition()
    
    # Ensure microphone is unmuted when stopping
    unmute_microphone()  
  
# --------------------------- Streamlit UI ---------------------  
# CSS for sidebar width
st.markdown(
"""
    <style>
        /* Ajusta el ancho de la sidebar */
            [data-testid="stSidebar"] {
                min-width: 240px;
                max-width: 240px;
        }
        [data-testid="stSidebarContent"] {
            padding: 1rem;
        }
    </style>
    """,
    unsafe_allow_html=True
)

st.set_page_config(page_title="Live Speech Translator", layout="wide")  
st.image("microsoft.png", width=100)
st.title("🎙️ Real-time Speech-to-Speech Translation")  
st.caption("Azure Speech Translation  •  Automatic language detection  •  Text-to-Speech")

st.write(  
    "Click **Start** and speak into your microphone. "  
    "The system will transcribe and translate your sentences in the target languages."
)

# ---- one-time session state initialisation ----  
if "transcript" not in st.session_state:  
    st.session_state.transcript   = []          # list[str]  
    st.session_state.translation = []           # list[dict]  
    st.session_state.audio        = []          # list[bytes] - audio data
    st.session_state.status      = {"language": None, "confidence": None}  
    st.session_state.thread       = None  
    st.session_state.stop_event   = None  
    st.session_state.ui_ping      = threading.Event()  
  
# current status  
is_recording = (  
    st.session_state.thread is not None and  
    st.session_state.thread.is_alive()  
)

with st.sidebar:
    st.markdown("### Settings")
    detect_language = st.checkbox('Detect language', True, disabled=is_recording, help=f"Automatically detect the language between {', '.join(AUTO_DETECT_LOCALES)} or set it to {ORIGIN_LANGUAGE}.")
    if not detect_language:
        ORIGIN_LANGUAGE = st.selectbox("Source language:", 
                                       [f"{code}" for code in AUTO_DETECT_LOCALES],
                                       index=0,
                                       disabled=is_recording)  # Default to the first language (English)
    
    st.markdown("---")
    st.markdown("### Synthesis Settings")
    synthesis_enabled = st.checkbox("Enable TTS synthesis", True, disabled=is_recording, help="Synthesize translations with text-to-speech")
    
    if synthesis_enabled:
        # Target language selection for synthesis
        codes_names = [f"{code} - {name}" for code, (_, name) in LANGUAGES.items()]
        target_selection = st.selectbox("Target language for synthesis", codes_names, index=1, disabled=is_recording)  # Default to Spanish (index 1)
        target_lang_code = target_selection.split(" - ")[0]
        
        # Voice selection
        voice_display_name = st.selectbox(
            "Select voice",
            list(VOICE_OPTIONS.keys()),
            index=0,  # Default to Ximena
            disabled=is_recording,
            help="Choose between Spanish female, male, or personal voice"
        )
        selected_voice = VOICE_OPTIONS[voice_display_name]
    else:
        target_lang_code = "es"
        selected_voice = VOICE_OPTIONS["Ximena (Spanish Female)"]

# ------------------ single toggle button ---------------------  
button_label = "⏹️ Stop" if is_recording else "▶️ Start"  
  
if st.button(button_label, type="primary"):  
    if is_recording:  
        # stop  
        st.session_state.stop_event.set()  
    else:  
        # start fresh lists & status  
        st.session_state.transcript.clear()  
        st.session_state.translation.clear()  
        st.session_state.audio.clear()
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
                target_lang_code,
                selected_voice,
                st.session_state.audio,
                detect_language,
                ORIGIN_LANGUAGE,
            ),  
        )  
        st.session_state.thread.start()  
  
    do_rerun()  
  
# ---------------- detected language + confidence --------------  
lang  = st.session_state.status.get("language")  
conf  = st.session_state.status.get("confidence")  
  
if lang:
    if conf == "Unknown":
        st.markdown(f"**Detected language:** {get_language_name(lang)}")
    else:
        st.markdown(f"**Detected language:** {get_language_name(lang)}  • **Confidence:** {conf}")  
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
        st.info("Press “Start” and speak…")  
  
with col_r:  
    st.subheader("Latest translations")  
    if st.session_state.translation:  
        total = len(st.session_state.translation)  
        for idx, tdict in enumerate(st.session_state.translation):  
            num = total - idx  
            st.markdown(f"**Sentence {num}**")  
            translations=""
            # Mostrar la traducción del idioma destino seleccionado para síntesis
            if target_lang_code in tdict:
                translations += f"{LANGUAGES.get(target_lang_code, ('', ''))[1]}: {tdict[target_lang_code]}\n"    # Mostrar la traducción del idioma destino seleccionado para síntesis
            for lang_code, txt in tdict.items():  
                translations += f"- {LANGUAGES.get(lang_code, ("", ""))[1]}: {txt}\n" 
            st.write(translations)
            
            # Display audio player if synthesis is enabled and audio is available
            if synthesis_enabled and idx < len(st.session_state.audio):
                if st.session_state.audio[idx]:
                    st.audio(st.session_state.audio[idx], format="audio/wav")
            
            st.write("---")  
    else:  
        st.info("Translations will appear here.")  
  
# --------------------------- footer ---------------------------  
src_info = "auto-detected" if ORIGIN_LANGUAGE is None else ORIGIN_LANGUAGE  
st.caption(f"Source language: {src_info} • Powered by Azure Speech Service")  