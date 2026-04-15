from __future__ import annotations

import json
from typing import List

from .base import SpeakerSegment


def align_speakers_to_transcript(
    speaker_segments: List[SpeakerSegment],
    transcript_segments_json: str,
    tolerance: float = 0.5,
) -> str:
    """
    Align speaker labels to transcript segments by temporal overlap.

    transcript_segments_json: JSON string of
        [{"start": float, "end": float, "text": str, ...}]
    Returns: updated JSON string with "speaker" field added to each segment.
    """
    transcript_segments = json.loads(transcript_segments_json)

    for ts in transcript_segments:
        ts_start = float(ts.get("start", 0))
        ts_end = float(ts.get("end", 0))

        # Find speaker with maximum overlap
        best_speaker = ""
        best_overlap = 0.0

        for ss in speaker_segments:
            overlap_start = max(ts_start, ss.start_sec)
            overlap_end = min(ts_end, ss.end_sec)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = ss.speaker

        # Only assign if overlap exceeds tolerance
        if best_overlap >= tolerance or (
            best_overlap > 0 and ts_end - ts_start < tolerance * 2
        ):
            ts["speaker"] = best_speaker
        else:
            ts["speaker"] = ""

    return json.dumps(transcript_segments, ensure_ascii=False)
