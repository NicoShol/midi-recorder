"""Piano MIDI tool: record sessions, watch notes live, and replay MIDI files.

    uv run piano.py live                    # live view; press R to start/stop recording
    uv run piano.py record [-o take.mid]    # live view, recording starts immediately
    uv run piano.py record --no-gui         # terminal-only recording, Ctrl+C to stop
    uv run piano.py play song.mid           # falling-notes playback of a MIDI file
    uv run piano.py export song.mid         # save that view as song.mp4 (with sound)
    uv run piano.py ports                   # list MIDI inputs/outputs

Options: --port NAME picks an input by (partial) name. play sends sound to the piano if
connected, otherwise to the Mac's speakers via FluidSynth; --out NAME / --synth / --mute.
"""

from __future__ import annotations

import argparse
import bisect
import os
import queue
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import mido

LOW, HIGH = 21, 108  # 88-key range, A0..C8
BLACK_PCS = {1, 3, 6, 8, 10}
RECORD_TYPES = {"note_on", "note_off", "control_change", "pitchwheel",
                "program_change", "aftertouch", "polytouch"}
TICKS_PER_BEAT = 480
TEMPO = 500_000  # 120 bpm; only used to convert seconds to ticks
PALETTE = [(90, 170, 255), (255, 150, 80), (120, 220, 130), (230, 120, 230),
           (250, 220, 90), (100, 220, 220)]
BG = (22, 24, 30)
SPEEDS = [60, 90, 130, 180, 250, 350, 500]  # pixels per second


# ---------------------------------------------------------------- MIDI helpers

def is_note_on(msg) -> bool:
    return msg.type == "note_on" and msg.velocity > 0


def is_note_off(msg) -> bool:
    return msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)


def pick_port(names: list[str], wanted: str | None, kind: str) -> str:
    if not names:
        sys.exit(f"No MIDI {kind} ports found. Connect/turn on your piano and check "
                 "Audio MIDI Setup → Window → Show MIDI Studio.")
    if wanted is None:
        return names[0]
    for name in names:
        if wanted.lower() in name.lower():
            return name
    sys.exit(f"No MIDI {kind} port matching {wanted!r}. Available: {names}")


def open_input(name: str, q: queue.Queue):
    # The callback runs on rtmidi's thread; timestamp there for accurate timing.
    return mido.open_input(name, callback=lambda msg: q.put((time.perf_counter(), msg)))


SOUNDFONT_DIRS = [Path(__file__).resolve().parent / "soundfonts",
                  Path.home() / "Library/Audio/Sounds/Banks"]


class SynthOutput:
    """Plays MIDI messages through the Mac's speakers with FluidSynth + a SoundFont."""

    def __init__(self, soundfont: str | None, realtime: bool = True, samplerate: int = 44100):
        sf2 = Path(soundfont) if soundfont else next(
            (p for d in SOUNDFONT_DIRS if d.is_dir() for p in sorted(d.glob("*.sf2"))), None)
        if sf2 is None:
            raise RuntimeError("no SoundFont (.sf2) found in soundfonts/")
        if not sf2.exists():
            raise RuntimeError(f"SoundFont not found: {sf2}")
        # pyfluidsynth only finds Homebrew's libfluidsynth through HOMEBREW_PREFIX.
        if Path("/opt/homebrew/lib").is_dir():
            os.environ.setdefault("HOMEBREW_PREFIX", "/opt/homebrew")
        try:
            import fluidsynth
        except ImportError as e:
            raise RuntimeError("FluidSynth not installed (brew install fluid-synth)") from e

        self.fs = fluidsynth.Synth(gain=0.6, samplerate=float(samplerate))
        if realtime:
            self.fs.start(driver="coreaudio")  # otherwise render with get_samples()
        sfid = self.fs.sfload(str(sf2))
        if sfid < 0:
            self.fs.delete()
            raise RuntimeError(f"could not load {sf2}")
        for ch in range(16):
            if ch != 9:  # channel 10 is drums
                self.fs.program_select(ch, sfid, 0, 0)  # bank 0, program 0: grand piano
        self.name = f"Mac speakers ({sf2.name})"

    def send(self, msg) -> None:
        if is_note_on(msg):
            self.fs.noteon(msg.channel, msg.note, msg.velocity)
        elif is_note_off(msg):
            self.fs.noteoff(msg.channel, msg.note)
        elif msg.type == "control_change":
            self.fs.cc(msg.channel, msg.control, msg.value)

    def close(self) -> None:
        self.fs.delete()


