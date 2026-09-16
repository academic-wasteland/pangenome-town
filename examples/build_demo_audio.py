"""Rebuild bundled narration using Piper and a locally downloaded LJ Speech model.

uv run --with piper-tts python examples/build_demo_audio.py /path/en_US-ljspeech-medium.onnx
Requires ffmpeg on PATH. No participant data or credentials are sent to a service.
"""
import argparse
import json
import subprocess
import tempfile
import wave
from pathlib import Path

from piper import PiperVoice


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parents[1] / 'src/pangenome_town/demo_audio'
    voice = PiperVoice.load(str(args.model))
    notes = json.loads((folder / 'playbook.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        for group in notes.values():
            for note in group:
                wav = Path(tmp) / 'speech.wav'
                with wave.open(str(wav), 'wb') as output:
                    voice.synthesize_wav(note['text'], output)
                subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(wav),
                                '-codec:a', 'libmp3lame', '-b:a', '64k', str(folder / (note['id'] + '.mp3'))], check=True)
                print(note['id'], flush=True)


if __name__ == '__main__':
    main()
