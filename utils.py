# ───────────────────────────────────────────────────────────────────────────────  
# Target languages
# key    -> ISO code of the target language for translation
# locale -> locale code used as a candidate for automatic detection  
# name   -> display name  
# NOTE: Azure Speech Translation in continuous mode supports MAX 10 languages
# ───────────────────────────────────────────────────────────────────────────────  
LANGUAGES: dict[str, tuple[str, str]] = {  
    "es": ("es-ES", "Spanish"),     
    "en": ("en-US", "English"),  
    "de": ("de-DE", "German"),  
    "it": ("it-IT", "Italian"),  
    "fr": ("fr-FR", "French"),      
    "nl": ("nl-NL", "Dutch"),       
    "sv": ("sv-SE", "Swedish"),  
    "da": ("da-DK", "Danish"),  
    "el": ("el-GR", "Greek"),       
    "pt": ("pt-PT", "Portuguese"),  
}

LANGUAGES_EU_COMMISION: dict[str, tuple[str, str]] = {  
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

# Get language name from long code
def get_language_name(long_code):
    #print(f"Looking for language name for code: {long_code}")
    for key, (code, name) in LANGUAGES.items():
        if code == long_code:
            return name
    return long_code