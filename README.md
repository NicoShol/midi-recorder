# Piano MIDI tool

Record your piano sessions to MIDI files, watch the notes you play live, and replay MIDI files as falling notes on an 88-key keyboard.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Connect your piano over USB or Bluetooth (it should appear in **Audio MIDI Setup → Window → Show MIDI Studio**), then:

```sh
uv sync
uv run piano.py ports   # check that your piano is listed
```

## Usage

```sh
uv run piano.py live                  # live view; press R to start/stop recording
uv run piano.py record                # live view, recording starts immediately
uv run piano.py record -o take.mid    # same, with a chosen file name
uv run piano.py record --no-gui       # record in the terminal only; Ctrl+C to stop
uv run piano.py play song.mid         # replay a MIDI file as falling notes
uv run piano.py play song.mid --synth # replay through the Mac's speakers
uv run piano.py play song.mid --mute  # replay without sound
uv run piano.py export song.mid       # save the falling-notes view as song.mp4
```

Recordings without `-o` go to `recordings/` as `YYYY-MM-DD_HH-MM-SS.mid`. Silence before the first note is trimmed. Quitting while recording still saves the take.

If several devices are connected, choose the input with `--port NAME` (a partial name is enough).

`play` sends the notes to your piano if it's connected, so the piano plays the file itself. Without a piano, it plays through the Mac's speakers using FluidSynth (see below). Use `--synth` to force the Mac's speakers, `--out NAME` to pick a MIDI output, or `--mute` for picture only.

## Exporting a video

`export` renders the falling-notes view to an MP4 (H.264 + AAC) with the piano sound from FluidSynth. It runs faster than real time, with no window, and needs `ffmpeg` (`brew install ffmpeg`).

```sh
uv run piano.py export song.mid                          # 1920x1080, 60 fps -> song.mp4
uv run piano.py export song.mid -o clip.mp4 --size 1080x1920 --fps 30   # vertical, for phones
uv run piano.py export song.mid --fit --title "My piece"
```

- `--fit` zooms the keyboard to the octaves the song uses (on by default for vertical videos; `--no-fit` to turn it off).
- `--title TEXT` sets the text in the top-left corner (default: file name; `--title ""` for none).
- `--speed N` sets the scroll speed in pixels per second; `--mute` makes a silent video.

## Sound without the piano (FluidSynth)

```sh
brew install fluid-synth
mkdir -p soundfonts
curl -L -o soundfonts/GeneralUser-GS.sf2 \
  https://raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/main/GeneralUser-GS.sf2
```

Any `.sf2` file in `soundfonts/` (or `~/Library/Audio/Sounds/Banks/`) works; the first one found is used. Pick a specific one with `--soundfont path/to/file.sf2`. The `soundfonts/` folder is git-ignored.

## Keys

| Key | Live / record | Play |
|---|---|---|
| R | Start/stop recording | — |
| Space | — | Pause/resume |
| Left / Right | — | Seek 5 s |
| Home | — | Restart |
| Click progress bar | — | Seek |
| Up / Down | Scroll speed | Scroll speed |
| Esc / Q | Quit | Quit |
