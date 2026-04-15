from __future__ import annotations

from reel_scout.vision.prompts import get_frame_prompt


BASE_PROMPT = (
    "Describe this frame from a short-form video. Include:\n"
    "1. Visual elements (people, objects, text overlays, backgrounds)\n"
    "2. Any on-screen text (OCR)\n"
    "3. Estimated mood/energy level\n"
    "4. Production style (talking head, b-roll, screen recording, etc.)\n"
    "Be concise. 2-3 sentences max."
)


def test_prompt_no_context() -> None:
    result = get_frame_prompt()
    assert result == BASE_PROMPT


def test_prompt_with_full_context() -> None:
    result = get_frame_prompt(
        frame_index=0,
        total_frames=8,
        timestamp_sec=1.5,
        video_duration_sec=30.0,
    )
    assert result.startswith("This is frame 1 of 8. Captured at 1.5s of a 30s video.")
    assert result.endswith(BASE_PROMPT)
    assert "\n\n" in result


def test_prompt_partial_context_timestamp_only() -> None:
    result = get_frame_prompt(timestamp_sec=5.0)
    assert result.startswith("Captured at 5.0s")
    assert "frame" not in result.split("\n\n")[0].lower()
    assert result.endswith(BASE_PROMPT)
