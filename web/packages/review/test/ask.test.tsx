/* "Ask about this recording" (the Overview): the field sends one question to the host, the page waits, the answer
   arrives with the doc, a failure offers Try Again, and a time in the summary or an answer opens the recording there. */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BlurtHost, QATurn, ReviewDoc } from "../src/host";
import { createMacosHost } from "../src/hosts/macos";
import { ReviewStore } from "../src/store";
import { StoreCtx, mdLite, mdTimes } from "../src/ui/ctx";
import { Overview } from "../src/ui/Views";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
Element.prototype.scrollBy ??= () => {}; // jsdom has none

let root: Root | undefined;
let el: HTMLElement | undefined;
afterEach(() => {
  act(() => root?.unmount());
  el?.remove();
  root = el = undefined;
});

const SESSION = "/tmp/rec/.blurt/sessions/20261117-093005";

/** a recording's page in the Mac app: the host answers `ask` by recording a running turn, as the app does */
async function show(over: Partial<ReviewDoc> = {}, extra: Partial<BlurtHost> = {}) {
  let rev = 1;
  const qa: QATurn[] = [...((over.qa as QATurn[]) || [])];
  const asked: string[] = [];
  const doc = (): ReviewDoc => ({
    schema: 3,
    scope: { session: SESSION },
    title: "Release planning call",
    digest: "## Summary\nA call about the release. [25:51]\n\n## Action items\n- Amy: send the checklist [1:05:36]",
    source_video: SESSION + "/recording.mp4",
    _video: true,
    items: [{ id: "REC-4", uid: "U1", kind: "task", title: "Send the checklist" }],
    ...over,
    qa: [...qa],
  });
  const host: BlurtHost = {
    host: "macos",
    scope: { session: SESSION },
    load: async () => ({ doc: doc(), etag: `"${rev}"` }),
    poll: async (e) => (e === `"${rev}"` ? { etag: e } : { doc: doc(), etag: `"${rev}"` }),
    sendOps: async () => ({ ok: true, etag: `"${rev}"` }),
    fileUrl: (p) => "blurt-app://file/" + encodeURIComponent(p),
    ask: async (question) => {
      asked.push(question);
      const id = "q-" + (qa.length + 1);
      qa.push({ id, question, status: "running", at: 1 });
      rev++;
      return { id };
    },
    ...extra,
  };
  const s = new ReviewStore(host, { lang: "en" });
  await s.load();
  el = document.body.appendChild(document.createElement("div"));
  root = createRoot(el);
  await act(async () => root!.render(<StoreCtx.Provider value={s}><Overview /></StoreCtx.Provider>));
  /** the app wrote the end of a turn; the page polls */
  const settle = async (id: string, fields: Partial<QATurn>) => {
    Object.assign(qa.find((t) => t.id === id)!, fields);
    rev++;
    await act(async () => {
      s.pollNow();
      await new Promise((r) => setTimeout(r, 0));
    });
  };
  const input = () => el!.querySelector<HTMLInputElement>("#askInput")!;
  const type = (text: string) =>
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input(), text);
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
  const turns = () => [...el!.querySelectorAll<HTMLElement>(".recask-turn")];
  return { s, asked, settle, input, type, turns, send: () => el!.querySelector<HTMLButtonElement>("#askSend")! };
}
const click = (b: Element | null | undefined) => act(async () => (b as HTMLElement).click());