def open_playback_output(args):
    """Returns (output or None, status text). Prefers the piano, falls back to the Mac."""
    if args.mute:
        return None, "off (--mute)"
    if args.out:
        return mido.open_output(pick_port(mido.get_output_names(), args.out, "output")), None
    if mido.get_output_names() and not args.synth:
        return mido.open_output(mido.get_output_names()[0]), None
    try:
        return SynthOutput(args.soundfont), None
    except RuntimeError as e:
        print(f"No sound: {e}")
        return None, f"off ({e})"


class Recorder:
    def __init__(self):
        self.start: float | None = None
        self.events: list[tuple[float, mido.Message]] = []
        self.held: set[tuple[int, int]] = set()

    @property
    def active(self) -> bool:
        return self.start is not None

    def begin(self) -> None:
        self.start, self.events, self.held = time.perf_counter(), [], set()

    def add(self, t: float, msg) -> None:
        if not self.active or msg.type not in RECORD_TYPES:
            return
        self.events.append((t, msg))
        if is_note_on(msg):
            self.held.add((msg.channel, msg.note))
        elif is_note_off(msg):
            self.held.discard((msg.channel, msg.note))

    def stop(self, path: Path) -> Path | None:
        """Stop recording and write a MIDI file. Returns None if nothing was played."""
        events, held = self.events, self.held
        self.start, self.events, self.held = None, [], set()
        if not any(is_note_on(m) for _, m in events):
            return None
        end = time.perf_counter()
        events += [(end, mido.Message("note_off", channel=ch, note=n)) for ch, n in held]

        track = mido.MidiTrack()
        track.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
        t0, last_tick = events[0][0], 0  # trim the silence before the first event
        for t, msg in events:
            tick = round(mido.second2tick(t - t0, TICKS_PER_BEAT, TEMPO))
            track.append(msg.copy(time=tick - last_tick))
            last_tick = tick
        track.append(mido.MetaMessage("end_of_track", time=0))

        mid = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
        mid.tracks.append(track)
        path.parent.mkdir(parents=True, exist_ok=True)
        mid.save(path)
        return path


class OutputPaths:
    """First save goes to -o if given, the rest to timestamped files in --dir."""

    def __init__(self, output: str | None, directory: str):
        self.output = Path(output) if output else None
        self.directory = Path(directory)

    def next(self) -> Path:
        if self.output:
            path, self.output = self.output, None
            return path
        return self.directory / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.mid"


# ---------------------------------------------------------------- drawing

@dataclass
class Note:
    pitch: int
    start: float
    end: float | None
    velocity: int
    color: tuple[int, int, int]


def shade(color, factor: float):
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def note_color(base, velocity: int, pitch: int):
    c = shade(base, 0.45 + 0.55 * velocity / 127)
    return shade(c, 0.75) if pitch % 12 in BLACK_PCS else c


