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
    from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
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
if not speech_region:  
    st.error("Missing SPEECH_REGION environment variable.")  
    st.stop()  
speech_endpoint = f"https://{speech_region}.api.cognitive.microsoft.com"  
# Custom subdomain endpoint (required for Entra ID auth)
speech_endpoint_custom = os.getenv("SPEECH_ENDPOINT")  # e.g. https://myresource.cognitiveservices.azure.com

# Azure AD token-based authentication (used when SPEECH_KEY is not set)
# Cognitive Services Speech User Impersonation role must be assigned to the user/service principal for this to work
_credential = None
def get_speech_token() -> str:
    """Get an Azure AD token for Cognitive Services (cached/refreshed by azure-identity)."""
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential()
    return _credential.get_token("https://cognitiveservices.azure.com/.default").token

use_token_auth = not speech_key
if use_token_auth and not speech_endpoint_custom:
    st.error("Entra ID auth requires SPEECH_ENDPOINT with custom subdomain (e.g. https://myresource.cognitiveservices.azure.com). Set it in .env")
    st.stop()

if "auth_logged" not in st.session_state:
    st.session_state.auth_logged = True
    if use_token_auth:
        print("[AUTH] Using Azure AD token authentication (no API key)")
    else:
        print("[AUTH] Using API key authentication")

# Constants
PRIMARY_LANGUAGE = "es-ES"  # Main language (Spanish)
ORIGIN_LANGUAGE     = None                       # None → auto-detect  
AUTO_DETECT_LOCALES = ["en-US", "es-ES", "fr-FR", "it-IT", "de-DE", "pt-PT", "nl-NL", "pl-PL", "ru-RU", "ja-JP"]

# Voice options for TTS
VOICE_OPTIONS = {
    "Ximena (Spanish Female)": "es-ES-Ximena:DragonHDLatestNeural",
    "Tristan (Spanish Male)": "es-ES-Tristan:DragonHDLatestNeural",
    #"Personal Voice": "personal"
}

