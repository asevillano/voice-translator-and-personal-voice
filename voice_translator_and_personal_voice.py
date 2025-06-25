from dotenv import load_dotenv
from datetime import datetime
import os

# Import namespaces
import azure.cognitiveservices.speech as speech_sdk

LANGUAGES = {
        "ca": ("ca-ES", "Catalan"),
        "de": ("de-DE", "German"),
        "en": ("en-US", "English"),
        "fr": ("fr", "French"),
        "ja": ("ja-JP", "Japanese"),
        "it": ("it-IT", "Italian"),
        "pt": ("pt-PT", "Portuguese")
}

def main():
    try:
        global speech_config
        global translation_config

        # Get Configuration Settings
        load_dotenv()
        speech_key = os.getenv('SPEECH_KEY')
        speech_region = os.getenv('SPEECH_REGION')

        # Configure translation
        translation_config = speech_sdk.translation.SpeechTranslationConfig(speech_key, speech_region)
        translation_config.speech_recognition_language = 'es-ES' # Traducir desde Español. #'en-US'
        langs = LANGUAGES.keys()
        for lang in langs:
            print(f'lang: {lang}')
            translation_config.add_target_language(lang)
        print('Ready to translate from', translation_config.speech_recognition_language)

        # Configure speech
        speech_config = speech_sdk.SpeechConfig(speech_key, speech_region)

        # Get user input
        targetLanguage = ''
        while targetLanguage != 'quit':
            mensaje='\nEnter a target language:'
            for id, (lang_code, language) in LANGUAGES.items():
                mensaje += f"\n\t{id}: {language}"
            mensaje += "\nEnter anything else to stop\n"
            #targetLanguage = input('\nEnter a target language\n fr = French\n en = English\n pt = Portuguese\n Enter anything else to stop\n').lower()
            targetLanguage = input(mensaje).lower()

            if targetLanguage in translation_config.target_languages:
                SynthesizePersonalVoice(targetLanguage)
            else:
                targetLanguage = 'quit'

    except Exception as ex:
        print(ex)

def TranslateVoice(targetLanguage):
    translation = ''

    # Translate speech
    audio_config = speech_sdk.AudioConfig(use_default_microphone=True)
    translator = speech_sdk.translation.TranslationRecognizer(translation_config, audio_config = audio_config)
    print("Speak now...")
    result = translator.recognize_once_async().get()
    print(f'Translating "{result.text}" to {LANGUAGES.get(targetLanguage, ("", ""))[1]}')
    try:
        translation = result.translations[targetLanguage]
        print(f'translation: {translation}')
    except Exception as ex:
        print(ex)

    return translation

def SynthesizePersonalVoice(targetLanguage):

    # Voice translation
    translation = TranslateVoice(targetLanguage)

    # Synthesize translation
    speaker_profile_id = '43b2d9e8-8756-4e54-b72a-d26c7b52406f'

    # Use PhoenixLatestNeural if you want word boundary event
    language = LANGUAGES.get(targetLanguage, ("", ""))[0]
    ssml = f"""<speak version='1.0' xml:lang='en-US' xmlns='http://www.w3.org/2001/10/synthesis' 
            xmlns:mstts='http://www.w3.org/2001/mstts'> 
            <voice name='DragonLatestNeural'> 
            <mstts:ttsembedding speakerProfileId='{speaker_profile_id}'/> 
            <mstts:express-as style='Prompt'> 
            <lang xml:lang='{language}'> {translation} </lang> 
            </mstts:express-as> 
            </voice></speak>"""

    def word_boundary(evt):
        print(f"Word Boundary: Text='{evt.text}', Audio offset={evt.audio_offset / 10000}ms, Duration={evt.duration / 10000}ms, text={evt.text}")

    speech_synthesizer = speech_sdk.SpeechSynthesizer(speech_config)
    speech_synthesizer.synthesis_word_boundary.connect(word_boundary)
    speak = speech_synthesizer.speak_ssml_async(ssml).get()

    if speak.reason != speech_sdk.ResultReason.SynthesizingAudioCompleted:
        print(speak.reason)

if __name__ == "__main__":
    main()