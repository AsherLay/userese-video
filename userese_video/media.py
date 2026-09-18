"""Deterministic local media processing using FFmpeg and measured clip clocks."""

import hashlib
import html
import math
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .model import file_lock, read_json, relative_file, write_json


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"{Path(command[0]).name} 处理失败：{result.stderr[-2000:]}")
    return result.stdout


def check_tools():
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise ValueError("请先安装 FFmpeg（含 ffprobe）：" + ", ".join(missing))


def probe(path):
    import json
    check_tools()
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def inspect_source(path):
    info = probe(path)
    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"), None)
    if not video:
        raise ValueError("输入文件没有视频轨道")
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67") or any(
        "DOVI" in item.get("side_data_type", "") for item in video.get("side_data_list", [])
    ):
        raise ValueError("本版仅接受 SDR 视频；请先正确转换 HDR / Dolby Vision 为 SDR Rec.709")
    duration = float(info["format"].get("duration", video.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法读取原片时长")
    return {"duration": duration, "width": video["width"], "height": video["height"],
            "audio": any(stream["codec_type"] == "audio" for stream in info["streams"])}


def fingerprint(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def dimensions(project):
    settings = project.data.get("output", {})
    width, height = settings.get("width", 720), settings.get("height", 1280)
    if any(type(value) is not int or value < 160 or value > 3840 or value % 2 for value in (width, height)):
        raise ValueError("输出宽高需为 160–3840 范围内的偶数")
    return width, height


def encode_part(project, part, destination):
    source = project.sources[part["source"]]
    path = relative_file(project.root, source["file"])
    width, height = dimensions(project)
    seconds = math.ceil((part["end"] - part["start"]) * 25 - 1e-6) / 25
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(part["start"]), "-i", str(path)]
    if not source["audio"]:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    command += ["-map", "0:v:0", "-map", "0:a:0" if source["audio"] else "1:a:0",
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,tpad=stop_mode=clone:stop_duration=0.1",
                "-af", f"aresample=48000:async=1:first_pts=0,apad,atrim=duration={seconds},asetpts=PTS-STARTPTS",
                "-t", str(seconds), "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
                "-threads", "2", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(destination)]
    run(command)
    video = next(stream for stream in probe(destination)["streams"] if stream["codec_type"] == "video")
    return float(video["duration"])


def stamp(seconds):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def write_captions(path, project, state, blocks):
    cues = []
    for block in blocks:
        for cue in project.cues:
            if cue["source"] != block["source"]:
                continue
            if cue["start"] >= block["source_start"] - 0.02 and cue["end"] <= block["source_end"] + 0.02:
                start = max(block["output_start"], block["output_start"] + cue["start"] - block["source_start"])
                end = min(block["output_end"], block["output_start"] + cue["end"] - block["source_start"])
                if end <= start:
                    continue
                value = state["captions"].get(cue["id"], cue["text"])
                cues.append({"source_cue": cue["id"], "start": start, "end": end, "text": value})
    path.write_text("\n".join(
        f"{i}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{html.escape(cue['text'])}\n"
        for i, cue in enumerate(cues, 1)), encoding="utf-8")
    return cues


def verify_sources(project):
    for source in project.sources.values():
        path = relative_file(project.root, source["file"])
        if fingerprint(path) != source["sha256"]:
            raise ValueError("原片在导入后发生变化，请从未修改的源文件创建新项目")


def build(project, state=None, draft=False, burn=True):
    state = state if state is not None else project.state()
    selection = project.selected(state, draft=draft)
    with file_lock(project.root / ".render.lock", blocking=False):
        verify_sources(project)
        build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        folder = project.root / "builds" / build_id
        folder.mkdir(parents=True)
        write_json(folder / "decisions.json", state)
        write_json(folder / "evidence.json", {"project": project.data, "catalog": project.catalog, "transcript": project.cues})
        write_json(folder / "status.json", {"status": "running", "id": build_id})
        try:
            blocks, clock = [], 0.0
            for family, take in selection:
                for part in take["parts"]:
                    name = f"clip-{len(blocks):04}.mp4"
                    measured = encode_part(project, part, folder / name)
                    blocks.append({"family": family["id"], "take": take["id"], "source": part["source"],
                                   "source_start": part["start"], "source_end": part["end"], "file": name,
                                   "output_start": round(clock, 6), "output_end": round(clock + measured, 6)})
                    clock += measured
            (folder / "concat.txt").write_text("".join(f"file '{block['file']}'\n" for block in blocks), encoding="utf-8")
            cues = write_captions(folder / "captions.srt", project, state, blocks)
            command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1", "-i", "concat.txt",
                       "-map", "0:v:0", "-map", "0:a:0"]
            if burn:
                command += ["-vf", "subtitles=captions.srt:force_style='FontName=Noto Sans CJK SC,FontSize=18,"
                            "Outline=1,MarginV=24,MarginL=16,MarginR=16'", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2"]
            else:
                command += ["-c:v", "copy"]
            command += ["-af", "alimiter=limit=0.95:level=false,aresample=48000:async=1:first_pts=0,apad",
                        "-t", str(clock), "-c:a", "aac", "-ar", "48000", "-ac", "2", "-map_metadata", "-1",
                        "-map_chapters", "-1", "-movflags", "+faststart", "video.mp4"]
            run(command, cwd=folder)
            run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "video.mp4", "-frames:v", "1", "-update", "1", "cover.jpg"], cwd=folder)
            run(["ffmpeg", "-v", "error", "-xerror", "-i", str(folder / "video.mp4"), "-f", "null", "-"])
            info = probe(folder / "video.mp4")
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            duration = float(video["duration"])
            if abs(duration - clock) > 0.08:
                raise ValueError("成片时间线与实测片长不一致")
            audio = next(stream for stream in info["streams"] if stream["codec_type"] == "audio")
            if abs(float(audio["duration"]) - duration) > 0.1:
                raise ValueError("成片音画时长不一致")
            manifest = {"id": build_id, "revision": state["revision"], "draft": draft, "burned_captions": burn,
                        "duration": duration, "width": video["width"], "height": video["height"], "blocks": blocks,
                        "captions": cues, "sha256": fingerprint(folder / "video.mp4"), "decode_verified": True,
                        "files": ["video.mp4", "cover.jpg", "captions.srt", "decisions.json", "manifest.json", "evidence.json"]}
            write_json(folder / "manifest.json", manifest)
            write_json(folder / "status.json", {"status": "complete", "id": build_id})
            write_json(project.root / "latest.json", manifest)
            return manifest
        except Exception as exc:
            write_json(folder / "status.json", {"status": "failed", "id": build_id, "error": str(exc)})
            raise


