"""Render lyrics with macOS speech and a MIDI-controlled channel vocoder."""
from array import array
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import wave

from studio_audio import RATE, write_pcm

SAY = Path('/usr/bin/say')
VOWEL_BANDS = (260, 400, 560, 780, 1060, 1450, 1980, 2700, 3700, 5100, 6900)


class SpeechUnavailable(ValueError):
    """The host speech engine could not produce usable audio."""


def lyric_sections(text, title):
    sections = {'verse': [], 'build': [], 'chorus': [], 'bridge': []}
    section = 'verse'
    for line in text.splitlines():
        line = line.strip().strip('*# ').strip()
        if not line or line.casefold() == title.casefold():
            continue
        if re.match(r'^\[?(?:customer brief|revision note)\b', line, re.I):
            break
        if re.match(r'^original .+demo\b', line, re.I):
            continue
        heading = re.fullmatch(r'\[?(verse|pre[ -]?chorus|build|(?:final )?chorus|hook|bridge|intro|outro)\s*\d*\]?:?', line, re.I)
        if heading:
            name = re.sub(r'[ -]', '', heading[1].lower())
            section = {'prechorus': 'build', 'finalchorus': 'chorus', 'hook': 'chorus'}.get(name, name)
        elif section in sections:
            line = re.sub(r'\[.*?\]|\{.*?\}|[\x00-\x1f]', '', line).strip()
            if re.search(r'\w', line):
                sections[section].append(line[:180])
    if not any(sections.values()):
        raise ValueError('The approved lyric sheet has no words to perform.')
    if not sections['chorus']:
        sections['chorus'] = sections['verse'][-4:] or sections['build'] or sections['bridge']
    return sections


def schedule(plan, lyrics):
    sections = plan.get('sections') or [
        dict(name='Verse', start=0, bars=min(4, plan['bars'])),
        dict(name='Chorus', start=min(4, plan['bars']), bars=max(0, plan['bars']-4)),
    ]
    for section in sections:
        name = section['name'].lower()
        kind = 'chorus' if 'chorus' in name else name
        lines = lyrics.get(kind)
        if not lines or not section['bars']:
            continue
        count = section['bars']
        # Four lines over four bars is typical. Longer drafts use the first section-length take.
        lines = lines[:count]
        span = count * 4 / len(lines)
        for index, line in enumerate(lines):
            yield section['start'] * 4 + index * span, span, line


def speech_samples(text, seconds, voice, output):
    # Remove speech markup and option-like prefixes from untrusted lyric text.
    text = re.sub(r'^[-\s]+', '', text.replace('[', '').replace(']', ''))
    words = len(text.split())
    pace = max(135, min(330, round(words * 66 / seconds)))
    command = [str(SAY), '--file-format=WAVE', f'--data-format=LEI16@{RATE}',
               '-r', str(pace), '-o', str(output)]
    if voice:
        command.extend(('-v', voice))
    try:
        subprocess.run([*command, '--', text], check=True, capture_output=True, timeout=15)
        with wave.open(str(output), 'rb') as track:
            if (track.getframerate(), track.getsampwidth(), track.getnchannels()) != (RATE, 2, 1):
                raise SpeechUnavailable('Unexpected macOS speech audio format.')
            samples = array('h', track.readframes(track.getnframes()))
    except (OSError, subprocess.SubprocessError, wave.Error, EOFError) as exc:
        raise SpeechUnavailable('macOS could not render the lyrics. Check the installed System Voice and AGENTHOOD_VOICE, then retry.') from exc
    active = [i for i, sample in enumerate(samples) if abs(sample) > 190]
    if not active:
        raise SpeechUnavailable('macOS returned silent speech. Choose an installed System Voice and retry.')
    samples = samples[max(0, active[0]-120):active[-1]+400]
    maximum = int(seconds * RATE)
    if len(samples) > maximum:
        ratio = len(samples) / maximum
        return array('f', (samples[int(i * ratio)] / 32768 for i in range(maximum)))
    return array('f', (sample / 32768 for sample in samples))


