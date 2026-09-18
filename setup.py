"""Install a private environment and restore bundled viewer assets when absent."""
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import venv

BASE = Path(__file__).resolve().parent
def main():
    target = BASE/'.venv'
    python = target/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.exists(): venv.create(target,with_pip=True)
    requirements = BASE/'requirements.lock.txt'
    if not requirements.exists(): requirements = BASE/'requirements.txt'
    subprocess.check_call([str(python),'-m','pip','install','-r',str(requirements)])
    vendor = BASE/'static'/'vendor'
    vendor.mkdir(parents=True,exist_ok=True)
    for name,url in [('openseadragon.min.js','https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/openseadragon.min.js'),
                     ('LICENSE-OpenSeadragon.txt','https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/LICENSE.txt')]:
        if not (vendor/name).exists(): (vendor/name).write_bytes(urllib.request.urlopen(url).read())
    print('Setup complete. Run run.bat or ./run.sh.')

if __name__ == '__main__': main()
