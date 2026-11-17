"""A recording's transcript and the user's words (docs/design/dictation.md §Custom words: recordings): which terms
a segment may have missed (dictation_vocab.misheard), the second hearing (transcribe.hear_again) and the run command
around it. A fake backend stands in for the model. No model, no network, no ffmpeg.

  uv run --with numpy --with pytest pytest tests/test_transcribe_recording_words.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "blurt" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import dictation_vocab as V  # noqa: E402
import test_asr_select as A  # noqa: E402

T = A.T
SR = A.SR

TERMS = V.normalize([
    {"term": "Midhun", "sounds_like": ["Madan"], "source": "manual"},
    {"term": "Haneel", "source": "manual"},
    {"term": "Anees", "source": "manual"},
    {"term": "Deepa", "sounds_like": ["Adipa"], "source": "manual"},
    {"term": "Db2u", "sounds_like": ["DB to you", "DBT operator"], "source": "manual"},
    {"term": "Big SQL", "sounds_like": ["big sequel"], "source": "manual"},
    {"term": "vCluster", "source": "manual"},
    {"term": "Helm", "source": "manual"},
    {"term": "SRE", "source": "manual"},
    {"term": "Claude", "source": "default"},
])
LOWER = V.lowercase_words(["Sure, we need to test this out.", "and sure, help me with the deep cloud"])


def names(terms) -> list[str]:
    return [t["term"] for t in terms]


class Misheard(unittest.TestCase):
    def test_a_name_spelled_like_a_term_or_a_sounds_like(self):
        self.assertEqual(names(V.misheard(TERMS, "Uh, Haniel, could you do one thing?", LOWER)), ["Haneel"])
        self.assertEqual(names(V.misheard(TERMS, "Mithun, can you say the ticket number?", LOWER)), ["Midhun"])
        self.assertEqual(names(V.misheard(TERMS, "Hmm. Madan. Ah, one more thing.", LOWER)), ["Midhun"])
        self.assertEqual(names(V.misheard(TERMS, "the DB to you operator and big sequel", LOWER)), ["Db2u", "Big SQL"])

    def test_what_the_model_wrote_as_ordinary_words_is_no_near_miss(self):
        # sure / SRE, help / Helm, deep / Deepa, cloud / Claude, needs / Anees: lower case, or a sentence's first
        # word that the transcript also has in lower case
        self.assertEqual(V.misheard(TERMS, "Sure, he needs help with the deep cloud. Help me.", LOWER), [])
        self.assertEqual(V.misheard(TERMS, "regarding the weekluster", LOWER), [])     # a sounds-like's to teach
        self.assertEqual(V.misheard(TERMS, "", LOWER), [])

    def test_a_term_written_right_needs_nothing(self):
        self.assertEqual(V.misheard(TERMS, "Haneel and Midhun, the Vcluster and DB2U", LOWER), [])
        # …and is no near-miss of another listed term (Haneel / Anees)
        self.assertEqual(V.misheard(TERMS, "Sorry, Haneel. Go ahead.", LOWER), [])

    def test_a_sounds_like_is_matched_as_whole_words(self):
        self.assertEqual(V.misheard(TERMS, "the madanapalle office", LOWER), [])
        self.assertEqual(names(V.misheard(TERMS, "so adipa is tracking this", LOWER)), ["Deepa"])

    def test_there_is_no_word_list_behind_it(self):
        """docs/design/dictation.md §Rules vs model: the name signal is the model's own capitals, not a lexicon."""
        saved, V._DICT = V._DICT, set()
        try:
            self.assertEqual(names(V.misheard(TERMS, "Uh, Haniel, sure", LOWER)), ["Haneel"])
        finally:
            V._DICT = saved

    def test_written_counts_the_listed_spelling_only(self):
        self.assertEqual(V.written("Deepa", "Deepa, so Deepa's and Deepak and deepa"), 2)
        self.assertEqual(V.written("Big SQL", "the Big SQL head node, BigSQL"), 1)
        self.assertEqual(V.written("", "anything"), 0)


