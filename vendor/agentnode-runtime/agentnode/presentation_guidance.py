"""Compatibility export; the full Surface contract has one source in prompts/."""
from pathlib import Path
PRESENTATION_GUIDANCE = (Path(__file__).resolve().parents[1] / 'prompts/surface.md').read_text() + '\n\n'
