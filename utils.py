# ───────────────────────────────────────────────────────────────────────────────  
# Target languages
# key    -> ISO code of the target language for translation
# locale -> locale code used as a candidate for automatic detection  
# name   -> display name  
# ───────────────────────────────────────────────────────────────────────────────  
LANGUAGES: dict[str, tuple[str, str]] = {  
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