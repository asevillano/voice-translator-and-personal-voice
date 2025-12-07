"""  
Speech-to-Speech translator with automatic source-language detection
and using personal voice to synthesize the translation.
-------------------------------------------------------------------  
Requirements:  
pip install azure-cognitiveservices-speech python-dotenv  
Environment variables:  
    SPEECH_KEY          => your Speech resource key  
    SPEECH_REGION       => your Speech resource region 
    SPEAKER_PROFILE_ID  => ID of the custom neural voice profile (optional)
"""  
  
from dotenv import load_dotenv  
import os
import json
import time
import azure.cognitiveservices.speech as speech_sdk  
  
# ──────────────────────────────────────────────────────────────────────────────  
# Working languages  
#   key   -> ISO code used as the target when requesting translation  
#   locale-> locale code used as a candidate for automatic language detection
#   name  -> label to display on screen
# ──────────────────────────────────────────────────────────────────────────────  
LANGUAGES = {
    "bg": ("bg-BG", "Bulgarian"),
    "hr": ("hr-HR", "Croatian"),
    "cs": ("cs-CZ", "Czech"),
    "da": ("da-D", "Danish"),
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
    "sk": ("sk-S", "Slovak"),
    "sl": ("sl-SI", "Slovenian"),
    "es": ("es-ES", "Spanish"),
    "sv": ("sv-SE", "Swedish")
}

# Source language for translation. Set None to use automatic detection.
ORIGIN_LANGUAGE = None #'en-US'

# List of languages to be passed to the service for automatic identification
AUTO_DETECT_LOCALES: dict[str, str] = {
    "bg-BG": "Bulgarian", "hr-HR": "Croatian",  
    "cs-CZ": "Czech", "da-D":  "Danish",  
    "nl-NL": "Dutch", "en-US": "English",  
    "et-EE": "Estonian", "fi-FI": "Finnish",  
    "fr-FR": "French", "de-DE": "German",  
    "el-GR": "Greek", "hu-HU": "Hungarian",  
    "ga-IE": "Irish", "it-IT": "Italian",  
    "lv-LV": "Latvian", "lt-LT": "Lithuanian",  
    "mt-MT": "Maltese", "pl-PL": "Polish",  
    "pt-PT": "Portuguese", "ro-RO": "Romanian",  
    "sk-S":  "Slovak", "sl-SI": "Slovenian",  
    "es-ES": "Spanish", "sv-SE": "Swedish",  
}
if ORIGIN_LANGUAGE is None:
    print("Languages for automatic detection:", list(AUTO_DETECT_LOCALES.keys()))
  
# ------------------------------------------------------------------------------  
# Configuration initialization  
# ------------------------------------------------------------------------------  
def build_translation_config() -> speech_sdk.translation.SpeechTranslationConfig:  
    load_dotenv(override=True)  # Load environment variables from .env file
    speech_key = os.getenv("SPEECH_KEY")  
    speech_region = os.getenv("SPEECH_REGION")  
  
    # Translation configuration  
    translation_config = speech_sdk.translation.SpeechTranslationConfig(  
        subscription=speech_key,  
        region=speech_region,
        #speech_recognition_language=ORIGIN_LANGUAGE
    )  
  
    # Source language for translation
    if ORIGIN_LANGUAGE is not None:
        translation_config.speech_recognition_language = ORIGIN_LANGUAGE

    # Languages available for translation (all keys in the dictionary)
    for lang_code in LANGUAGES.keys():  
        translation_config.add_target_language(lang_code)  

    # Audio configuration (default microphone)
    audio_config = speech_sdk.AudioConfig(use_default_microphone=True) 
  
    # Configuration for automatic or fixed source language detection
    if ORIGIN_LANGUAGE is None:
        auto_config = speech_sdk.languageconfig.AutoDetectSourceLanguageConfig(languages=list(AUTO_DETECT_LOCALES.keys()))
        # Translation recognizer with auto-detect  
        recognizer = speech_sdk.translation.TranslationRecognizer(  
            translation_config=translation_config,  
            auto_detect_source_language_config=auto_config,  
            audio_config=audio_config  
        )
    else:
        # Fixed Language
        recognizer = speech_sdk.translation.TranslationRecognizer(  
            translation_config=translation_config,
            audio_config=audio_config  
        )

    return recognizer
  
