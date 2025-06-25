import json
import requests
from time import sleep
import os
import logging
from dotenv import load_dotenv
try:
    import customvoice
except ImportError:
    print('Pleae copy folder https://github.com/Azure-Samples/cognitive-services-speech-sdk/tree/master/samples/custom-voice/python/customvoice and keep the same folder structure as github.' )
    quit()

import azure.cognitiveservices.speech as speechsdk


def create_personal_voice(project_id: str,
                          consent_id: str, consent_file_path: str, voice_talent_name: str, company_name: str,
                          personal_voice_id: str, audio_folder: str):

    customvoice.PersonalVoice.delete(config, personal_voice_id)
    customvoice.Consent.delete(config, consent_id)
    customvoice.Project.delete(config, project_id)

    # create project
    project = customvoice.Project.create(config, project_id, customvoice.ProjectKind.PersonalVoice)
    print('Project created. project id: %s' % project.id)

    # upload consent
    consent = customvoice.Consent.create(config, project_id, consent_id, voice_talent_name, company_name, consent_file_path, 'es-ES')
    if consent.status == customvoice.Status.Failed:
        print('Create consent failed. consent id: %s' % consent.id)
        raise Exception
    elif consent.status == customvoice.Status.Succeeded:
        print('Create consent succeeded. consent id: %s' % consent.id)

    # create personal voice
    personal_voice = customvoice.PersonalVoice.create(config, project_id, personal_voice_id, consent_id, audio_folder)
    if personal_voice.status == customvoice.Status.Failed:
        print('Create personal voice failed. personal voice id: %s' % personal_voice.id)
        raise Exception
    elif personal_voice.status == customvoice.Status.Succeeded:
        print('Create personal voice succeeded. personal voice id: %s, speaker profile id: %s' % (personal_voice.id, personal_voice.speaker_profile_id))
    return personal_voice.speaker_profile_id


def speech_synthesis_to_wave_file(text: str, output_file_path: str, speaker_profile_id: str):
    # Creates an instance of a speech config with specified subscription key and service region.
    speech_config = speechsdk.SpeechConfig(subscription=config.key, region=config.region)
    speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm)
    file_config = speechsdk.audio.AudioOutputConfig(filename=output_file_path)
    speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=file_config)


    # use PhoenixLatestNeural if you want word boundary event.  We will support events on DragonLatestNeural in the future.
    ssml = "<speak version='1.0' xml:lang='en-US' xmlns='http://www.w3.org/2001/10/synthesis' " \
           "xmlns:mstts='http://www.w3.org/2001/mstts'>" \
           "<voice name='DragonLatestNeural'>" \
           "<mstts:ttsembedding speakerProfileId='%s'/>" \
           "<mstts:express-as style='Prompt'>" \
           "<lang xml:lang='en-US'> %s </lang>" \
           "</mstts:express-as>" \
           "</voice></speak> " % (speaker_profile_id, text)

    def word_boundary(evt):
        print(f"Word Boundary: Text='{evt.text}', Audio offset={evt.audio_offset / 10000}ms, Duration={evt.duration / 10000}ms, text={evt.text}")

    speech_synthesizer.synthesis_word_boundary.connect(word_boundary)
    result = speech_synthesizer.speak_ssml_async(ssml).get()

    # Check result
    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        print("Speech synthesized for text [{}], and the audio was saved to [{}]".format(text, output_file_path))
        print("result id: {}".format(result.result_id))
    elif result.reason == speechsdk.ResultReason.Canceled:
        cancellation_details = result.cancellation_details
        print("Speech synthesis canceled: {}".format(cancellation_details.reason))
        if cancellation_details.reason == speechsdk.CancellationReason.Error:
            print("Error details: {}".format(cancellation_details.error_details))
            print("result id: {}".format(result.result_id))


# MAIN APPLICATION TO CREATE THE PERSONAL VOICE

logging.basicConfig(filename="customvoice.log",
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    filemode='w')
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Get Configuration Settings
load_dotenv()
speech_key = os.getenv('SPEECH_KEY')
speech_region = os.getenv('SPEECH_REGION')
project_id = os.getenv('PROJECT_ID')
consent_id = os.getenv('CONSENT_ID')
personal_voice_id = os.getenv('PERSONAL_VOICE_ID')

# step 1: create personal voice
# Need consent file and audio file to create personal voice.
# This is consent WAV file template.
# I [voice talent name] am aware that recordings of my voice will be used by [company name] to create and use a synthetic version of my voice.
# You can find sample consent file here
# https://github.com/Azure-Samples/Cognitive-Speech-TTS/blob/master/CustomVoice/Sample%20Data/Individual%20utterances%20%2B%20matching%20script/VoiceTalentVerbalStatement.wav

consent_file_path = os.getenv('CONSENT_FILE_PATH')
voice_talent_name = os.getenv('VOICE_TALENT_NAME')
company_name = os.getenv('COMPANY_NAME')

config = customvoice.Config(speech_key, speech_region, logger)




# Need 5 - 90 seconds audio file.
# You can find sample audio file here.
# https://github.com/Azure-Samples/Cognitive-Speech-TTS/blob/master/CustomVoice/Sample%20Data/Individual%20utterances%20%2B%20matching%20script/SampleAudios.zip
audio_folder = r'data\\' # WAV files

try:
    speaker_profile_id = create_personal_voice(project_id, 
                                            consent_id, consent_file_path, voice_talent_name, company_name,
                                            personal_voice_id, audio_folder)

    print(f'speaker_profile_id: {speaker_profile_id}')

    # step 2: synthesis wave (just for testing)
    text = 'Esta es una voz personal. Prueba 1'
    output_wave_file_path = 'output_sdk.wav'
    speech_synthesis_to_wave_file(text, output_wave_file_path, speaker_profile_id)

except Exception as e:
    print(e)
