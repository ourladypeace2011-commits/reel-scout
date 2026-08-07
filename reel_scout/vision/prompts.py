from __future__ import annotations

from typing import List, Optional


def get_frame_prompt(
    frame_index: Optional[int] = None,
    total_frames: Optional[int] = None,
    timestamp_sec: Optional[float] = None,
    video_duration_sec: Optional[float] = None,
) -> str:
    """Build VLM prompt for frame description, with optional context."""
    context_parts = []  # type: List[str]
    if frame_index is not None and total_frames is not None:
        context_parts.append(
            "This is frame %d of %d." % (frame_index + 1, total_frames)
        )
    if timestamp_sec is not None:
        context_parts.append("Captured at %.1fs" % timestamp_sec)
        if video_duration_sec:
            context_parts.append("of a %.0fs video." % video_duration_sec)

    context_line = " ".join(context_parts)

    # The two trailing lines are what `vision.parse` reads back into
    # `text_in_frame` and `objects`. Asking item 2 in prose was never enough:
    # the model answered, but inside the paragraph, where nothing structured
    # could pick it up. A model that ignores the directive still gets parsed --
    # `parse.text_from_prose` recovers quoted spans from the paragraph -- so the
    # tail raises the ceiling without being load-bearing.
    base_prompt = (
        "Describe this frame from a short-form video. Include:\n"
        "1. Visual elements (people, objects, text overlays, backgrounds)\n"
        "2. Any on-screen text (OCR)\n"
        "3. Estimated mood/energy level\n"
        "4. Production style (talking head, b-roll, screen recording, etc.)\n"
        "Be concise. 2-3 sentences max.\n"
        "\n"
        "Then end your reply with exactly these two lines:\n"
        "ON_SCREEN_TEXT: <every word of text laid over the frame, verbatim; "
        "NONE if there is none. Do not include text that belongs to the scene "
        "itself, such as a shop sign or a printed shirt.>\n"
        "OBJECTS: <3-8 key visual objects, comma-separated>"
    )

    if context_line:
        return context_line + "\n\n" + base_prompt
    return base_prompt