class Names(A.FakeBackend):
    """Writes "Haniel" unless it was told Haneel: a model that needs the hint. Every word list it is given is kept."""
    seen: list = []
    fail = False

    def __init__(self, *_a, **_k):
        self.terms: list[str] = []

    def set_vocab(self, terms):
        self.terms = names(terms)
        Names.seen.append(list(self.terms))
        return bool(terms)

    def transcribe(self, x):
        if Names.fail and self.terms:
            raise T.Fail("the service is down", "network")
        secs = len(x) / SR
        if secs < 2.5:
            return [(0.0, secs, "Sure, we need to test this out.", "en")]
        who = "Haneel" if "Haneel" in self.terms else "Haniel"
        return [(0.0, secs, f"Uh, {who}, could you check the vCluster?", "en")]


class HearAgain(unittest.TestCase):
    def setUp(self):
        Names.seen, Names.fail = [], False
        self.x = np.zeros(10 * SR, np.float32)

    def test_only_a_segment_that_may_have_missed_a_word_is_heard_again(self):
        b = Names()
        plain = (0.0, 2.0, "Sure, we need to test this out.", "mic", "en")
        miss = (4.0, 8.0, "Uh, Haniel, could you check the vCluster?", "mic", "en")
        out, again, changed = T.hear_again(b, self.x, [plain, miss], TERMS)
        self.assertIs(out[0], plain)
        self.assertEqual(out[1], (4.0, 8.0, "Uh, Haneel, could you check the vCluster?", "mic", "en"))
        self.assertEqual((again, changed), (1, 1))
        # only the words the segment may have missed went to the model, and they were taken back
        self.assertEqual(Names.seen, [["Haneel"], []])
        self.assertEqual(b.terms, [])

    def test_the_first_hearing_stands_when_the_second_has_no_new_listed_word(self):
        b = Names()
        row = (0.0, 2.0, "Sorry, Dipa. Go ahead.", "system")     # heard again: the fake still writes no "Deepa"
        out, again, changed = T.hear_again(b, self.x, [row], TERMS)
        self.assertIs(out[0], row)
        self.assertEqual((again, changed), (1, 0))
        self.assertEqual(Names.seen, [["Deepa"], []])

    def test_the_track_is_loaded_only_when_a_segment_needs_it(self):
        loads = []
        load = lambda: (loads.append(1), self.x)[1]  # noqa: E731
        T.hear_again(Names(), load, [(0.0, 2.0, "Sure, we need to test this out.", "mic")], TERMS)
        self.assertEqual(loads, [])
        rows = [(4.0, 8.0, "Uh, Haniel, could you check?", "mic"), (8.5, 9.5, "And Haniel, I see.", "mic")]
        self.assertEqual(T.hear_again(Names(), load, rows, TERMS)[1:], (2, 1))      # 1 s: the fake writes no name
        self.assertEqual(loads, [1])

    def test_a_failed_second_hearing_keeps_the_first(self):
        b = Names()
        Names.fail = True
        row = (4.0, 8.0, "Uh, Haniel, could you check?", None)
        with contextlib.redirect_stderr(io.StringIO()):
            out, again, changed = T.hear_again(b, self.x, [row], TERMS)
        self.assertIs(out[0], row)
        self.assertEqual((again, changed), (1, 0))
        self.assertEqual(b.terms, [])


