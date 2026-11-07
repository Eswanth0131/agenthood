"""Score, arrangement, performance and PCM rendering for the local pop studio."""
from array import array
import hashlib
import math
import random
import re
import sys
import wave

RATE = 22050
MAJOR = (0, 2, 4, 5, 7, 9, 11)
MINOR = (0, 2, 3, 5, 7, 8, 10)


def seeded(*parts):
    digest = hashlib.sha256('|'.join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], 'big'))


def has(text, pattern):
    return bool(re.search(r'\b(?:' + pattern + r')\b', text, re.I))


def compose(brief, title, profile, feedback='', revision=0, previous=None):
    """Write two contrasting four-bar phrases with a recurring chorus motif."""
    rnd = seeded(brief, title, profile, revision)
    direction = feedback.lower()
    tempo_match = re.search(r'\b(\d{2,3})\s*bpm\b', direction or brief.lower())
    fast = has(brief, 'upbeat|dance|dance-pop|energetic|punchy')
    midtempo = has(brief, 'midtempo') or (has(brief, 'syncopated r&b') and not fast)
    tempo = rnd.choice((88, 92, 96, 100) if midtempo else (116, 120, 124) if fast else (100, 104, 108, 112))
    register = 12 if has(direction, 'higher') else -12 if has(direction, 'lower') else 0
    tempo_change = has(direction, 'slower|faster')
    mode_change = has(direction, 'major|minor|dark|moody|melancholy')
    if previous and not mode_change and (register or tempo_change or tempo_match):
        bpm = int(tempo_match[1]) if tempo_match else previous['tempo']
        if tempo_change and not tempo_match:
            bpm += 12 if has(direction, 'faster') else -12
        lead = [(b, min(96, max(36, n + register)), d, v, ch) for b, n, d, v, ch in previous['lead']]
        return dict(previous, tempo=max(65, min(165, bpm)), lead=lead)
    if tempo_match:
        tempo = max(65, min(165, int(tempo_match[1])))
    elif has(direction, 'slower|faster'):
        tempo += 12 if has(direction, 'faster') else -12
    minor = not has(direction, 'major') and has(direction if mode_change else brief, 'minor|dark|moody|melancholy')
    scale = MINOR if minor else MAJOR
    root = rnd.choice((48, 50, 52, 53, 55, 57))
    progression = rnd.choice(((0, 4, 5, 3), (5, 3, 0, 4), (0, 3, 5, 4))) if not minor else (0, 5, 2, 6)
    motif = [rnd.choice((2, 4, 5, 7)), rnd.choice((4, 6, 7)), rnd.choice((3, 4, 5))]
    lead, chords, bass = [], [], []

    def pitch(degree):
        return scale[degree % 7] + degree // 7 * 12

    for bar in range(8):
        at, degree = bar * 4, progression[bar % 4]
        for offset in (0, 2, 4):
            chords.append((at, root + 12 + pitch(degree + offset), 3.85, 62 - offset, 1))
        for beat in (0, 2):
            bass.append((at + beat, root - 12 + pitch(degree), 1.7, 81, 2))
        if bar < 4:
            phrase = ((0, degree, .78), (1, degree + 2, .8), (2.5, degree + 1, 1.25))
        else:
            finish = 7 if bar == 7 else motif[2]
            phrase = ((0, motif[0], .67), (.75, motif[1], .63), (1.5, motif[0], .4), (2, finish, 1.8))
        for beat, note, length in phrase:
            lead.append((at + beat, root + 24 + register + pitch(note), length, 84 if bar < 4 else 100, 0))
    return dict(tempo=tempo, bars=8, lead=lead, chords=chords, bass=bass, root=root,
                mode='minor' if minor else 'major')