def preview(project, take_id, context=False):
    state = project.state()
    takes = project.all_takes(state)
    catalog = project.catalog_with_proposals(state)
    if take_id not in takes:
        raise ValueError("候选不存在")
    parts = list(takes[take_id]["parts"])
    if context:
        index = next(i for i, family in enumerate(catalog) if any(take["id"] == take_id for take in family["takes"]))
        for families, before in ((list(reversed(catalog[:index])), True), (catalog[index + 1:], False)):
            neighbor = next((takes[state["choices"][family["id"]]["take"]] for family in families
                             if state["choices"][family["id"]]["take"] and state["choices"][family["id"]]["status"] != "skip"), None)
            if neighbor:
                part = dict(neighbor["parts"][-1 if before else 0])
                if before:
                    part["start"] = max(part["start"], part["end"] - 3)
                    parts.insert(0, part)
                else:
                    part["end"] = min(part["end"], part["start"] + 3)
                    parts.append(part)
    from .model import digest
    key = digest({"parts": parts, "evidence": project.identity})[:24]
    folder = project.root / "cache" / key
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock(project.root / ".preview.lock"):
        target = folder / "preview.mp4"
        if not target.exists():
            for i, part in enumerate(parts):
                encode_part(project, part, folder / f"{i}.mp4")
            (folder / "concat.txt").write_text("".join(f"file '{i}.mp4'\n" for i in range(len(parts))), encoding="utf-8")
            run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1", "-i", "concat.txt",
                 "-c", "copy", "-map_metadata", "-1", "-movflags", "+faststart", "preview.tmp.mp4"], cwd=folder)
            (folder / "preview.tmp.mp4").replace(target)
    return key


def history(project):
    return [read_json(path) for path in sorted((project.root / "builds").glob("*/manifest.json"), reverse=True)]
