# voice-translator-and-personal-voice
Integrate the Azure Speech service to translate voice with the synthesis with Personal Voice

1. Install Speech Library with pip install azure-cognitiveservices-speech

2. Create the Personal Voice with create_personal_voice.py. configuring the .env file using the .env-template and setting the values:
- SPEECH_KEY: API Key of Speech service
- SPEECH_REGION: region of Speech service
- PROJECT_ID: a string to identify the project
- CONSENT_ID: a string to identify the content
- PERSONAL_VOICE_ID: the id of the personal voice to create
- CONSENT_FILE_PATH: the path to the WAV file with the consent sentence
- VOICE_TALENT_NAME: the name of the talent that recorded the consent
- COMPANY_NAME

Annotate the speaker_profile_id provided when the Personal Voice service is created because is needed to sythesize with Personal Voice.

4. Run the demo of voice translation and syntesis with Personal Voice:

Set the required target languages in the constant LANGUAGES.
If you need to fix the original language set it in the constant ORIGIN_LANGUAGE.
If you want automatic language detection set ORIGIN_LANGUAGE to None and set the constant AUTO_DETECT_LOCALES as the candidates languages.

- Option 1 (black and white text interface): run `python voice_translator_and_personal_voice.py`

- Option 2 (web interface): run `streamlit run voice_translator_and_personal_voice_app.py`
<img src="./Demo.gif" alt="Video Demo"/>