# ------------------- global variables for TTS state -----------
# Flag to track if TTS is currently playing
tts_is_playing = False
# Timestamp of the end of the last TTS (to ignore audio captured during TTS)
tts_end_time = 0.0
# Cache for the microphone volume interface
_microphone_volume_interface = None
# Track the last detected non-Spanish language
last_non_spanish_lang = "en"  # Default to English

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
    if use_token_auth:
        cfg = speechsdk.SpeechConfig(endpoint=speech_endpoint_custom)
        cfg.authorization_token = get_speech_token()
        return cfg
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
    """Build SSML string based on voice choice and target language"""
    
    # Map language codes to high-quality neural voices (based on Microsoft documentation)
    voice_map = {
        "es": "es-ES-ElviraNeural",           # Spanish (Spain) - Female
        "en": "en-US-AvaNeural",              # English (US) - Female
        "fr": "fr-FR-DeniseNeural",           # French (France) - Female
        "de": "de-DE-KatjaNeural",            # German (Germany) - Female
        "it": "it-IT-ElsaNeural",             # Italian (Italy) - Female
        "pt": "pt-PT-RaquelNeural",           # Portuguese (Portugal) - Female
        "nl": "nl-NL-FennaNeural",            # Dutch (Netherlands) - Female
        "pl": "pl-PL-AgnieszkaNeural",        # Polish (Poland) - Female
        "ru": "ru-RU-SvetlanaNeural",         # Russian (Russia) - Female
        "ja": "ja-JP-NanamiNeural",           # Japanese (Japan) - Female
        "ar": "ar-SA-ZariyahNeural",          # Arabic (Saudi Arabia) - Female
        "bg": "bg-BG-KalinaNeural",           # Bulgarian (Bulgaria) - Female
        "ca": "ca-ES-JoanaNeural",            # Catalan - Female
        "cs": "cs-CZ-VlastaNeural",           # Czech (Czechia) - Female
        "da": "da-DK-ChristelNeural",         # Danish (Denmark) - Female
        "el": "el-GR-AthinaNeural",           # Greek (Greece) - Female
        "et": "et-EE-AnuNeural",              # Estonian (Estonia) - Female
        "fi": "fi-FI-SelmaNeural",            # Finnish (Finland) - Female
        "he": "he-IL-HilaNeural",             # Hebrew (Israel) - Female
        "hi": "hi-IN-SwaraNeural",            # Hindi (India) - Female
        "hr": "hr-HR-GabrijelaNeural",        # Croatian (Croatia) - Female
        "hu": "hu-HU-NoemiNeural",            # Hungarian (Hungary) - Female
        "id": "id-ID-GadisNeural",            # Indonesian (Indonesia) - Female
        "ko": "ko-KR-SunHiNeural",            # Korean (Korea) - Female
        "lt": "lt-LT-OnaNeural",              # Lithuanian (Lithuania) - Female
        "lv": "lv-LV-EveritaNeural",          # Latvian (Latvia) - Female
        "ms": "ms-MY-YasminNeural",           # Malay (Malaysia) - Female
        "nb": "nb-NO-PernilleNeural",         # Norwegian Bokmål (Norway) - Female
        "ro": "ro-RO-AlinaNeural",            # Romanian (Romania) - Female
        "sk": "sk-SK-ViktoriaNeural",         # Slovak (Slovakia) - Female
        "sl": "sl-SI-PetraNeural",            # Slovenian (Slovenia) - Female
        "sv": "sv-SE-SofieNeural",            # Swedish (Sweden) - Female
        "ta": "ta-IN-PallaviNeural",          # Tamil (India) - Female
        "te": "te-IN-ShrutiNeural",           # Telugu (India) - Female
        "th": "th-TH-PremwadeeNeural",        # Thai (Thailand) - Female
        "tr": "tr-TR-EmelNeural",             # Turkish (Türkiye) - Female
        "uk": "uk-UA-PolinaNeural",           # Ukrainian (Ukraine) - Female
        "vi": "vi-VN-HoaiMyNeural",           # Vietnamese (Vietnam) - Female
        "zh": "zh-CN-XiaoxiaoNeural"          # Chinese (Mandarin, Simplified) - Female
    }
    
    # Get locale from LANGUAGES dict or construct it
    if target_lang in LANGUAGES:
        locale = LANGUAGES[target_lang][0]
    else:
        # If not in LANGUAGES, try to construct it (e.g., "en" -> "en-US")
        locale = f"{target_lang}-{target_lang.upper()}" if len(target_lang) == 2 else target_lang
    
    # If Personal Voice is selected, use it for ANY language
    if voice_choice == "personal":
        speaker_profile_id = os.getenv("SPEAKER_PROFILE_ID") or ""
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
    
    # Determine which voice to use based on target language
    if target_lang == "es":
        # Spanish synthesis: use the selected Spanish voice (Ximena or Tristan)
        # Use Ximena or Tristan DragonHD voice
        return f"""
        <speak version='1.0' xml:lang='es-ES'
            xmlns='http://www.w3.org/2001/10/synthesis'>
            <voice name='{voice_choice}'>
                {text}
            </voice>
        </speak>
        """
    else:
        # Non-Spanish synthesis: use appropriate neural voice from voice_map
        neural_voice = voice_map.get(target_lang, "en-US-AvaNeural")
        return f"""
        <speak version='1.0' xml:lang='{locale}'
            xmlns='http://www.w3.org/2001/10/synthesis'>
            <voice name='{neural_voice}'>
                {text}
            </voice>
        </speak>
        """