describe("ask about this recording", () => {
  it("sends the question, waits, then shows the answer the doc brings", async () => {
    const v = await show();
    expect(v.send().disabled).toBe(true); // nothing typed
    v.type("  What did we decide about the release date?  ");
    expect(v.send().disabled).toBe(false);
    await click(v.send());
    expect(v.asked).toEqual(["What did we decide about the release date?"]);
    expect(v.input().value).toBe("");
    expect(v.turns()).toHaveLength(1);
    expect(v.turns()[0].dataset.status).toBe("running");
    expect(v.turns()[0].textContent).toContain("Reading the recording…");
    v.type("another one");
    expect(v.send().disabled).toBe(true); // one question at a time

    await v.settle("q-1", { status: "done", answer: "It is **not ready** to merge. [25:51]" });
    expect(v.turns()[0].dataset.status).toBe("done");
    expect(v.turns()[0].querySelector(".md b")?.textContent).toBe("not ready");
    expect(v.turns()[0].querySelector("button.ts")?.getAttribute("data-t")).toBe(String(25 * 60 + 51));
    expect(v.send().disabled).toBe(false); // the next question can go
  });

  it("Enter asks; an input method's Enter never does", async () => {
    const v = await show();
    v.type("Who owns this PR");
    act(() => void v.input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 229, bubbles: true, cancelable: true })));
    expect(v.asked).toEqual([]);
    await act(async () => void v.input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(v.asked).toEqual(["Who owns this PR"]);
  });

  it("a failed turn says why and Try Again asks the same question again", async () => {
    const v = await show({ qa: [{ id: "q-1", question: "Who owns it?", status: "failed", code: "timeout" }] });
    expect(v.turns()[0].textContent).toContain("This took too long and was stopped.");
    await click(v.turns()[0].querySelector(".recask-err button"));
    expect(v.asked).toEqual(["Who owns it?"]);
    expect(v.turns()).toHaveLength(2);
    // an agent's own reason is shown as the host wrote it
    await v.settle("q-2", { status: "failed", code: "auth", error: "Claude Code isn't logged in: run claude and /login" });
    expect(v.turns()[1].textContent).toContain("Claude Code isn't logged in");
    expect(v.turns()[0].querySelector(".recask-err button")).toBeNull(); // Try Again only on the last one
  });

  it("a host that can't start the question says so and lets the user ask again", async () => {
    const v = await show({}, { ask: async () => ({ id: null, why: "busy" }) });
    v.type("Anything?");
    await click(v.send());
    expect(v.turns()).toHaveLength(0);
    expect(v.s.asking).toBe(false);
    expect(v.s.toast?.msg).toBe("Couldn’t get an answer.");
  });

  it("without a host that can ask there is no field, and earlier answers still show", async () => {
    const v = await show({ qa: [{ id: "q-1", question: "Who owns it?", status: "done", answer: "Amy. [00:42]" }] }, { ask: undefined });
    expect(v.input()).toBeNull();
    expect(v.turns()[0].textContent).toContain("Amy.");
    const none = await show({ qa: [] }, { ask: undefined });
    expect(el!.querySelector("#recAsk")).toBeNull();
    expect(none.s.canAsk).toBe(false);
  });

  it("a time in the summary opens the recording at that moment; no video, no button", async () => {
    await show();
    const ts = [...el!.querySelectorAll<HTMLButtonElement>(".ov > .md button.ts")];
    expect(ts.map((b) => b.dataset.t)).toEqual([String(25 * 60 + 51), String(3600 + 5 * 60 + 36)]);
    expect(ts[1].getAttribute("aria-label")).toBe("Play the recording at 1:05:36");
    expect(el!.querySelector("#ovMoment")).toBeNull();
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(async () => {});
    await click(ts[1]);
    expect(ts[1].isConnected).toBe(true); // the summary is not drawn again: the clicked time is still the same button
    const video = el!.querySelector<HTMLVideoElement>("#ovMoment video")!;
    expect(video.getAttribute("src")).toBe("blurt-app://file/" + encodeURIComponent(SESSION + "/recording.mp4"));
    act(() => void video.dispatchEvent(new Event("loadedmetadata")));
    expect(video.currentTime).toBe(3600 + 5 * 60 + 36);
    expect(play).toHaveBeenCalled();
    await click(el!.querySelector("#ovMomentClose"));
    expect(el!.querySelector("#ovMoment")).toBeNull();
    play.mockRestore();

    act(() => root!.unmount());
    el!.remove();
    await show({ _video: false }); // the video was deleted (Settings → Storage): the times are plain text
    expect(el!.querySelector("button.ts")).toBeNull();
    expect(el!.querySelector(".ov > .md")?.textContent).toContain("[25:51]");
  });
});

describe("the recording's bar", () => {
  it("never opens over the time that was clicked: the page scrolls it clear", async () => {
    await show();
    const ts = el!.querySelector<HTMLButtonElement>(".ov > .md button.ts")!;
    const scroller = el!.querySelector<HTMLElement>("#overviewView")!;
    const rect = (bottom: number) => ({ top: bottom - 18, bottom, left: 0, right: 0, width: 0, height: 18, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    ts.getBoundingClientRect = () => rect(760); // near the bottom of a 768-high page
    scroller.getBoundingClientRect = () => rect(768);
    const height = vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.id === "ovMoment" ? 280 : 0;
    });
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(async () => {});
    const scrolled: ScrollToOptions[] = [];
    scroller.scrollBy = ((o: ScrollToOptions) => void scrolled.push(o)) as typeof scroller.scrollBy;
    await click(ts);
    expect(scrolled).toEqual([{ top: 760 + 12 - (768 - 280) }]); // the bar takes the bottom 280: the time ends 12 above it
    scrolled.length = 0;
    ts.getBoundingClientRect = () => rect(300); // well above the bar: nothing moves
    await click(ts);
    expect(scrolled).toEqual([]);
    height.mockRestore();
    play.mockRestore();
  });
});

describe("times in markdown", () => {
  it("turns [mm:ss] and [h:mm:ss] into buttons and leaves everything else alone", () => {
    const html = mdTimes(mdLite("- Merge later [26:03], see [the PR](https://x.test/1) and [notes]\n- At [1:05:36]"), "Play at {t}");
    expect(html).toContain('<button type="button" class="ts" data-t="1563" aria-label="Play at 26:03">26:03</button>');
    expect(html).toContain('data-t="3936"');
    expect(html).toContain('<a href="https://x.test/1"');
    expect(html).toContain("[notes]");
  });
});

describe("macos host: ask", () => {
  type Sent = { id: number; method: string; params: Record<string, unknown> };
  const win = (info: Record<string, unknown>, sent: Sent[]) =>
    ({ location: { search: "" }, webkit: { messageHandlers: { blurt: { postMessage: (m: Sent) => void sent.push(m) } } }, blurtHostInfo: info }) as never;

  it("is there only when the app offers it, on a recording's page", async () => {
    const sent: Sent[] = [];
    const scope = { session: SESSION };
    const w = win({ host: "macos", methods: ["ask"], scope }, sent);
    const h = createMacosHost(w);
    const p = h.ask!("Who owns it?");
    expect(sent[0]).toMatchObject({ method: "ask", params: { question: "Who owns it?", scope } });
    (w as unknown as { __blurtReply: (id: number, r: unknown) => void }).__blurtReply(sent[0].id, { id: "q-1", why: null });
    await expect(p).resolves.toEqual({ id: "q-1", why: null });
    expect(createMacosHost(win({ host: "macos", methods: ["reveal"], scope }, [])).ask).toBeUndefined(); // an older app
    expect(createMacosHost(win({ host: "macos", methods: ["ask"], scope: { project: "3f2a" } }, [])).ask).toBeUndefined();
  });
});