def arrange(score, brief, profile, feedback='', revision=0):
    """Build a new timeline from score phrases, with section-specific entrances and energy."""
    compact = has(feedback or brief, 'short|shorter|compact')
    sparse = has(feedback or brief, 'sparse|spacious|stripped|minimal')
    extended = not compact and (has(feedback or brief, 'big|extended|longer|anthem') or (profile + revision) % 2 == 1)
    sections = [('Intro', 0, 2, .46), ('Verse', 0, 4, .66),
                ('Build', 2, 2, .83), ('Chorus', 4, 4, 1.05)]
    if extended:
        sections.append(('Final chorus', 4, 4, 1.16))
    if not compact:
        sections.append(('Outro', 0, 2, .45))
    result = dict(score, bars=0, sections=[], lead=[], chords=[], bass=[])
    for name, source, bars, energy in sections:
        start = result['bars']
        result['sections'].append(dict(name=name, start=start, bars=bars, energy=energy))
        for track in ('lead', 'chords', 'bass'):
            if track == 'lead' and name in ('Intro', 'Outro') or track == 'bass' and name == 'Intro':
                continue
            for beat, note, length, velocity, channel in score[track]:
                if sparse and name == 'Verse' and track == 'chords' and velocity < 62:
                    continue
                if source * 4 <= beat < (source + bars) * 4:
                    shifted = beat + (start - source) * 4
                    result[track].append((shifted, note, length, min(127, int(velocity * energy)), channel))
                    if track == 'chords' and name == 'Final chorus' and not sparse:
                        result[track].append((shifted, note + 12, length, velocity // 3, channel))
        result['bars'] += bars
    result['sparse'] = sparse
    return result


def perform(plan, brief, profile, feedback='', revision=0):
    """Interpret the arrangement into distinct drums, bass and keys performances."""
    rnd = seeded(brief, profile, revision, 'performance')
    sparse = plan.get('sparse') or has(feedback, 'sparse|minimal|softer')
    punchy = has(feedback or brief, 'punchy|dance|dance-pop|heavier|four-on-the-floor')
    syncopated = has(feedback or brief, 'syncopated|dancehall')
    drums, bass, keys = [], [], []
    for section in plan.get('sections', [dict(start=0, bars=plan['bars'], energy=.85)]):
        energy = section['energy']
        for bar in range(section['start'], section['start'] + section['bars']):
            for beat in range(4):
                at = bar * 4 + beat
                if beat in (0, 2) or (energy >= .8 and punchy):
                    offset = .48 if beat == 2 and syncopated and energy < 1 else 0
                    drums.append((at + offset, 36, .28, int(96 * energy), 9))
                if beat in (1, 3) and energy >= .55:
                    drums.append((at + .017, 38, .19, int(82 * energy), 9))
                if energy >= .55:
                    drums.append((at + .48, 42, .09, rnd.randint(43, 60), 9))
                if energy >= 1 and not sparse:
                    drums.append((at + .74, 42, .09, 36, 9))
    for beat, note, length, velocity, channel in plan['bass']:
        bass.append((beat + (.22 if syncopated and beat % 4 else 0), note, length * .59, velocity, channel))
        if not sparse and length >= 1:
            bass.append((beat + 1.5, note + 12, .31, int(velocity * .69), channel))
    for beat, note, length, velocity, channel in plan['chords']:
        keys.append((beat + (note % 3) * .028, note, length * .87, velocity, channel))
    return dict(plan, drums=drums, bass=bass, chords=keys, timbre=profile % 4)


def guide(plan, profile, feedback='', revision=0):
    """Set the note timings and register for the lyric vocal."""
    register = 12 if has(feedback, 'higher') else -12 if has(feedback, 'lower') else 0
    legato = has(feedback, 'smoother|legato')
    notes = [(beat + .028, note + register, length * (1.06 if legato else .94), velocity, channel)
             for beat, note, length, velocity, channel in plan['lead']]
    return dict(plan, lead=notes)


def write_wav(dest, plan, tracks, profile=0):
    """Render note events to stereo PCM using enveloped synths and procedural drums."""
    seconds_per_beat = 60 / plan['tempo']
    frames = math.ceil((plan['bars'] * 4 * seconds_per_beat + .2) * RATE)
    left, right = array('f', [0]) * frames, array('f', [0]) * frames
    rng = seeded('drum noise')
    oscillator = math.sin
    tau = math.tau
    for track, voice, gain, pan in tracks:
        for beat, note, duration, velocity, _ in plan[track]:
            start = int(beat * seconds_per_beat * RATE)
            size = min(frames - start, max(1, int(duration * seconds_per_beat * RATE)))
            freq = 440 * 2 ** ((note - 69) / 12)
            vol = velocity / 127 * gain
            for i in range(size):
                t = i / RATE
                phase = tau * freq * t
                if voice == 'drums':
                    if note == 36:
                        sound = oscillator(tau * (45 * t + 1.2 * (1 - math.exp(-t * 42)))) * math.exp(-t * 23)
                    else:
                        sound = rng.uniform(-1, 1) * math.exp(-t * (45 if note == 38 else 95))
                        if note == 38:
                            sound += .35 * oscillator(tau * 175 * t) * math.exp(-t * 44)
                    envelope = min(1, t * 650, (size - i) / 100)
                else:
                    envelope = min(1, t * 80, (size - i) / (RATE * .055))
                    if voice == 'keys':
                        color = (profile % 4) * .07
                        sound = .71 * oscillator(phase) + (.19 + color) * oscillator(phase * 2) + .10 * oscillator(phase * 3)
                        envelope *= .60 + .40 * math.exp(-t * 3)
                    else:
                        sound = .82 * oscillator(phase) + .20 * oscillator(phase * 2)
                sample = sound * envelope * vol
                left[start + i] += sample * (1 - max(0, pan))
                right[start + i] += sample * (1 + min(0, pan))
    write_pcm(dest, left, right)
    return frames / RATE


def write_pcm(dest, left, right, normalize=False):
    peak = max((max(abs(a), abs(b)) for a, b in zip(left, right)), default=0)
    gain = .86 / peak if normalize and peak else min(1, .95 / max(peak, .01))
    pcm = array('h')
    for a, b in zip(left, right):
        pcm.extend((int(a * gain * 32767), int(b * gain * 32767)))
    if sys.byteorder == 'big':
        pcm.byteswap()
    with wave.open(str(dest), 'wb') as w:
        w.setparams((2, 2, RATE, 0, 'NONE', 'not compressed'))
        w.writeframes(pcm.tobytes())


def mix_wavs(dest, inputs, master=False, width=1.0):
    """Mix delivered WAVs with per-stem gains and optional soft peak control."""
    left, right = array('f'), array('f')
    for source, gain in inputs:
        with wave.open(str(source), 'rb') as w:
            pcm = array('h', w.readframes(w.getnframes()))
        if sys.byteorder == 'big':
            pcm.byteswap()
        frames = len(pcm) // 2
        if frames > len(left):
            padding = array('f', [0]) * (frames - len(left))
            left.extend(padding)
            right.extend(padding)
        for i in range(frames):
            left[i] += pcm[2 * i] * gain / 32768
            right[i] += pcm[2 * i + 1] * gain / 32768
    if master:
        for i, (a, b) in enumerate(zip(left, right)):
            center = (a + b) * .5
            side = (a - b) * .5 * width
            fade = min(1, (i + 1) / (RATE * .04), (len(left) - i) / (RATE * .24))
            left[i] = math.tanh((center + side) * 1.6) * fade
            right[i] = math.tanh((center - side) * 1.6) * fade
    write_pcm(dest, left, right, normalize=master)
    return len(left) / RATE