def synthesize_and_play(text: str, target_lang: str, voice_choice: str, synthesizer: speechsdk.SpeechSynthesizer) -> bytes:
    """
    Synthesizes text and plays it SYNCHRONOUSLY with precise microphone control.
    
    CRITICAL DESIGN:
    - Uses speak_ssml_async().get() which is FULLY SYNCHRONOUS and BLOCKING
    - When AudioConfig uses use_default_speaker=True, .get() waits until:
      1. Audio synthesis completes
      2. Audio playback through speaker FULLY completes
    - Microphone is muted BEFORE synthesis starts
    - Microphone is unmuted AFTER playback fully completes
    - NO time estimation needed - SDK handles exact timing
    - tts_is_playing flag protects against any edge cases
    
    Returns: Audio bytes for UI display (optional)
    """
    global tts_is_playing, tts_end_time
    
    if not text:
        return None
    
    audio_bytes = None
    mic_was_muted = False
    tts_start_time = time.time()
    
    try:
        # STEP 1: Set flag BEFORE any synthesis activity
        tts_is_playing = True
        print(f"[DEBUG] *** TTS START *** Flag set at {tts_start_time:.3f}")
        
        # STEP 2: Mute microphone IMMEDIATELY before synthesis
        mic_was_muted = mute_microphone()
        if not mic_was_muted and PYCAW_AVAILABLE:
            print(f"[DEBUG] ⚠️ WARNING: Failed to mute microphone!")
        
        # STEP 3: Build SSML
        ssml = build_ssml(text, target_lang, voice_choice)
        
        # STEP 4: SYNCHRONOUS synthesis + playback
        # Refresh Azure AD token before synthesis (tokens expire after ~1h)
        if use_token_auth:
            get_speech_config().authorization_token = get_speech_token()
        # This call BLOCKS until audio is FULLY PLAYED through the speaker
        print(f"[DEBUG] Calling speak_ssml_async().get() - will block until playback completes")
        result = synthesizer.speak_ssml_async(ssml).get()
        print(f"[DEBUG] speak_ssml_async().get() returned - playback is complete")
        
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            audio_bytes = result.audio_data
            print(f"[DEBUG] ✅ Synthesis successful, audio size: {len(audio_bytes)} bytes")
        elif result.reason == speechsdk.ResultReason.Canceled:
            cancellation = result.cancellation_details
            print(f"[DEBUG] ❌ Synthesis canceled: {cancellation.reason}")
            if cancellation.reason == speechsdk.CancellationReason.Error:
                print(f"[DEBUG] Error details: {cancellation.error_details}")
        else:
            print(f"[DEBUG] ❌ Synthesis failed: {result.reason}")
        
        return audio_bytes
    
    except Exception as e:
        print(f"[DEBUG] ❌ Exception in synthesize_and_play: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    finally:
        # STEP 5: Unmute microphone AFTER playback completes
        # Small delay to ensure audio driver releases resources
        if mic_was_muted:
            time.sleep(0.05)  # Minimal delay for driver stability
            unmute_microphone()
        
        # STEP 6: Clear flag and record end time
        tts_is_playing = False
        tts_end_time = time.time()
        duration = tts_end_time - tts_start_time
        print(f"[DEBUG] *** TTS END *** Flag cleared at {tts_end_time:.3f} (total duration: {duration:.3f}s)")

# ------------------- recognizer builder -----------------------  
def build_recognizer(detect_lang: bool, source_lang: str = None, target_langs: list = None, auto_detect_locales: list = None) -> speechsdk.translation.TranslationRecognizer:  
    # Create a translation configuration
    if use_token_auth:
        token = get_speech_token()
        print(f"[AUTH] Token acquired (length={len(token)})")
        translation_cfg = speechsdk.translation.SpeechTranslationConfig(
            endpoint=speech_endpoint_custom,
        )
        translation_cfg.authorization_token = token
    else:
        translation_cfg = speechsdk.translation.SpeechTranslationConfig(  
            subscription=speech_key,  
            endpoint=speech_endpoint,  
        )
    
    # Add target languages
    if target_langs:
        for lang in target_langs:
            translation_cfg.add_target_language(lang)
    else:
        translation_cfg.add_target_language("es")  # Default to Spanish
  
    # Use cached audio config for microphone
    audio_cfg = get_audio_input_config()
  
    if detect_lang:
        # Auto-detect source language using the provided locales
        locales_to_use = auto_detect_locales if auto_detect_locales else AUTO_DETECT_LOCALES
        auto_cfg = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(  
            languages=locales_to_use  
        )
        # Set the language detection mode to continuous
        translation_cfg.set_property(
            property_id=speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode, value='Continuous',
        )
        # Set the profanity option to 'raw' to avoid filtering
        translation_cfg.set_property(
            property_id=speechsdk.PropertyId.SpeechServiceResponse_ProfanityOption, value='raw'
        )

        # Set segmentation strategy to Semantic
        translation_cfg.set_property(speechsdk.PropertyId.Speech_SegmentationStrategy, "Semantic")

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
                       voice_choice: str,
                       audio_list: list,
                       detect_lang: bool,
                       source_lang: str,
                       detected_langs_list: list,
                       auto_detect_locales: list = None,
                       selected_languages: list = None):  
    
    # Initialize COM for this thread (required for pycaw)
    if PYCAW_AVAILABLE:
        try:
            CoInitialize()
            print("[DEBUG] COM initialized for recognition thread")
        except Exception as e:
            print(f"[DEBUG] Error initializing COM: {e}")
    
    # Ensure microphone is unmuted at start
    unmute_microphone()
    
    # Set initial default for last_non_spanish_lang based on selection
    global last_non_spanish_lang
    if selected_languages and len(selected_languages) > 0:
        # Use the first selected language as the initial default
        last_non_spanish_lang = selected_languages[0]
    else:
        # No specific selection → use first non-Spanish language from LANGUAGES dict
        first_non_spanish = next((code for code in LANGUAGES if code != "es"), "en")
        last_non_spanish_lang = first_non_spanish
    print(f"[DEBUG] Initial last_non_spanish_lang set to: {last_non_spanish_lang}")
    
    # Debug: show auto_detect_locales
    print(f"[DEBUG] auto_detect_locales received: {auto_detect_locales}")
    print(f"[DEBUG] detect_lang: {detect_lang}, source_lang: {source_lang}")
    print(f"[DEBUG] selected_languages: {selected_languages}")
    
    # Build primary recognizer that translates to multiple target languages at once
    # This recognizer will handle all non-Spanish languages -> Spanish/English/etc
    target_langs = ["es", "en", "fr", "de", "it", "pt", "nl", "pl", "ru", "ja"]
    
    try:
        recognizer = build_recognizer(detect_lang, source_lang, target_langs, auto_detect_locales)
        print(f"[DEBUG] Recognizer built successfully")
    except Exception as e:
        print(f"[DEBUG] ❌ Error building recognizer: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Create synthesizer once if synthesis is enabled (reuse it for all syntheses)
    synthesizer = get_synthesizer() if synthesis_enabled else None
  
    def should_discard_tts_echo(text):
        """
        Determines if the transcribed text should be discarded as TTS echo.
        Returns True if it should be discarded, False if valid.
        
        With synchronous synthesis and microphone muting, we should have minimal echo,
        but we still keep a small safety window for any system delays.
        """
        global tts_is_playing, tts_end_time
        
        if not text:
            return True
        
        current_time = time.time()
        
        # Log for debugging
        print(f"[DEBUG] Transcribed: '{text}' | tts_is_playing={tts_is_playing}")
        
        # If TTS is actively playing (flag is set), ALWAYS DISCARD
        if tts_is_playing:
            print(f"[DEBUG] ⚠️ DISCARDED (TTS is actively playing) - '{text}'")
            return True
        
        # Add a minimal safety window after TTS ends (reduced from 0.2s to 0.1s)
        # This is only for edge cases where the audio driver takes time to release the mic
        time_since_tts_end = current_time - tts_end_time
        discard_window = 0.1 if PYCAW_AVAILABLE else 0.5
        
        if tts_end_time > 0 and time_since_tts_end < discard_window:
            print(f"[DEBUG] ⚠️ DISCARDED (TTS ended recently: {time_since_tts_end:.3f}s ago) - '{text}'")
            return True
        
        return False
    
    def on_recognized(evt: speechsdk.translation.TranslationRecognitionEventArgs):  
        global last_non_spanish_lang
        
        print(f"[DEBUG] 🎯 on_recognized called! Reason: {evt.result.reason}")
        
        if evt.result.reason != speechsdk.ResultReason.TranslatedSpeech:
            print(f"[DEBUG] ⚠️ Skipping - not TranslatedSpeech (reason: {evt.result.reason})")
            return  
        
        print(f"[DEBUG] ✅ TranslatedSpeech detected! Text: '{evt.result.text}'")
        
        # Check if it should be discarded due to TTS echo
        if should_discard_tts_echo(evt.result.text):
            return
  
        # 1) Extract language + confidence first
        lang_res = speechsdk.AutoDetectSourceLanguageResult(evt.result)  
        detected_lang = lang_res.language  
  
        try:  
            json_result = json.loads(evt.result.json)  
            confidence = json_result.get("SpeechPhrase", {}).get("PrimaryLanguage", {}).get("Confidence", None)
        except Exception:  
            confidence = None  
  
        # store in shared dict  
        status_dict["language"] = detected_lang  
        status_dict["confidence"] = confidence
        
        # 2) Determine source and target languages based on detected language
        is_spanish = detected_lang.startswith("es")
        
        if not is_spanish:
            # Non-Spanish detected → translate to Spanish, and remember this language
            detected_lang_code = detected_lang.split("-")[0]  # e.g. "en" from "en-US"
            last_non_spanish_lang = detected_lang_code
            status_dict["last_non_spanish"] = last_non_spanish_lang
            target_lang = "es"
            print(f"[DEBUG] Non-Spanish detected ({detected_lang_code}) → translating to Spanish")
        else:
            # Spanish detected → translate to the last non-Spanish language spoken
            target_lang = last_non_spanish_lang
            print(f"[DEBUG] Spanish detected → translating to last non-Spanish language: {target_lang}")
        
        # 3) Get the translation for the target language from the SDK results
        # Since we configured multiple target languages, we can get any translation directly
        translated_text = None
        
        if target_lang in evt.result.translations:
            translated_text = evt.result.translations[target_lang]
            print(f"[DEBUG] Translation from SDK: {detected_lang} -> {target_lang}: '{translated_text}'")
        else:
            print(f"[DEBUG] Warning: Translation for '{target_lang}' not found in results")
            print(f"[DEBUG] Available translations: {list(evt.result.translations.keys())}")
        
        # 4) Update lists with original and translation
        transcript_list.insert(0, evt.result.text)
        translation_dict = {target_lang: translated_text} if translated_text else {}
        translation_list.insert(0, translation_dict)
        detected_langs_list.insert(0, detected_lang)

        # 5) Synthesize translation if enabled
        # CRITICAL: Execute synthesis SYNCHRONOUSLY in the main recognition thread
        # This ensures the microphone remains muted during the entire playback
        if synthesis_enabled and synthesizer and translated_text:
            # Synthesize in the appropriate language
            synthesis_lang = "es" if not is_spanish else target_lang
            
            try:
                print(f"[DEBUG] Starting SYNCHRONOUS synthesis for: '{translated_text}'")
                # Execute synthesis SYNCHRONOUSLY - blocks until playback completes
                audio_bytes = synthesize_and_play(translated_text, synthesis_lang, voice_choice, synthesizer)
                if audio_bytes:
                    audio_list.insert(0, audio_bytes)
                    print(f"[DEBUG] Audio bytes added to list (size: {len(audio_bytes)})")
                print(f"[DEBUG] SYNCHRONOUS synthesis completed")
            except Exception as e:
                print(f"[DEBUG] Synthesis error: {e}")
                import traceback
                traceback.print_exc()
  
        ui_ping.set()  # ask UI to refresh  
  
    # Connect event handlers with debug logging
    def on_recognizing(evt):
        print(f"[DEBUG] 🎤 Recognizing: {evt.result.text}")
    
    def on_canceled(evt):
        print(f"[DEBUG] ❌ Canceled: {evt}")
        if evt.reason == speechsdk.CancellationReason.Error:
            print(f"[DEBUG] Error details: {evt.error_details}")
    
    def on_session_started(evt):
        print(f"[DEBUG] ✅ Session started: {evt}")
    
    def on_session_stopped(evt):
        print(f"[DEBUG] ⏹️ Session stopped: {evt}")
    
    recognizer.recognizing.connect(on_recognizing)
    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)
    recognizer.session_started.connect(on_session_started)
    recognizer.session_stopped.connect(on_session_stopped)
    
    print("[DEBUG] Starting continuous recognition...")
    try:
        recognizer.start_continuous_recognition()
        print("[DEBUG] ✅ Continuous recognition started successfully")
    except Exception as e:
        print(f"[DEBUG] ❌ Error starting recognition: {e}")
        import traceback
        traceback.print_exc()
        return
  
    while not stop_event.is_set():  
        time.sleep(0.1)  
  
    print("[DEBUG] Stopping continuous recognition...")
    recognizer.stop_continuous_recognition()
    
    # Ensure microphone is unmuted when stopping
    unmute_microphone()
    
    # Uninitialize COM before thread exits
    if PYCAW_AVAILABLE:
        try:
            CoUninitialize()
            print("[DEBUG] COM uninitialized for recognition thread")
        except Exception as e:
            print(f"[DEBUG] Error uninitializing COM: {e}")  
  
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
    st.session_state.detected_langs = []        # list[str] - detected language codes
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
    st.markdown("### Language Detection Settings")
    
    # Individual language codes (excluding Spanish)
    non_spanish_langs = {code: (locale, name) for code, (locale, name) in LANGUAGES.items() if code != "es"}
    
    # Track previous state of "All languages" to detect transitions
    if "prev_all_langs" not in st.session_state:
        st.session_state.prev_all_langs = True
    
    # Master checkbox: All languages
    all_langs = st.checkbox("🌍 All languages", value=True, disabled=is_recording)
    
    # If "All languages" was just unchecked, reset all individual checkboxes to False
    if st.session_state.prev_all_langs and not all_langs:
        for code in non_spanish_langs:
            st.session_state[f"lang_{code}"] = False
    st.session_state.prev_all_langs = all_langs
    
    # Individual language checkboxes
    selected_codes = []
    for code, (locale, name) in non_spanish_langs.items():
        checked = st.checkbox(f"{name} ({code})", disabled=is_recording or all_langs, key=f"lang_{code}")
        if checked and not all_langs:
            selected_codes.append(code)
    
    # Build auto_detect_locales
    if all_langs:
        selected_language = None
        auto_detect_locales = [locale_tuple[0] for locale_tuple in LANGUAGES.values()]
    elif selected_codes:
        auto_detect_locales = [LANGUAGES[code][0] for code in selected_codes]
        # Always include Spanish
        if PRIMARY_LANGUAGE not in auto_detect_locales:
            auto_detect_locales.append(PRIMARY_LANGUAGE)
        selected_language = selected_codes[0]
    else:
        # Nothing selected → only Spanish
        auto_detect_locales = [PRIMARY_LANGUAGE]
        selected_language = None
    
    st.caption(f"🔍 Detection locales ({len(auto_detect_locales)}): {', '.join(auto_detect_locales)}")
    
    #st.markdown("---")
    st.markdown("### Synthesis Settings")
    synthesis_enabled = st.checkbox("Enable TTS synthesis", True, disabled=is_recording, help="Synthesize translations with text-to-speech (only in non-Spanish language)")
    
    if synthesis_enabled:
        # Voice selection
        voice_display_name = st.selectbox(
            "Select voice",
            list(VOICE_OPTIONS.keys()),
            index=0,  # Default to Ximena
            disabled=is_recording,
            help="Choose between Spanish female, male, or personal voice for non-Spanish synthesis"
        )
        selected_voice = VOICE_OPTIONS[voice_display_name]
    else:
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
                True,  # always use auto-detection
                None,  # not used anymore
                st.session_state.detected_langs,
                auto_detect_locales,  # pass the dynamic list
                selected_codes if not all_langs else [],  # pass selected languages for fallback
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
        detected_langs = st.session_state.get("detected_langs", [])  
        for idx, sent in enumerate(st.session_state.transcript):  
            num = total - idx          # newest gets highest number  
            lang_id = detected_langs[idx].split("-")[0] if idx < len(detected_langs) else ""  
            st.markdown(f"**{num}. ({lang_id})** {sent}")  
    else:  
        st.info("Press 'Start' and speak…")
  
with col_r:  
    st.subheader("Latest translations")  
    if st.session_state.translation:  
        total = len(st.session_state.translation)  
        last_non_spanish = st.session_state.status.get("last_non_spanish", "en")
        detected_langs = st.session_state.get("detected_langs", [])  
        for idx, tdict in enumerate(st.session_state.translation):  
            num = total - idx  
            # Get the target language from the translation dict (the key is the target language)
            target_lang_code = list(tdict.keys())[0] if tdict else ""
            # Mostrar la traducción en la misma línea que el número y el idioma
            for lang_code, txt in tdict.items():  
                st.markdown(f"**{num}. ({lang_code})** {txt}")
            
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