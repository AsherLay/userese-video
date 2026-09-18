"""Optional real-model smoke test; downloads a tiny English model on first run."""

import tempfile
from pathlib import Path

from userese_video.cli import main
from userese_video.ingest import create
from userese_video.media import build, run
from userese_video.model import read_json


def smoke():
    with tempfile.TemporaryDirectory(prefix="userese-asr-") as directory:
        folder = Path(directory)
        source = folder / "speech.mp4"
        # Entirely synthetic speech; no user footage leaves the test machine.
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
             "color=c=0x18231d:size=240x426:rate=25", "-f", "lavfi", "-i",
             "flite=text='Welcome to the video editing workbench. Compare the recordings and choose your favorite.':voice=slt",
             "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)])
        destination = folder / "transcript.json"
        code = main(["transcribe", str(source), "--output", str(destination), "--model", "tiny.en", "--language", "en"])
        assert code == 0, "ASR command failed"
        result = read_json(destination)
        cues = result["segments"]
        assert result["raw_segments"]
        assert cues and all(cue["end"] > cue["start"] >= 0 for cue in cues)
        recognized = " ".join(cue["text"] for cue in cues).lower()
        assert "video" in recognized and "recordings" in recognized, recognized
        project = create(folder / "project", "ASR smoke", [(source, destination)], output={"width": 240, "height": 426})
        result = build(project, draft=True)
        assert result["decode_verified"] and result["duration"] > 1
        print("Real ASR -> timestamped import -> rendered captions: passed")


if __name__ == "__main__":
    smoke()
