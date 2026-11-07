"""Translate album references in a brief into production cues for the specialists."""
import re

REFERENCES = (
    (r'\bloud\b', 'bright dance-pop, four-on-the-floor kick, bright synth chords, big chorus'),
    (r'\btalk that talk\b|\bwe found love\b', 'energetic dance-pop, pulsing bass, build into a big final chorus'),
    (r'\bunapologetic\b|\bdiamonds\b', 'midtempo pop, spacious verse, broad rising chorus, sustained keys'),
    (r'\banti\b|\bneeded me\b', 'moody syncopated R&B, sparse verse, intimate upfront vocal, space between drum hits'),
)


def production_cues(brief):
    # Bare album words such as "loud" have their ordinary meaning outside an artist reference.
    if not re.search(r'\brihanna\b', brief, re.I):
        return []
    matches = [cues for pattern, cues in REFERENCES if re.search(pattern, brief, re.I)]
    return matches or ['bright dance-pop chorus, syncopated bass, sparse verse, upfront vocal']
