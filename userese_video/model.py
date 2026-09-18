"""Project evidence, proposals, human decisions and immutable build snapshots."""

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCHEMA = "userese-video/1"
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")


class Conflict(ValueError):
    """A client attempted to overwrite a newer decision revision."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def relative_file(root, name):
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("文件必须使用项目内的相对路径")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("文件不存在，或位于项目目录之外")
    return path


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("编号只能包含字母、数字、下划线和短横线，最长 80 字符")
    return value


def text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"文字不能为空，且最多 {maximum} 字符")
    return value


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("时间必须是有限数字（秒）")
    return value


def validate_transcript(cues, duration):
    if not isinstance(cues, list) or not cues:
        raise ValueError("需要至少一句带时间戳的转写")
    seen, previous = set(), 0
    for cue in cues:
        cid = identifier(cue["id"])
        start, end = number(cue["start"]), number(cue["end"])
        if cid in seen or start < previous - 0.001 or not 0 <= start < end <= duration + 0.05:
            raise ValueError("转写编号需唯一，时间按序且不重叠，并处于原片时长内")
        text(cue["text"])
        seen.add(cid)
        previous = end


def validate_catalog(catalog, sources, cues):
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("至少需要一个语义段")
    family_ids, take_ids = set(), set()
    for family in catalog:
        fid = identifier(family["id"])
        if fid in family_ids:
            raise ValueError("语义段编号重复")
        family_ids.add(fid)
        text(family["title"], 200)
        if not isinstance(family["takes"], list) or not family["takes"]:
            raise ValueError("每段至少需要一个候选")
        local_ids = set()
        for take in family["takes"]:
            tid = identifier(take["id"])
            if tid in take_ids:
                raise ValueError("候选编号重复")
            take_ids.add(tid)
            local_ids.add(tid)
            text(take["label"], 200)
            if not isinstance(take["parts"], list) or not take["parts"]:
                raise ValueError("候选至少包含一个原片区间")
            for part in take["parts"]:
                sid = part["source"]
                start, end = number(part["start"]), number(part["end"])
                if sid not in sources or not 0 <= start < end <= sources[sid]["duration"] + 0.05:
                    raise ValueError("候选区间超出原片")
                if end - start < 0.08:
                    raise ValueError("候选区间过短")
                # Full cues are the smallest safe editorial unit; do not invent a
                # complete sentence caption over half of its recorded speech.
                for cue in cues:
                    if cue["source"] == sid and cue["end"] > start + 0.02 and cue["start"] < end - 0.02:
                        if cue["start"] < start - 0.02 or cue["end"] > end + 0.02:
                            raise ValueError(f"{tid} 切断字幕 {cue['id']}，请按完整短句调整边界")
        if family.get("suggested") not in local_ids | {None}:
            raise ValueError("建议必须引用本段候选")


@contextmanager
def file_lock(path, blocking=True):
    with Path(path).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise Conflict("已有任务正在运行，请稍后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data = read_json(self.root / "project.json")
        if self.data.get("schema") != SCHEMA:
            raise ValueError("不支持的项目版本")
        identifier(self.data["id"])
        text(self.data["title"], 200)
        self.sources = {source["id"]: source for source in self.data["sources"]}
        if not self.sources or len(self.sources) != len(self.data["sources"]):
            raise ValueError("原片编号缺失或重复")
        self.cues = read_json(self.root / "transcript.json")
        cue_ids = set()
        for sid, source in self.sources.items():
            identifier(sid)
            relative_file(self.root, source["file"])
            number(source["duration"])
            subset = [cue for cue in self.cues if cue["source"] == sid]
            validate_transcript(subset, source["duration"])
            for cue in subset:
                if cue["id"] in cue_ids:
                    raise ValueError("字幕编号必须在整个项目中唯一")
                cue_ids.add(cue["id"])
        if len(cue_ids) != len(self.cues):
            raise ValueError("字幕引用了不存在的原片")
        self.catalog = read_json(self.root / "catalog.json")
        validate_catalog(self.catalog, self.sources, self.cues)
        self.families = {family["id"]: family for family in self.catalog}
        self.takes = {take["id"]: take for family in self.catalog for take in family["takes"]}
        self.identity = digest({"project": self.data, "catalog": self.catalog, "transcript": self.cues})

    def state(self):
        state = read_json(self.root / "decisions.json")
        self.validate_state(state)
        return state

    def catalog_with_proposals(self, state):
        additions = state.get("proposals", {})
        if not isinstance(additions, dict) or not set(additions).issubset(self.families):
            raise ValueError("建议需对应已有语义段")
        merged = copy.deepcopy(self.catalog)
        for family in merged:
            extra = additions.get(family["id"], [])
            if not isinstance(extra, list):
                raise ValueError("候选建议需为数组")
            family["takes"].extend(extra)
        validate_catalog(merged, self.sources, self.cues)
        return merged

    def all_takes(self, state):
        return {take["id"]: take for family in self.catalog_with_proposals(state) for take in family["takes"]}

    def validate_state(self, state):
        if not isinstance(state, dict):
            raise ValueError("选择文件必须为 JSON 对象")
        if state.get("schema") != SCHEMA or state.get("project_id") != self.data["id"] or state.get("evidence") != self.identity:
            raise ValueError("选择文件与项目证据不匹配，请使用同一项目的导出文件")
        if type(state.get("revision")) is not int or state["revision"] < 0:
            raise ValueError("无效的修改版本")
        choices = state.get("choices")
        if not isinstance(choices, dict) or set(choices) != set(self.families):
            raise ValueError("选择文件的段落不匹配")
        families = {family["id"]: family for family in self.catalog_with_proposals(state)}
        for fid, choice in choices.items():
            if not isinstance(choice, dict):
                raise ValueError("选择格式无效")
            allowed = {take["id"] for take in families[fid]["takes"]}
            if choice.get("take") not in allowed | {None}:
                raise ValueError("候选不属于对应语义段")
            if choice.get("status") not in ("pending", "keep", "skip", "optimize"):
                raise ValueError("无效的审片状态")
            if choice["status"] == "keep" and choice["take"] is None:
                raise ValueError("使用此段需要选择候选")
            if choice["status"] == "skip" and choice["take"] is not None:
                raise ValueError("暂不放不能同时选择候选")
            if not isinstance(choice.get("note"), str) or len(choice["note"]) > 3000:
                raise ValueError("备注最长 3000 字符")
        overrides = state.get("captions")
        if not isinstance(overrides, dict):
            raise ValueError("字幕纠错格式无效")
        known = {cue["id"] for cue in self.cues}
        for cid, value in overrides.items():
            text(value, 200)
            if cid not in known or any(char in value for char in "\n\r<>{}\\"):
                raise ValueError("字幕须为单行纯文本，并引用现有字幕编号")

    def save(self, proposed, expected_revision):
        with file_lock(self.root / ".decisions.lock"):
            current = self.state()
            if type(expected_revision) is not int or current["revision"] != expected_revision:
                raise Conflict("其他页面已保存新修改。请先导出本页修改，再刷新并重新合并")
            updated = copy.deepcopy(proposed)
            self.validate_state(updated)
            # Existing candidate IDs are immutable. New proposals can be added;
            # a restored older snapshot retains candidates already delivered.
            extras = updated.setdefault("proposals", {})
            for fid, existing in current.get("proposals", {}).items():
                incoming = {take["id"]: take for take in extras.setdefault(fid, [])}
                for take in existing:
                    if take["id"] in incoming and incoming[take["id"]] != take:
                        raise ValueError("已有候选编号不可改写，请为新剪法使用新编号")
                    if take["id"] not in incoming:
                        extras[fid].append(copy.deepcopy(take))
            self.validate_state(updated)
            updated["revision"] = current["revision"] + 1
            # Keep the previous state even when an imported snapshot is restored.
            write_json(self.root / "history" / f"decisions-{current['revision']:06}.json", current)
            write_json(self.root / "decisions.json", updated)
            return updated

    def selected(self, state, draft=False):
        self.validate_state(state)
        takes = self.all_takes(state)
        result = []
        for family in self.catalog:
            choice = state["choices"][family["id"]]
            if choice["status"] == "optimize":
                raise ValueError(f"{family['title']} 标为待优化，请处理并重新确认")
            if choice["status"] == "pending" and not draft:
                raise ValueError("仍有未确认段落。请逐段确认，或明确生成建议版")
            if choice["status"] != "skip" and choice["take"]:
                result.append((family, takes[choice["take"]]))
        if not result:
            raise ValueError("至少保留一个片段")
        return result


def initialize_state(project):
    state = {"schema": SCHEMA, "project_id": project.data["id"], "evidence": project.identity,
             "revision": 0, "captions": {}, "proposals": {}, "choices": {
                 family["id"]: {"take": family.get("suggested"), "status": "pending", "note": ""}
                 for family in project.catalog}}
    write_json(project.root / "decisions.json", state)
    return state
