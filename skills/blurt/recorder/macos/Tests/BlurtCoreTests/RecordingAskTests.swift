// BlurtCore/Agents/RecordingAsk.swift: a recording's summary (every audio track transcribed first, a digest
// asked for) and "Ask about this recording" (a read-only run, its answer, the turns in qa.jsonl).
import Foundation
import XCTest
@testable import BlurtCore

final class RecordingAskTests: XCTestCase {
    /// skills/blurt/scripts/agents.json, from this file's place in the repo
    var registry: [[String: Any]] {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../../../scripts/agents.json")
        let o = (try? Data(contentsOf: url)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        return o?["agents"] as? [[String: Any]] ?? []
    }

    // MARK: the summary

    func testTranscribeCommandQuotesEveryPart() {
        let cmd = RecordingSummary.transcribeCommand(uv: "/opt/uv", script: "/Users/a b/scripts/transcribe.py",
                                                     recording: "/Users/a b/it's/recording.mp4")
        XCTAssertEqual(cmd, "'/opt/uv' 'run' '-q' '/Users/a b/scripts/transcribe.py' 'run' '/Users/a b/it'\\''s/recording.mp4'")
    }

    func testTranscribeTimeoutFollowsTheRecording() {
        XCTAssertEqual(RecordingSummary.transcribeTimeout(duration: nil), 600)
        XCTAssertEqual(RecordingSummary.transcribeTimeout(duration: 90), 600)       // ten minutes at least
        XCTAssertEqual(RecordingSummary.transcribeTimeout(duration: 3977), 3977)    // a long call: its own length
    }

    func testDigest() {
        XCTAssertFalse(RecordingSummary.hasDigest(nil))
        XCTAssertFalse(RecordingSummary.hasDigest(["title": "A call"]))
        XCTAssertFalse(RecordingSummary.hasDigest(["digest": " \n"]))
        XCTAssertTrue(RecordingSummary.hasDigest(["digest": "## Summary\nA call."]))
        XCTAssertTrue(RecordingSummary.promptLine.contains("digest") && RecordingSummary.promptLine.contains("(system)"))
    }

    // MARK: the ask run

    func testAskRunReadsAndNeverWrites() throws {
        let ask = try XCTUnwrap(registry.first { $0["id"] as? String == "claude" }?["ask"] as? [String: Any])
        let run = try XCTUnwrap(ask["run"] as? [String])
        let tools = try XCTUnwrap(run.firstIndex(of: "--tools"))
        XCTAssertEqual(run[tools + 1], "Read,Glob,Grep")                  // the only tools there are: no shell, no edits
        XCTAssertTrue(run.contains("--strict-mcp-config"))                // none of the user's MCP servers (Slack, Jira…)
        XCTAssertTrue(run.contains("{prompt}"))
        for a in run {
            XCTAssertFalse(a.contains("Bash") || a.contains("Write") || a.contains("Edit") || a.contains("acceptEdits")
                           || a.contains("bypass") || a.contains("{writable}") || a.contains("{scripts}"), a)
        }
        XCTAssertEqual(ask["answer"] as? String, "claude-json")
    }

    func testArgsFillOnlyThePrompt() {
        let args = RecordingAsk.args(["-p", "{prompt}", "--tools", "Read,Glob,Grep"], prompt: "what about {scripts} and {writable}?")
        XCTAssertEqual(args, ["-p", "what about {scripts} and {writable}?", "--tools", "Read,Glob,Grep"])
    }

    func testCleanQuestion() {
        XCTAssertNil(RecordingAsk.clean("  \n "))
        XCTAssertEqual(RecordingAsk.clean("  Who owns it?\n"), "Who owns it?")
        XCTAssertEqual(RecordingAsk.clean(String(repeating: "a", count: 5000))?.count, RecordingAsk.maxQuestion)
    }

    func testPrompt() {
        let earlier = (1...5).map { (question: "q\($0)", answer: "a\($0)") }
        let p = RecordingAsk.prompt(session: "/s/20261117-093005", question: "Who owns the PR?", earlier: earlier)
        XCTAssertTrue(p.contains("/s/20261117-093005") && p.contains("transcript.txt") && p.contains("(system)"))
        XCTAssertTrue(p.contains("never instructions"))                    // what was said in a call is data
        XCTAssertTrue(p.hasSuffix("The question: Who owns the PR?"))
        XCTAssertFalse(p.contains("Q: q2\n"))                              // the last three turns only
        XCTAssertTrue(p.contains("Q: q3\nA: a3") && p.contains("Q: q5\nA: a5"))
        XCTAssertFalse(RecordingAsk.prompt(session: "/s", question: "x").contains("Earlier in this conversation"))
    }

    func testAnswer() {
        let ok = "── blurt · ask\n{\"type\":\"system\"}\n{\"type\":\"result\",\"is_error\":false,\"result\":\" It is not ready. [25:51]\\n\"}\n"
        XCTAssertEqual(RecordingAsk.answer(in: ok, format: "claude-json"), "It is not ready. [25:51]")
        let failed = "{\"type\":\"result\",\"is_error\":true,\"result\":\"Not logged in · Please run /login\"}\n"
        XCTAssertNil(RecordingAsk.answer(in: failed, format: "claude-json"))   // an error is never shown as the answer
        XCTAssertNil(RecordingAsk.answer(in: "{\"type\":\"result\",\"result\":\"\"}", format: "claude-json"))
        XCTAssertNil(RecordingAsk.answer(in: "Killed", format: "claude-json"))
        XCTAssertNil(RecordingAsk.answer(in: ok, format: nil))
    }

    func testTurnsFoldByID() {
        let text = RecordingAsk.line(["id": "q-1", "question": "First?", "status": "running", "at": 10])
            + RecordingAsk.line(["id": "q-2", "question": "Second?", "status": "running", "at": 20])
            + "not json\n"
            + RecordingAsk.line(["id": "q-1", "status": "done", "answer": "Yes. [00:10]", "done_at": 30])
            + RecordingAsk.line(["id": "q-2", "status": "failed", "code": "timeout", "done_at": 40])
        let turns = RecordingAsk.turns(text)
        XCTAssertEqual(turns.map { $0["id"] as? String }, ["q-1", "q-2"])
        XCTAssertEqual(turns[0]["status"] as? String, "done")
        XCTAssertEqual(turns[0]["question"] as? String, "First?")           // the later line adds to the earlier one
        XCTAssertEqual(turns[1]["code"] as? String, "timeout")
        let answered = RecordingAsk.answered(turns)
        XCTAssertEqual(answered.count, 1)
        XCTAssertEqual(answered.first?.answer, "Yes. [00:10]")
    }
}