# ------------------------------------------------------------------------------  
# Translation to target languages
# ------------------------------------------------------------------------------  
def translate_voice(recognizer, target_language: str) -> str:  
  
    print("Speak now…")
    start = time.time()
    result = recognizer.recognize_once_async().get()
    #result = recognizer.recognize_once()
    end = time.time()
    #print(f"Execution time: {(end - start):.4f} segundos")

    # Evaluate the result 
    if result.reason == speech_sdk.ResultReason.TranslatedSpeech:
        if ORIGIN_LANGUAGE is None:
            detected_lang = speech_sdk.AutoDetectSourceLanguageResult(result).language
            json_result = json.loads(result.json)
            #print(f"Result in JSON: {json.dumps(json_result, indent=2)}")  # Mostrar el JSON completo
            confidence = json_result.get("SpeechPhrase", {}).get("PrimaryLanguage", {}).get("Confidence", None)

            # Search for the language name
            nombre_idioma = None
            for short_code, values in LANGUAGES.items():
                if detected_lang in values:
                    nombre_idioma = next((v for v in values if "-" not in v), None)
                    break

            print(f"Detected language → {nombre_idioma} with confidence {confidence}")  
        
        print(f"Recognized text → {result.text}")  
        # Display the translated text for speech synthesis
        translated_text = result.translations.get(target_language, "")  
        print(f"Translation to {LANGUAGES.get(target_language, ("", ""))[1]} → {translated_text}")  
  
        # We also display all available translations
        for lang in result.translations:
            print(f'\t- {LANGUAGES.get(lang, ("", ""))[1]}: \t{result.translations[lang]}') 
  
        return translated_text  
  
    elif result.reason == speech_sdk.ResultReason.NoMatch:  
        print("No speech could be recognized.")  
    else:  # Cancelado o error  
        cancellation_details = result.cancellation_details  
        print(f"Operation cancelled: {cancellation_details.reason}")  
        if cancellation_details.error_details:  
            print(f"Error details: {cancellation_details.error_details}")  
  
    return ""  
  
  
# ------------------------------------------------------------------------------  
# Speech synthesis with Personal Voice Service  
# ------------------------------------------------------------------------------  
def synthesize_personal_voice(speech_config, text: str, target_language: str):  
    if not text:  
        return  
  
    speaker_profile_id = os.getenv("SPEAKER_PROFILE_ID")  # opcional  
    locale = LANGUAGES[target_language][0]  
  
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
  
    synthesizer = speech_sdk.SpeechSynthesizer(speech_config=speech_config)  
  
    def on_word_boundary(evt):  
        print(f"WordBoundary → '{evt.text}' (offset: {evt.audio_offset/10000:.0f} ms)")  
  
    synthesizer.synthesis_word_boundary.connect(on_word_boundary)  
  
    result = synthesizer.speak_ssml_async(ssml).get()  
    if result.reason != speech_sdk.ResultReason.SynthesizingAudioCompleted:  
        print(f"Error during synthesis: {result.reason}")  
  
  
# ------------------------------------------------------------------------------  
# Main program  
# ------------------------------------------------------------------------------  
def main():  
    try:  
        recognizer = build_translation_config()  
        speech_config = speech_sdk.SpeechConfig(  
            subscription=os.getenv("SPEECH_KEY"),  
            region=os.getenv("SPEECH_REGION")  
        )   

        while True:
            print("\nAvailable languages for translation:")

            # Extract pairs (short code, language name)
            pares = []
            for code, values in LANGUAGES.items():
                name = next((v for v in values if "-" not in v), "")
                pares.append((code, name))
            # Display in rows of 3 columns
            for i in range(0, len(pares), 3):
                fila = pares[i:i+3]
                columnas = [f"{code} → {name:<10}" for code, name in fila]
                print("  |  ".join(columnas))

            target = input("Enter language to synthesize the translation (or 'quit' to exit): ").lower()  
            if target == "quit":  
                break  
            if target not in LANGUAGES:  
                print("Unsupported language.")  
                continue  

            translated = translate_voice(recognizer, target)  
            synthesize_personal_voice(speech_config, translated, target)  
  
    except Exception as ex:  
        print(f"Se produjo una excepción: {ex}")  
  
  
if __name__ == "__main__":  
    main()  