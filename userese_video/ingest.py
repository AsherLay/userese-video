"""Import timestamped evidence without coupling projects to an ASR provider."""

import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .media import fingerprint, inspect_source, run
from .model import Project, SCHEMA, initialize_state, text, validate_transcript, write_json


def parse_transcript(path):
    path = Path(path)
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".srt":
        cues = []
        pattern = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")
        for block in re.split(r"\n\s*\n", raw.strip().replace("\r\n", "\n")):
            lines = block.splitlines()
            timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
            if timing_index is None:
                raise ValueError("SRT 中缺少时间戳")
            matches = pattern.findall(lines[timing_index])
            if len(matches) != 2:
                raise ValueError("SRT 时间戳格式无效")
            seconds = [int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for h, m, s, ms in matches]
            cues.append({"start": seconds[0], "end": seconds[1], "text": " ".join(lines[timing_index + 1:])})
        return cues
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("segments", data.get("cues"))
    if not isinstance(data, list):
        raise ValueError("转写应为 JSON 数组、含 segments/cues 的对象，或 SRT")
    return [{"start": cue["start"], "end": cue["end"], "text": cue["text"].strip()} for cue in data]


def create(destination, title, inputs, catalog=None, output=None, script=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("目标目录已存在，请使用新的项目目录，避免覆盖审片记录")
    text(title, 200)
    if not inputs:
        raise ValueError("至少需要一组原片和转写")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".userese-import-", dir=destination.parent))
    try:
        (temporary / "media").mkdir()
        sources, cues = [], []
        for i, (media_path, transcript_path) in enumerate(inputs, 1):
            media_path = Path(media_path).resolve()
            sid = f"source-{i:03}"
            info = inspect_source(media_path)
            imported = [{**cue, "id": f"{sid}-cue-{j:04}", "source": sid}
                        for j, cue in enumerate(parse_transcript(transcript_path), 1)]
            validate_transcript(imported, info["duration"])
            suffix = media_path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                suffix = ".video"
            name = f"media/{sid}{suffix}"
            shutil.copyfile(media_path, temporary / name)
            sources.append({"id": sid, "file": name, **info, "sha256": fingerprint(temporary / name)})
            cues.extend(imported)
        if catalog is None:
            # A deterministic starting point, not a claim of semantic AI grouping.
            catalog = [{"id": f"segment-{i:04}", "title": cue["text"][:100],
                        "suggested": f"take-{i:04}", "takes": [{"id": f"take-{i:04}", "label": "原始片段",
                        "parts": [{"source": cue["source"], "start": cue["start"], "end": cue["end"]}]}]}
                       for i, cue in enumerate(cues, 1)]
        manifest = {"schema": SCHEMA, "id": "project-" + uuid.uuid4().hex[:16], "title": title,
                    "sources": sources, "output": output or {"width": 720, "height": 1280}}
        if script:
            (temporary / "script.md").write_text(Path(script).read_text(encoding="utf-8"), encoding="utf-8")
            manifest["script"] = "script.md"
        write_json(temporary / "project.json", manifest)
        write_json(temporary / "transcript.json", cues)
        write_json(temporary / "catalog.json", catalog)
        project = Project(temporary)
        from .media import dimensions
        dimensions(project)
        initialize_state(project)
        temporary.rename(destination)
        return Project(destination)
    except Exception:
        # Only the new, uniquely allocated import staging directory is removed.
        shutil.rmtree(temporary)
        raise


def demo(destination):
    with tempfile.TemporaryDirectory(prefix="userese-demo-") as temporary:
        folder = Path(temporary)
        media = folder / "demo.mp4"
        # Own synthetic footage, no real face, recording, copyrighted music or API.
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=0x18231d:size=360x640:rate=25:duration=12",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12", "-vf",
             "drawbox=x=24:y=145:w=312:h=300:color=0x354530:t=fill,"
             "drawbox=x=24:y=145:w=312:h=300:color=0x303a51:t=fill:enable='between(t,2,4)',"
             "drawbox=x=24:y=145:w=312:h=300:color=0x45324b:t=fill:enable='gte(t,8)',"
             "drawtext=text='userese video':fontcolor=0xd5ef78:fontsize=24:x=28:y=60,"
             "drawtext=text='TAKE A':fontcolor=white:fontsize=40:x=48:y=230:enable='lt(t,2)',"
             "drawtext=text='TAKE B':fontcolor=white:fontsize=40:x=48:y=230:enable='between(t,2,3.999)',"
             "drawtext=text='YOUR CALL':fontcolor=white:fontsize=36:x=48:y=230:enable='between(t,4,7.999)',"
             "drawtext=text='A NEW CUT':fontcolor=white:fontsize=36:x=48:y=230:enable='gte(t,8)',"
             "drawtext=text='Listen. Choose. Keep.':fontcolor=0xc4cabc:fontsize=20:x=48:y=305,"
             "drawtext=text='SYNTHETIC DEMO':fontcolor=0xc4cabc:fontsize=14:x=28:y=490,"
             "drawtext=text='Test tone - no real speech':fontcolor=0xc4cabc:fontsize=13:x=28:y=515",
             "-af", "volume=0.08", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)])
        transcript = folder / "transcript.json"
        write_json(transcript, [
            {"start": 0, "end": 2, "text": "开头 A：先把想法讲清楚"},
            {"start": 2, "end": 4, "text": "开头 B：这一段，用哪一遍？"},
            {"start": 4, "end": 8, "text": "比较候选，保存你的决定"},
            {"start": 8, "end": 12, "text": "确认之后，生成一个新版本"},
        ])
        catalog = [
            {"id": "opening", "title": "选一个更好的开头", "suggested": "opening-b", "takes": [
                {"id": "opening-a", "label": "A · 直接说观点", "parts": [{"source": "source-001", "start": 0, "end": 2}]},
                {"id": "opening-b", "label": "B · 用问题开场", "parts": [{"source": "source-001", "start": 2, "end": 4}]},
            ]},
            {"id": "body", "title": "保留你的判断", "suggested": "body-a", "takes": [
                {"id": "body-a", "label": "完整表达", "parts": [{"source": "source-001", "start": 4, "end": 8}]},
            ]},
            {"id": "ending", "title": "完成一次交付", "suggested": "ending-a", "takes": [
                {"id": "ending-a", "label": "完整结尾", "parts": [{"source": "source-001", "start": 8, "end": 12}]},
            ]},
        ]
        result = create(destination, "这一段，用哪一遍？", [(media, transcript)], catalog, {"width": 360, "height": 640})
        return result
