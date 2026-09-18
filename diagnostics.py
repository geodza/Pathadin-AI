import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent/'.env')
from zsendo.slide import OPENSLIDE_ERROR
from zsendo.adapters import configured
print('Python:',sys.version)
print('OpenSlide:',OPENSLIDE_ERROR or 'loaded')
for provider,info in configured().items():
    print(provider+': key '+('configured' if info['configured'] else 'missing')+'; model IDs: '+', '.join(info['models']))
print('API key values are never printed.')
