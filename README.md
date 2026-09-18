# Pathadin AI

Local whole-slide pathology research tool for comparing vision-language models, inspecting evidence and running folder studies.

**Research prototype. Model outputs require qualified pathologist review. Not clinically validated.**

## Windows quick start

1. Download the Windows ZIP from Releases and extract it completely.
2. Install Python 3.11 or newer if needed.
3. Double-click **SET API KEY.bat** to enter your own provider key locally.
4. Double-click **START PATHADIN.bat**. First-time setup requires internet access.
5. Keep the terminal open. The interface opens at http://127.0.0.1:8765.

This is a Python application package, not a standalone executable. The synthetic demo does not require paid API calls.

## Guides

- [Beginner guide](START%20HERE.txt)
- [Folder analysis](FOLDER%20ANALYSIS%20-%20START%20HERE.txt)
- Download the HTML guides with the application to read them in a browser.

## Features

- OpenSlide-supported whole-slide images and ordinary JPEG/PNG images.
- Editable tissue masks, calibrated architectural fields, native center detail and exact input previews.
- Model comparison with saved reports and traceable GeoJSON field annotations.
- Reviewed regions and measurements, with explicit calibration requirements.
- Persistent folder queues, slide selection, request caps, pause/resume and CSV results.

GeoJSON evidence rectangles represent cited fields, not precise lesion segmentation. Physical measurements describe reviewed drawings. Automatic tissue masks may miss tissue.

## Privacy and costs

Opening and preparing images is local. Analysis sends slide-derived images and organ information to selected providers. Each user supplies their own API key and pays provider charges. Never commit `.env`, the `data` directory, patient slides or saved runs. Share clean release archives rather than working installations.

## Development

Install the dependencies in `requirements.lock.txt`, then run:

```sh
python -m unittest discover -s tests -q
python launch.py
```

See [verification notes](VERIFICATION.md) for checks and limitations. Queue provider tests use simulated responses, not paid requests.

## Third-party components

Bundled OpenSeadragon includes its license in `static/vendor`. Other dependencies retain their respective licenses. No project-wide open-source license has been selected yet.