def bandpass(signal, freq):
    angle = math.tau * freq / RATE
    alpha = math.sin(angle) / 2.5
    b = alpha / (1 + alpha)
    a1 = -2 * math.cos(angle) / (1 + alpha)
    a2 = (1 - alpha) / (1 + alpha)
    x1 = x2 = y1 = y2 = 0.0
    result = array('f')
    for x in signal:
        y = b * (x - x2) - a1 * y1 - a2 * y2
        result.append(y)
        x2, x1, y2, y1 = x1, x, y1, y
    return result


def carrier_for(notes, start, count, spb, smooth):
    carrier = array('f')
    index = 0
    phase = 0.0
    frequency = None
    pitches = [(round(beat * spb * RATE), 440 * 2**((note - 69)/12)) for beat, note, *_ in notes]
    for i in range(count):
        while index + 1 < len(pitches) and pitches[index + 1][0] <= start + i:
            index += 1
        target = pitches[index][1]
        frequency = frequency + (target - frequency) * .007 if smooth and frequency else target
        step = frequency / RATE
        phase = (phase + step) % 1.0
        saw = 2 * phase - 1
        # Smooth the discontinuity to reduce aliasing in the upper consonant bands.
        if phase < step:
            x = phase / step
            saw -= 2 * x - x*x - 1
        elif phase > 1 - step:
            x = (phase - 1) / step
            saw -= x*x + 2*x + 1
        carrier.append(saw)
    return carrier


def render_vocal(dest, plan, text, title, smooth=False):
    if not SAY.is_file():
        raise SpeechUnavailable('Lyric vocals require the macOS built-in speech engine on this studio host.')
    lines = list(schedule(plan, lyric_sections(text, title)))
    notes = sorted(plan['lead'])
    if not lines or not notes:
        raise ValueError('The arrangement has no lyric lines or melody notes for the singer.')
    spb = 60 / plan['tempo']
    total = math.ceil((plan['bars'] * 4 * spb + .2) * RATE)
    output = array('f', [0]) * total
    voice = os.getenv('AGENTHOOD_VOICE', '').strip()
    with tempfile.TemporaryDirectory(dir=dest.parent) as folder:
        cache = {}
        for beat, span, line in lines:
            start = round((beat * spb + .025) * RATE)
            seconds = span * spb - .10
            key = (line, round(seconds, 3))
            if key not in cache:
                cache[key] = speech_samples(line, seconds, voice, Path(folder) / f'{len(cache)}.wav')
            speech = cache[key]
            carrier = carrier_for(notes, start, len(speech), spb, smooth)
            wet = array('f', [0]) * len(speech)
            for freq in VOWEL_BANDS:
                source = bandpass(speech, freq)
                tone = bandpass(carrier, freq)
                envelope = 0.0
                for i, sample in enumerate(source):
                    amplitude = abs(sample)
                    envelope += (amplitude - envelope) * (.045 if amplitude > envelope else .009)
                    wet[i] += tone[i] * envelope
            dry_rms = math.sqrt(sum(s*s for s in speech) / len(speech))
            wet_rms = math.sqrt(sum(s*s for s in wet) / len(wet))
            wet_gain = min(22, dry_rms / max(wet_rms, .001))
            for i in range(min(len(speech), total - start)):
                fade = min(1, i / 115, (len(speech) - i) / 300)
                # Retain original consonants while the vocoded part follows the score.
                value = .40 * speech[i] + .83 * wet[i] * wet_gain
                output[start + i] += math.tanh(value * 2.8) * fade * .60
    if max(map(abs, output), default=0) < .02:
        raise SpeechUnavailable('The lyric vocal rendered without an audible signal.')
    write_pcm(dest, output, output, normalize=True)
    return len(lines)
