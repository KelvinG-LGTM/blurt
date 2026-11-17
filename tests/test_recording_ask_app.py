"""A recording's summary and "Ask about this recording", end to end in the real app, with a real agent (opt-in GUI
test: it spends tokens and needs a recording of your own).

An isolated copy of the app organizes a short clip of a call you recorded with system audio. The clip starts with
the microphone-only transcript a live recording leaves, as the app finds it. Checks:
  1. before the agent starts, the app transcribes every audio track: transcript.txt has `(system)` lines;
  2. the run writes the session's summary (`digest`), and the page shows it on the Overview;
  3. a question typed into the page's field is answered there, from the recording, with times;
  4. a click on a time opens the recording at that moment.

  BLURT_GUI_TESTS=1 BLURT_ASK_RECORDING=<session dir> uv run --with pytest pytest tests/test_recording_ask_app.py -q -s
  BLURT_ASK_RECORDING   a session folder with recording.mp4 (two audio tracks) and its transcript.json
  BLURT_ASK_CLIP        START,SECONDS of the clip (default 1140,180)
  BLURT_ASK_QUESTION    the question (default: "What was decided in this recording?")
  BLURT_GUI_APP         an app you already built with BLURT_TEST_HOOKS=1 (default: build this checkout)
  BLURT_ASK_KEEP        a folder to keep the world and the logs in (default: a temp dir, removed). Not under a
                        `.claude` folder: Claude Code asks before it writes there, and an unattended run then waits
  BLURT_ASK_WORLD       a kept world whose recording is already organized: only the page checks (2 to 4) run
The speech models are the ones in your own Blurt home (linked, never written); the agent is your first usable one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gui_layout as gui  # noqa: E402

SOURCE = os.environ.get("BLURT_ASK_RECORDING")
WORLD = os.environ.get("BLURT_ASK_WORLD")
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("BLURT_GUI_TESTS") != "1" or not (SOURCE or WORLD),
                                reason="GUI test with a real agent: BLURT_GUI_TESTS=1 BLURT_ASK_RECORDING=<session> on macOS")

QUESTION = os.environ.get("BLURT_ASK_QUESTION", "What was decided in this recording?")
REAL_HOME = Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt")


def make_session(source: Path, ws: Path, start: float, seconds: float) -> Path:
    """A clip of the recording as the app leaves a take: the video with both audio tracks, meta.json, and the live
    transcript, which has only the microphone."""
    session = ws / "inbox" / "sessions" / "20261117-093005"
    (session / "live").mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(start), "-t", str(seconds), "-i",
                    str(source / "recording.mp4"), "-map", "0", "-c", "copy", str(session / "recording.mp4")], check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(session / "recording.mp4")], capture_output=True, text=True, check=True).stdout)
    full = json.loads((source / "transcript.json").read_text(encoding="utf-8"))
    mic = [dict(s, start=round(s["start"] - start, 2), end=round(s["end"] - start, 2)) for s in full["segments"]
           if s.get("speaker") != "system" and start <= s["start"] and s["end"] <= start + dur]
    for s in mic:
        s.pop("speaker", None)
    data = {**{k: v for k, v in full.items() if k != "segments"}, "source": "recording.mp4", "duration": round(dur, 2),
            "speech_seconds": round(sum(s["end"] - s["start"] for s in mic), 2), "segments": mic}
    (session / "transcript.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (session / "transcript.txt").write_text(
        "".join(f"[{int(s['start']) // 60:02d}:{s['start'] % 60:04.1f}-{int(s['end']) // 60:02d}:{s['end'] % 60:04.1f}] {s['text']}\n"
                for s in mic), encoding="utf-8")
    (session / "live" / "status.json").write_text(json.dumps({"asr": "done", "updated": time.time(), "audio_seconds": dur}))
    (session / "live" / "stop").write_text("")
    (session / "meta.json").write_text(json.dumps({"engine": "native-app", "video": "recording.mp4", "duration": dur,
                                                   "started_at": "2026-11-17T14:30:14Z"}))
    return session


def make_world(tmp: Path) -> tuple[Path, Path]:
    """An empty Blurt home and workspace that use your speech models and speech settings, read-only."""
    home, ws = tmp / "home", tmp / "ws"
    home.mkdir(parents=True)
    ws.mkdir(parents=True)
    # skill_dir: this checkout's scripts and page, never an installed copy
    cfg = {"record": {"install_app": False}, "app": {"onboarded": True}, "skill_dir": str(gui.ROOT / "skills" / "blurt")}
    try:
        cfg["asr"] = json.loads((REAL_HOME / "config.json").read_text(encoding="utf-8"))["asr"]
    except (OSError, ValueError, KeyError):
        pass
    (home / "config.json").write_text(json.dumps(cfg))
    if (REAL_HOME / "models").is_dir():
        (home / "models").symlink_to(REAL_HOME / "models")
    return home, ws


def send(app: gui.App, cmd: str) -> None:
    """A command on the app's test channel, which the app echoes: one that is not heard fails here, at once."""
    app.mark()
    app.post(cmd, "shell")
    app.wait("test_command", timeout=10, cmd=cmd)


def knock(app: gui.App) -> None:
    """The test channel is listened to a moment after the app says it is ready: knock until it answers."""
    for _ in range(40):
        app.mark()
        app.post("hello", "shell")
        try:
            app.wait("test_command", timeout=0.5)
            return
        except TimeoutError:
            pass
    raise TimeoutError("the app's test channel never answered")


def js(app: gui.App, code: str, timeout: float = 15) -> str:
    """Run `code` in the main window's review page; its result as text."""
    send(app, "js " + code)
    return str(app.wait("js", timeout=timeout)["result"])