class Keyboard:
    def __init__(self, width: int, height: int, low: int = LOW, high: int = HIGH):
        import pygame

        self.height = max(80, min(height // 5, width // 6))
        self.top = height - self.height
        whites = [p for p in range(low, high + 1) if p % 12 not in BLACK_PCS]
        ww = width / len(whites)
        self.rects: dict[int, pygame.Rect] = {}
        x = 0.0
        for p in range(low, high + 1):
            if p % 12 in BLACK_PCS:
                bw = ww * 0.6
                self.rects[p] = pygame.Rect(round(x - bw / 2), self.top,
                                            round(bw), round(self.height * 0.62))
            else:
                self.rects[p] = pygame.Rect(round(x), self.top,
                                            round(x + ww) - round(x), self.height)
                x += ww

    def bar(self, screen, pitch: int, y_top: float, y_bot: float, color) -> None:
        import pygame

        if pitch not in self.rects or y_bot < 0 or y_top > self.top:
            return
        r = self.rects[pitch]
        x, w = (r.x, r.w) if pitch % 12 in BLACK_PCS else (r.x + 2, r.w - 4)
        y_top, y_bot = max(y_top, -6), min(y_bot, self.top)
        pygame.draw.rect(screen, color, (x, int(y_top), w, max(3, int(y_bot - y_top))),
                         border_radius=4)
        pygame.draw.rect(screen, shade(color, 0.6), (x, int(y_top), w,
                         max(3, int(y_bot - y_top))), width=1, border_radius=4)

    def draw(self, screen, font, lit: dict[int, tuple]) -> None:
        import pygame

        pygame.draw.rect(screen, (200, 40, 40), (0, self.top - 3, screen.get_width(), 3))
        for p, r in self.rects.items():
            if p % 12 not in BLACK_PCS:
                pygame.draw.rect(screen, lit.get(p, (245, 245, 240)), r)
                pygame.draw.rect(screen, (60, 60, 60), r, width=1)
                if p % 12 == 0:
                    label = font.render(f"C{p // 12 - 1}", True, (110, 110, 110))
                    screen.blit(label, label.get_rect(midbottom=(r.centerx, r.bottom - 4)))
        for p, r in self.rects.items():
            if p % 12 in BLACK_PCS:
                pygame.draw.rect(screen, lit.get(p, (25, 25, 25)), r,
                                 border_bottom_left_radius=3, border_bottom_right_radius=3)


class Window:
    def __init__(self, title: str):
        import pygame

        pygame.init()
        pygame.display.set_caption(title)
        self.screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
        self.font = pygame.font.Font(None, 22)
        self.big = pygame.font.Font(None, 30)
        self.clock = pygame.time.Clock()
        self.kb = Keyboard(*self.screen.get_size())
        self.speed_i = 3

    @property
    def speed(self) -> int:
        return SPEEDS[self.speed_i]

    def handle_common(self, ev) -> bool:
        """Handle resize/zoom/quit. Returns False when the window should close."""
        import pygame

        if ev.type == pygame.QUIT:
            return False
        if ev.type == pygame.WINDOWRESIZED:
            self.kb = Keyboard(*self.screen.get_size())
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                return False
            if ev.key == pygame.K_UP:
                self.speed_i = max(0, self.speed_i - 1)
            if ev.key == pygame.K_DOWN:
                self.speed_i = min(len(SPEEDS) - 1, self.speed_i + 1)
        return True

    def text(self, s: str, pos, color=(200, 200, 200), big=False) -> None:
        self.screen.blit((self.big if big else self.font).render(s, True, color), pos)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------- live / record

def run_live(args, record_now: bool) -> None:
    import pygame

    in_name = pick_port(mido.get_input_names(), args.port, "input")
    q: queue.Queue = queue.Queue()
    rec, paths = Recorder(), OutputPaths(args.output, args.dir)
    active: dict[int, Note] = {}
    finished: list[Note] = []
    pedal = False
    flash, flash_until = "", 0.0

    def save():
        nonlocal flash, flash_until
        path = rec.stop(paths.next())
        flash = f"Saved {path}" if path else "Nothing played — no file saved"
        flash_until = time.perf_counter() + 4
        print(flash)

    win = Window(f"Piano — {in_name}")
    if record_now:
        rec.begin()
    try:
        with open_input(in_name, q):
            running = True
            while running:
                for ev in pygame.event.get():
                    running = win.handle_common(ev) and running
                    if ev.type == pygame.KEYDOWN and ev.key == pygame.K_r:
                        save() if rec.active else rec.begin()

                while True:
                    try:
                        t, msg = q.get_nowait()
                    except queue.Empty:
                        break
                    rec.add(t, msg)
                    if is_note_on(msg):
                        if (old := active.pop(msg.note, None)):
                            old.end = t
                            finished.append(old)
                        active[msg.note] = Note(msg.note, t, None, msg.velocity,
                                                note_color(PALETTE[0], msg.velocity, msg.note))
                    elif is_note_off(msg):
                        if (n := active.pop(msg.note, None)):
                            n.end = t
                            finished.append(n)
                    elif msg.type == "control_change" and msg.control == 64:
                        pedal = msg.value >= 64

                now, kb, screen = time.perf_counter(), win.kb, win.screen
                # Notes rise from the keyboard as time passes.
                finished[:] = [n for n in finished if kb.top - (now - n.end) * win.speed > 0]
                screen.fill(BG)
                for n in finished + list(active.values()):
                    kb.bar(screen, n.pitch, kb.top - (now - n.start) * win.speed,
                           kb.top - (now - (n.end or now)) * win.speed, n.color)
                kb.draw(screen, win.font, {p: n.color for p, n in active.items()})

                win.text(in_name, (12, 10))
                if pedal:
                    win.text("PEDAL", (12, 32), (250, 200, 90))
                if rec.active:
                    if int(now * 2) % 2:
                        pygame.draw.circle(screen, (230, 50, 50), (screen.get_width() - 120, 20), 8)
                    win.text(f"REC {fmt_time(now - rec.start)}",
                             (screen.get_width() - 105, 11), (230, 80, 80), big=True)
                if now < flash_until:
                    win.text(flash, (12, 56), (140, 220, 140))
                win.text("R record/stop · Up/Down scroll speed · Esc quit", (12, 80), (110, 110, 120))
                pygame.display.flip()
                win.clock.tick(60)
    except KeyboardInterrupt:
        pass
    finally:
        if rec.active:
            save()
        pygame.quit()


def run_record_headless(args) -> None:
    in_name = pick_port(mido.get_input_names(), args.port, "input")
    q: queue.Queue = queue.Queue()
    rec, count = Recorder(), 0
    print(f"Recording from {in_name} — press Ctrl+C to stop.")
    rec.begin()
    try:
        with open_input(in_name, q):
            while True:
                try:
                    t, msg = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                rec.add(t, msg)
                if is_note_on(msg):
                    count += 1
                    print(f"\r{count} notes, {fmt_time(t - rec.start)}", end="", flush=True)
    except KeyboardInterrupt:
        print()
    path = rec.stop(OutputPaths(args.output, args.dir).next())
    print(f"Saved {path}" if path else "Nothing played — no file saved")


# ---------------------------------------------------------------- playback

class Song:
    """A MIDI file's notes (for drawing) and events (for sound), times in seconds."""

    def __init__(self, path: Path):
        if not path.exists():
            sys.exit(f"File not found: {path}")
        self.path = path
        self.notes, self.events = load_midi(path)
        if not self.notes:
            sys.exit(f"No notes in {path}")
        self.starts = [n.start for n in self.notes]
        self.max_dur = max(n.end - n.start for n in self.notes)
        self.total = max(n.end for n in self.notes)
        self.event_times = [t for t, _ in self.events]

    def key_range(self) -> tuple[int, int]:
        """Whole octaves (C..B) covering the song's notes, at least two octaves."""
        low = min(n.pitch for n in self.notes)
        high = max(n.pitch for n in self.notes)
        low, high = low - low % 12, high + 11 - high % 12
        if high - low < 23:  # one octave only: add the octave above (or below at the top)
            if high + 12 <= HIGH + 11:
                high += 12
            else:
                low -= 12
        return max(LOW, low), min(HIGH, high)

    def draw(self, screen, kb: Keyboard, font, pos: float, speed: float) -> None:
        """Falling notes that hit the keyboard at their start time, plus a progress bar."""
        import pygame

        screen.fill(BG)
        lit = {}
        lo = bisect.bisect_left(self.starts, pos - self.max_dur)
        hi = bisect.bisect_right(self.starts, pos + kb.top / speed)
        for n in self.notes[lo:hi]:
            if n.end < pos:
                continue
            kb.bar(screen, n.pitch, kb.top - (n.end - pos) * speed,
                   kb.top - (n.start - pos) * speed, n.color)
            if n.start <= pos:
                lit[n.pitch] = n.color
        kb.draw(screen, font, lit)

        w, bar_h = screen.get_width(), max(6, screen.get_height() // 120)
        pygame.draw.rect(screen, (50, 52, 60), (0, 0, w, bar_h))
        pygame.draw.rect(screen, PALETTE[0], (0, 0, int(w * max(0, pos) / self.total), bar_h))


def load_midi(path: Path) -> tuple[list[Note], list[tuple[float, mido.Message]]]:
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(path))
    notes: list[Note] = []
    events: list[tuple[float, int, mido.Message]] = []
    for i, inst in enumerate(pm.instruments):
        if inst.is_drum:
            continue
        base = PALETTE[i % len(PALETTE)]
        for n in inst.notes:
            notes.append(Note(n.pitch, n.start, n.end, n.velocity,
                              note_color(base, n.velocity, n.pitch)))
            events.append((n.start, 1, mido.Message("note_on", note=n.pitch, velocity=n.velocity)))
            events.append((n.end, 0, mido.Message("note_off", note=n.pitch)))
        for cc in inst.control_changes:
            if cc.number == 64:
                events.append((cc.time, 0, mido.Message("control_change", control=64,
                                                        value=cc.value)))
    notes.sort(key=lambda n: n.start)
    events.sort(key=lambda e: (e[0], e[1]))  # note_offs before note_ons at the same time
    return notes, [(t, m) for t, _, m in events]


def run_play(args) -> None:
    import pygame

    song = Song(Path(args.file))
    path, events, event_times, total = song.path, song.events, song.event_times, song.total

    out, sound_status = open_playback_output(args)

    def silence():
        if out:
            out.send(mido.Message("control_change", control=64, value=0))
            out.send(mido.Message("control_change", control=123, value=0))

    lead_in = 2.0
    origin = time.perf_counter() + lead_in  # file time = perf_counter() - origin
    paused_at: float | None = None
    ev_i = 0

    def position() -> float:
        return paused_at if paused_at is not None else time.perf_counter() - origin

    def seek(to: float) -> None:
        nonlocal origin, paused_at, ev_i
        to = max(-lead_in, min(total, to))
        silence()
        if paused_at is not None:
            paused_at = to
        origin = time.perf_counter() - to
        ev_i = bisect.bisect_left(event_times, to)

    win = Window(f"Piano — {path.name}")
    try:
        running = True
        while running:
            for ev in pygame.event.get():
                running = win.handle_common(ev) and running
                if ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_SPACE:
                        if paused_at is None:
                            paused_at = position()
                            silence()
                        else:
                            origin, paused_at = time.perf_counter() - paused_at, None
                            ev_i = bisect.bisect_left(event_times, position())
                    elif ev.key == pygame.K_LEFT:
                        seek(position() - 5)
                    elif ev.key == pygame.K_RIGHT:
                        seek(position() + 5)
                    elif ev.key == pygame.K_HOME:
                        seek(-lead_in)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and ev.pos[1] < 14:
                    seek(ev.pos[0] / win.screen.get_width() * total)

            pos = position()
            if paused_at is None and pos > total + 0.5:
                paused_at = total
                silence()
            if paused_at is None:
                while ev_i < len(events) and events[ev_i][0] <= pos:
                    if out:
                        out.send(events[ev_i][1])
                    ev_i += 1

            screen, w = win.screen, win.screen.get_width()
            song.draw(screen, win.kb, win.font, pos, win.speed)
            win.text(f"{path.name}   {fmt_time(pos)} / {fmt_time(total)}", (12, 14))
            win.text(f"Sound: {out.name if out else sound_status}", (12, 36),
                     (140, 140, 150))
            if paused_at is not None:
                win.text("PAUSED", (w - 110, 14), (250, 200, 90), big=True)
            win.text("Space pause · Left/Right seek 5s · Home restart · click bar to seek · "
                     "Up/Down scroll speed · Esc quit", (12, 58), (110, 110, 120))
            pygame.display.flip()
            win.clock.tick(60)
    except KeyboardInterrupt:
        pass
    finally:
        silence()
        if out:
            out.close()
        pygame.quit()


# ---------------------------------------------------------------- video export

def render_audio(song: Song, synth: SynthOutput, t0: float, t1: float, sr: int, wav: Path):
    """Render the song offline from time t0 to t1 into a stereo 16-bit WAV."""
    import wave

    import numpy as np

    chunks, done = [], 0
    for t, msg in song.events:
        target = round((t - t0) * sr)
        if target > done:
            chunks.append(synth.fs.get_samples(target - done))
            done = target
        synth.send(msg)
    end = round((t1 - t0) * sr)
    if end > done:
        chunks.append(synth.fs.get_samples(end - done))
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(np.concatenate(chunks).astype(np.int16).tobytes())


def run_export(args) -> None:
    import math
    import shutil
    import subprocess
    import tempfile

    import pygame

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found (brew install ffmpeg)")
    song = Song(Path(args.file))
    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        sys.exit(f"Bad --size {args.size!r}, expected e.g. 1920x1080")
    out_path = Path(args.output) if args.output else song.path.with_suffix(".mp4")
    lead_in, tail, sr = 1.5, 2.0, 44100
    t0, t1 = -lead_in, song.total + tail
    n_frames = math.ceil((t1 - t0) * args.fps)

    with tempfile.TemporaryDirectory() as tmp:
        wav = None
        if not args.mute:
            try:
                synth = SynthOutput(args.soundfont, realtime=False, samplerate=sr)
            except RuntimeError as e:
                print(f"No sound in the video: {e}")
            else:
                print("Rendering audio…")
                wav = Path(tmp) / "audio.wav"
                render_audio(song, synth, t0, t1, sr, wav)
                synth.close()

        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", str(args.fps), "-i", "-"]
        if wav:
            cmd += ["-i", str(wav), "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(out_path)]
        ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE)

        pygame.font.init()
        scale = h / 720 if w >= h else w / 720
        font = pygame.font.Font(None, round(22 * scale))
        title_font = pygame.font.Font(None, round(30 * scale))
        fit = args.fit if args.fit is not None else h > w  # vertical videos zoom in
        screen = pygame.Surface((w, h))
        kb = Keyboard(w, h, *song.key_range()) if fit else Keyboard(w, h)
        speed = args.speed or 180 * scale
        title = args.title if args.title is not None else song.path.stem
        try:
            for i in range(n_frames):
                pos = t0 + i / args.fps
                song.draw(screen, kb, font, pos, speed)
                if title:
                    screen.blit(title_font.render(title, True, (220, 220, 220)),
                                (round(16 * scale), round(18 * scale)))
                ffmpeg.stdin.write(pygame.image.tobytes(screen, "RGB"))
                if i % args.fps == 0 or i == n_frames - 1:
                    print(f"\rRendering video… {100 * (i + 1) // n_frames}%", end="", flush=True)
            print()
        except (BrokenPipeError, KeyboardInterrupt):
            print("\nExport stopped.")
        finally:
            ffmpeg.stdin.close()
            ok = ffmpeg.wait() == 0
    if ok and i == n_frames - 1:
        print(f"Saved {out_path} ({fmt_time(t1 - t0)}, {w}x{h}, {args.fps} fps)")
    else:
        sys.exit("Export failed.")


# ---------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="Record, watch and replay piano MIDI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_ in [("live", "live view; press R to start/stop recording"),
                        ("record", "record a session (starts immediately)")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--port", help="input port name (partial match); default: first")
        p.add_argument("-o", "--output", help="file for the first recording")
        p.add_argument("--dir", default="recordings", help="folder for timestamped recordings")
        if name == "record":
            p.add_argument("--no-gui", action="store_true", help="record in the terminal only")

    p = sub.add_parser("play", help="show a MIDI file as falling notes")
    p.add_argument("file")
    p.add_argument("--out", help="output port for sound (partial match); default: first")
    p.add_argument("--mute", action="store_true", help="no sound")
    p.add_argument("--synth", action="store_true",
                   help="play through the Mac's speakers even if the piano is connected")
    p.add_argument("--soundfont", help="SoundFont (.sf2) for --synth; default: soundfonts/*.sf2")

    p = sub.add_parser("export", help="save the falling-notes view of a MIDI file as a video")
    p.add_argument("file")
    p.add_argument("-o", "--output", help="video file (default: same name as the MIDI, .mp4)")
    p.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT, e.g. 1080x1920 for vertical")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--speed", type=float, help="scroll speed in pixels/second")
    p.add_argument("--title", help="text shown top-left (default: file name; '' for none)")
    p.add_argument("--fit", action=argparse.BooleanOptionalAction, default=None,
                   help="zoom the keyboard to the octaves used (default: on for vertical)")
    p.add_argument("--mute", action="store_true", help="video without sound")
    p.add_argument("--soundfont", help="SoundFont (.sf2); default: soundfonts/*.sf2")

    sub.add_parser("ports", help="list MIDI ports")

    args = parser.parse_args()
    if args.cmd == "ports":
        print("Inputs: ", mido.get_input_names() or "none")
        print("Outputs:", mido.get_output_names() or "none")
    elif args.cmd == "live":
        run_live(args, record_now=False)
    elif args.cmd == "record":
        run_record_headless(args) if args.no_gui else run_live(args, record_now=True)
    elif args.cmd == "play":
        run_play(args)
    elif args.cmd == "export":
        run_export(args)


if __name__ == "__main__":
    main()
