# Bundled demo narration

`playbook.json` is the source of the visible presenter notes and the MP3 scripts.
The clips are synthesized speech, generated locally with Piper using the
[LJ Speech medium voice](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/ljspeech/medium).
Its [model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/ljspeech/medium/MODEL_CARD)
identifies the training dataset as public domain. No town data, credentials or
participant information were used to synthesize the clips. The model is not
bundled or required for playback. Playback requires no network speech service.

Download `en_US-ljspeech-medium.onnx` and its matching `.onnx.json` from the
linked model repository. Rebuild using:

```sh
uv run --with piper-tts python examples/build_demo_audio.py /path/to/en_US-ljspeech-medium.onnx
```

The build requires ffmpeg. All generated clips are encoded as mono MP3 at
64 kb/s. The website serves only manifest-listed MP3 files and `playbook.json`.