def poll(what: str, check, timeout: float, every: float = 1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = check()
        if r:
            return r
        time.sleep(every)
    raise TimeoutError(f"{what}: not within {timeout:.0f}s")


def test_summary_and_ask_in_the_app():
    start, seconds = (float(x) for x in os.environ.get("BLURT_ASK_CLIP", "1140,180").split(","))
    keep = WORLD or os.environ.get("BLURT_ASK_KEEP")
    tmp = Path(keep).expanduser() if keep else Path(tempfile.mkdtemp(prefix="blurt-ask-"))
    tmp.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    app = None
    try:
        built = Path(os.environ["BLURT_GUI_APP"]) if os.environ.get("BLURT_GUI_APP") else gui.build_app(tmp)
        if WORLD:
            home, ws = tmp / "home", tmp / "ws"
            session = next(p for p in sorted((ws / "inbox" / "sessions").iterdir()) if (p / "batch.json").is_file())
        else:
            home, ws = make_world(tmp)
            session = make_session(Path(SOURCE).expanduser(), ws, start, seconds)
        mic_lines = len((session / "transcript.txt").read_text(encoding="utf-8").splitlines())
        app = gui.App(gui.isolated_copy(built, tmp), home, ws, "en", tmp / "app.log")
        app.start()
        knock(app)

        if not WORLD:
            # 1 + 2: organizing transcribes the other side first, and writes the summary
            t0 = time.time()
            send(app, f"processdir {session}")
            heard = app.wait("process_transcribed", timeout=900)
            report["transcribe_s"] = round(time.time() - t0, 1)
            text = (session / "transcript.txt").read_text(encoding="utf-8")
            system = [l for l in text.splitlines() if "] (system) " in l]
            report.update(mic_lines=mic_lines, lines=len(text.splitlines()), system_lines=len(system))
            assert heard["exit"] == 0, heard
            assert system and heard["system_lines"] == len(system), "the system track was not transcribed"
            app.wait("process_usage", timeout=1500)
            report["organize_s"] = round(time.time() - t0, 1)
        batch = poll("batch.json", lambda: (session / "batch.json").is_file() and json.loads((session / "batch.json").read_text()), 60)
        digest = str(batch.get("digest") or "")
        report.update(items=len(batch.get("items") or []), digest_chars=len(digest), digest_head=digest[:160])
        assert "## Summary" in digest, f"no summary in the digest: {digest[:200]!r}"
        log = (session / "agent.log").read_text(encoding="utf-8")
        assert "transcribing every audio track" in log and "no summary" not in log

        # the page: the Overview with the summary and the field
        send(app, f"review {session}")
        page = poll("the Overview", lambda: (lambda r: r if r.startswith("ok") else None)(js(
            app, "(()=>{const m=document.querySelector('#overviewView .ov > .md');const i=document.querySelector('#askInput');"
                 "return m&&i?'ok '+m.innerText.length+' '+document.querySelectorAll('#overviewView .ov > .md button.ts').length:'no'})()")), 60, 2)
        report["overview"] = page

        # 3: a question, typed and sent on the page
        t1 = time.time()
        asked_from = app.log.stat().st_size
        sent = js(app, "(()=>{const i=document.querySelector('#askInput');"
                       "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i," + json.dumps(QUESTION) + ");"
                       "i.dispatchEvent(new Event('input',{bubbles:true}));"
                       "const b=document.querySelector('#askSend');if(b.disabled)return 'disabled';b.click();return 'sent'})()")
        assert sent == "sent", sent
        waiting = poll("the waiting turn", lambda: (lambda r: r if r != "none" else None)(js(
            app, "(()=>{const t=[...document.querySelectorAll('.recask-turn')].pop();return t?t.dataset.status+'|'+t.innerText:'none'})()")), 20)
        assert waiting.startswith("running|") and QUESTION in waiting, waiting
        done = app.wait("ask_done", timeout=330, since=asked_from)
        report["ask_s"] = round(time.time() - t1, 1)
        assert done["status"] == "done", done
        turn = json.loads((session / "qa.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert turn["status"] == "done" and turn["answer"], turn
        shown = poll("the answer on the page", lambda: (lambda r: r if r.startswith("done|") else None)(js(
            app, "(()=>{const t=[...document.querySelectorAll('.recask-turn')].pop();const m=t&&t.querySelector('.md');"
                 "return t?t.dataset.status+'|'+(m?m.querySelectorAll('button.ts').length+'|'+m.innerText:''):'none'})()")), 20)
        _, times, answer = shown.split("|", 2)
        report.update(answer=answer[:600], answer_times=int(times))
        assert answer.strip() and int(times) > 0, shown

        # 4: a time opens the recording at that moment
        at = int(js(app, "(()=>{const b=[...document.querySelectorAll('.recask-turn')].pop().querySelector('.md button.ts');b.click();return b.dataset.t})()"))
        played = poll("the recording at that moment", lambda: (lambda r: r if r.startswith("ok") else None)(js(
            app, "(()=>{const v=document.querySelector('#ovMoment video');"
                 "return v&&v.readyState>=1&&v.currentTime>=" + str(at) + "?'ok '+v.currentTime.toFixed(1)+' '+v.duration.toFixed(1):'no'})()")), 30)
        report.update(clicked_t=at, video=played)
        assert float(played.split()[1]) < at + 20, played
        # the bar docks under the time that was clicked, never over it
        clear = js(app, "(()=>{const b=[...document.querySelectorAll('.recask-turn')].pop().querySelector('.md button.ts').getBoundingClientRect();"
                        "const bar=document.querySelector('#ovMoment').getBoundingClientRect();"
                        "return b.bottom<=bar.top?'clear':'covered '+Math.round(b.bottom)+'>'+Math.round(bar.top)})()")
        assert clear == "clear", clear
    finally:
        (tmp / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
        if app:
            app.stop()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