class RunCommand(unittest.TestCase):
    """transcribe.py run: the second hearing is on for a backend that takes words, off with --no-vocab or --prompt."""

    def setUp(self):
        Names.seen, Names.fail = [], False
        self.saved = (T.BACKENDS, T.vad_segments, T.load_audio, T.audio_tracks, T.throttle, V.load, T.probe_duration)
        T.BACKENDS = {**T.BACKENDS, "sensevoice": Names}
        T.vad_segments = lambda x, max_speech=25: [(0.0, 2.0), (4.0, 8.0)]
        self.loaded = []
        T.load_audio = lambda path, track=0: (self.loaded.append(track), np.zeros(10 * SR, np.float32))[1]
        T.audio_tracks = lambda path: self.tracks
        T.throttle = lambda *a, **k: {}
        T.probe_duration = lambda path: 10.0
        V.load = lambda path=None: TERMS
        self.tracks = 1
        self.installed = A.fake_installed("sensevoice-small")
        self.dir = tempfile.TemporaryDirectory()
        self.session = Path(self.dir.name) / "session"
        self.session.mkdir()
        A.write_wav(self.session / "recording.wav", 1.0)

    def tearDown(self):
        (T.BACKENDS, T.vad_segments, T.load_audio, T.audio_tracks, T.throttle, V.load, T.probe_duration) = self.saved
        T.installed = self.installed
        self.dir.cleanup()

    def run_it(self, **opts) -> dict:
        a = Namespace(input=str(self.session / "recording.wav"), out=str(Path(self.dir.name) / "out"), backend=None,
                      model=None, model_id=None, language=None, prompt=None, concurrency=4, threads=None, force=False,
                      no_vocab=False)
        for k, v in opts.items():
            setattr(a, k, v)
        Path(a.out).mkdir(exist_ok=True)
        with A.config({"asr": {"auto": False, "backend": "sensevoice"}}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            T.cmd_run(a)
        return json.loads((Path(a.out) / "transcript.json").read_text(encoding="utf-8"))

    def test_a_recording_gets_the_second_hearing(self):
        data = self.run_it()
        self.assertEqual([s["text"] for s in data["segments"]],
                         ["Sure, we need to test this out.", "Uh, Haneel, could you check the vCluster?"])
        self.assertEqual(data["vocabulary"], {"words": len(TERMS), "heard_again": 1, "changed": 1})
        self.assertEqual(Names.seen, [["Haneel"], []])

    def test_no_vocab_and_a_glossary_of_the_callers_turn_it_off(self):
        for opts in ({"no_vocab": True}, {"prompt": "Haneel, Midhun"}):
            data = self.run_it(**opts)
            self.assertEqual(data["segments"][1]["text"], "Uh, Haniel, could you check the vCluster?")
            self.assertNotIn("vocabulary", data)
        self.assertEqual(Names.seen, [])

    def test_a_backend_that_takes_no_words_is_left_alone(self):
        T.BACKENDS = {**T.BACKENDS, "sensevoice": A.FakeBackend}
        self.assertNotIn("vocabulary", self.run_it())

    def test_the_live_transcript_of_the_mic_is_heard_again_too(self):
        """Both tracks: the mic's text came from live.py (no words there) and is reused; its segments get the same
        second hearing, from the recording's mic track."""
        self.tracks = 2
        live = self.session / "live"
        live.mkdir()
        (live / "status.json").write_text(json.dumps({"asr": "done"}), encoding="utf-8")
        (self.session / "transcript.json").write_text(json.dumps({
            "source": "recording.wav", "backend": "sensevoice", "model": "sense-voice-small-int8", "duration": 10.0,
            "speech_seconds": 6.0, "segments": [{"start": 4.0, "end": 8.0, "text": "And Haniel, I see your PR."}]}),
            encoding="utf-8")
        data = self.run_it(out=str(self.session))
        mic = [s["text"] for s in data["segments"] if s.get("speaker") == "mic"]
        system = [s["text"] for s in data["segments"] if s.get("speaker") == "system"]
        self.assertEqual(mic, ["Uh, Haneel, could you check the vCluster?"])
        self.assertEqual(system, ["Sure, we need to test this out.", "Uh, Haneel, could you check the vCluster?"])
        self.assertEqual(data["vocabulary"]["heard_again"], 2)
        self.assertEqual(sorted(self.loaded), [0, 1])      # the mic track was read only for its second hearing


if __name__ == "__main__":
    unittest.main()
