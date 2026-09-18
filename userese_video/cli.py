"""Command line interface shared by people, automation and the bundled skill."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .ingest import create, demo, normalize_asr
from .media import build, check_tools, inspect_source, run
from .model import Project, read_json, write_json


def parser():
    root = argparse.ArgumentParser(prog="userese-video", description="本地口播剪片台 · AI 提建议，你来决定")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查 Python、FFmpeg 和字幕渲染能力")
    example = commands.add_parser("demo", help="生成无私人素材的演示项目")
    example.add_argument("project")
    ingest = commands.add_parser("import", help="从原片与带时间戳的转写建立项目")
    ingest.add_argument("project")
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--source", action="append", nargs=2, metavar=("VIDEO", "TRANSCRIPT"), required=True)
    ingest.add_argument("--catalog", help="可选：按语义分组的候选 JSON")
    ingest.add_argument("--script", help="可选：原始文稿 Markdown 快照")
    ingest.add_argument("--width", type=int, default=720)
    ingest.add_argument("--height", type=int, default=1280)
    serve = commands.add_parser("serve", help="启动一个项目的审片工作台")
    serve.add_argument("project")
    serve.add_argument("--host", default="127.0.0.1", help="具体 IPv4 地址；分享时填写 Tailscale IPv4")
    serve.add_argument("--port", type=int, default=8765)
    render = commands.add_parser("build", help="按已保存决定生成新版本")
    render.add_argument("project")
    render.add_argument("--draft", action="store_true", help="明确允许未确认建议进入建议版；待优化仍会阻止生成")
    render.add_argument("--no-burn", action="store_true", help="只导出独立字幕，不烧录字幕")
    validate = commands.add_parser("validate", help="校验项目证据、候选和人工决定")
    validate.add_argument("project")
    export = commands.add_parser("export", help="把决定快照输出为 JSON")
    export.add_argument("project")
    apply = commands.add_parser("apply", help="以乐观锁导入人工决定快照")
    apply.add_argument("project")
    apply.add_argument("decisions")
    apply.add_argument("--revision", type=int, required=True)
    propose = commands.add_parser("propose", help="新增候选剪法，保留所有人工取舍；浏览器刷新后可试听")
    propose.add_argument("project")
    propose.add_argument("--family", required=True)
    propose.add_argument("--id", required=True)
    propose.add_argument("--label", required=True)
    propose.add_argument("--part", action="append", nargs=3, metavar=("SOURCE", "START", "END"), required=True)
    propose.add_argument("--revision", type=int, required=True)
    asr = commands.add_parser("transcribe", help="使用可选 faster-whisper 本地转写；首次下载模型")
    asr.add_argument("video")
    asr.add_argument("--output", required=True)
    asr.add_argument("--model", default="small")
    asr.add_argument("--language", default="zh")
    asr.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            check_tools()
            filters = run(["ffmpeg", "-hide_banner", "-filters"])
            encoders = run(["ffmpeg", "-hide_banner", "-encoders"])
            missing = [name for name, listing in (("subtitles", filters), ("drawtext", filters), ("libx264", encoders), ("aac", encoders)) if name not in listing]
            if missing:
                raise ValueError("FFmpeg 缺少功能：" + ", ".join(missing))
            print(json.dumps({"ok": True, "version": __version__, "python": sys.version.split()[0], "ffmpeg": run(["ffmpeg", "-version"]).splitlines()[0]}, ensure_ascii=False))
        elif args.command == "demo":
            project = demo(args.project)
            print(json.dumps({"project": str(project.root), "synthetic": True}, ensure_ascii=False))
        elif args.command == "import":
            project = create(args.project, args.title, args.source, read_json(args.catalog) if args.catalog else None,
                             {"width": args.width, "height": args.height}, args.script)
            print(json.dumps({"project": str(project.root), "segments": len(project.catalog)}, ensure_ascii=False))
        elif args.command == "transcribe":
            destination = Path(args.output)
            if destination.exists():
                raise ValueError("转写输出已存在，请使用新文件名")
            source_info = inspect_source(Path(args.video))
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ValueError('请先安装可选转写依赖：pip install ".[asr]"') from exc
            try:
                model = WhisperModel(args.model, device=args.device, compute_type="int8" if args.device == "cpu" else "float16")
                segments, _ = model.transcribe(args.video, language=args.language, vad_filter=True)
                cues = [{"start": segment.start, "end": segment.end, "text": segment.text.strip()} for segment in segments]
            except Exception as exc:
                raise ValueError(f"本地转写失败，请检查模型路径、下载连接或运行库：{exc}") from exc
            if not cues:
                raise ValueError("未识别到语音，请检查音轨、语言和录音内容")
            normalized = normalize_asr(cues, source_info["duration"])
            if not normalized["segments"]:
                raise ValueError("识别时间戳不在视频范围内，请检查音轨或换用其他模型")
            write_json(destination, normalized)
            if normalized["timing_adjustments"]:
                print("提示：已将识别时间限定在实际视频内，原始时间和调整记录保存在输出文件中。", file=sys.stderr)
            print(json.dumps({"transcript": str(destination)}, ensure_ascii=False))
        else:
            project = Project(args.project)
            if args.command == "serve":
                import ipaddress
                from .server import Workbench
                address = ipaddress.IPv4Address(args.host)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError("请绑定具体的本机 IPv4 地址")
                with Workbench((args.host, args.port), project) as server:
                    port = server.server_address[1]
                    print(f"Userese Video {__version__}\nhttp://{args.host}:{port}/#token={server.token}", flush=True)
                    print("访问口令仅保存在本次进程中；结束服务后链接失效。按 Ctrl+C 停止。", flush=True)
                    server.serve_forever()
            elif args.command == "build":
                print(json.dumps(build(project, draft=args.draft, burn=not args.no_burn), ensure_ascii=False))
            elif args.command == "validate":
                project.state()
                from .media import dimensions, verify_sources
                dimensions(project)
                verify_sources(project)
                print(json.dumps({"ok": True, "segments": len(project.catalog), "takes": len(project.takes)}, ensure_ascii=False))
            elif args.command == "export":
                print(json.dumps(project.state(), ensure_ascii=False, indent=2))
            elif args.command == "apply":
                print(json.dumps(project.save(read_json(args.decisions), args.revision), ensure_ascii=False))
            elif args.command == "propose":
                state = project.state()
                if args.family not in project.families:
                    raise ValueError("语义段不存在")
                candidate = {"id": args.id, "label": args.label, "parts": [
                    {"source": source, "start": float(start), "end": float(end)} for source, start, end in args.part]}
                state.setdefault("proposals", {}).setdefault(args.family, []).append(candidate)
                print(json.dumps(project.save(state, args.revision), ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
