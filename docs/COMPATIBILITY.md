# Which apps live mode works with

Measured on 18 November 2026, on one Mac running macOS 26.3.1. Each app
was opened in the background and looked at through live mode's own look,
the way an agent would. Nothing was pressed or typed. The sweep kept
counts only: no label, value or window title from any app was saved.

For each app, up to 30 elements an agent could press or type into were
checked: vent built each one's anchor and looked for it again in the
live app.

- **Replays** means the anchor found that element and nothing else. A
  saved tool can use it.
- **Live only** means the anchor ties with a lookalike (two scroll bars'
  arrow buttons, say). A live act still works, because vent knows which
  one the agent picked, but saving that step is refused.
- **Lost** means the anchor found nothing.

| App | Kind | Elements | Look time | Replays | Live only | Lost | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Books | Apple | 56 | 0.27 s | 30 | 0 | 0 | |
| Calculator | Apple | 60 | 0.12 s | 30 | 0 | 0 | Asks first before Close Bracket, Recall Memory, Delete, All Clear and Change Sign: their names hold words vent treats as risky (close, recall, delete, clear, sign) |
| Calendar | Apple | 71 | 0.13 s | 19 | 2 | 0 | |
| Clock | Apple | 13 | 0.12 s | 11 | 1 | 0 | |
| Dictionary | Apple | 21 | 0.09 s | 18 | 0 | 0 | |
| Finder | Apple | 215 | 2.9 s | 1 | 12 | 17 | Its window changed during the check. A recheck on a still /Applications window found all 28 sampled elements. A long file list can take over 10 s to read; the look then stops and says it has only part |
| Font Book | Apple | 188 | 0.32 s | 20 | 6 | 0 | |
| Freeform | Apple | 37 | 0.11 s | 16 | 10 | 0 | The lookalikes are scroll bar arrows |
| Maps | Apple | 27 | 0.25 s | 17 | 10 | 0 | |
| Music | Apple | 44 | 0.13 s | 13 | 6 | 0 | |
| Notes | Apple | 176 | 2.8 s | 19 | 4 | 5 | Still syncing during the check, and one anchor found a different element. A recheck once it had settled found none lost or elsewhere |
| Podcasts | Apple | 12 | 0.09 s | 11 | 0 | 0 | |
| Reminders | Apple | 5 | 0.10 s | 3 | 2 | 0 | Only 5 elements were showing when it was opened in the background |
| Stickies | Apple | 8 | 0.08 s | 6 | 2 | 0 | |
| Stocks | Apple | 15 | 0.10 s | 15 | 0 | 0 | |
| TextEdit | Apple | 75 | 0.97 s | 13 | 4 | 0 | |
| Weather | Apple | 44 | 0.30 s | 29 | 1 | 0 | |
| App Store | Apple | 62 | 0.12 s | 23 | 1 | 0 | |
| Microsoft Excel | Office | 96 | 0.14 s | 29 | 1 | 0 | |
| Microsoft Word | Office | 100 | 1.18 s | 16 | 14 | 0 | 33 of its 69 pressable elements ask first |
| Cursor | Electron | 63 | 0.37 s | 23 | 7 | 0 | |
| Discord | Electron | 400 | 0.58 s | 19 | 11 | 0 | The look stopped at its 400-element default; ask for more with max_elements |
| Figma | Electron | 24 | 2.3 s | 11 | 0 | 0 | The design canvas itself has no tree |
| Notion | Electron | 278 | 0.36 s | 16 | 14 | 0 | |
| Slack | Electron | 229 | 0.92 s | 24 | 6 | 0 | |
| Visual Studio Code | Electron | 65 | 0.34 s | 11 | 0 | 19 | Still drawing its window during the check. A recheck once it had finished found all 30 sampled elements |
| Microsoft PowerPoint | Office | | | | | | Didn't answer within 45 s of its first launch |
| Preview | Apple | 1 | | | | | No document open, so there was nothing to read |
| Xcode | Apple | | | | | | Opened with no window in the background |

Electron apps were started with `--force-renderer-accessibility`, which
they need (see the README).

## Acting

On 18 November 2026 three apps were also driven for real over MCP: live
acts, then saving them as a tool, then calling the saved tool. All 19
checks passed.

| App | Done live | Saved tool, replayed with no model |
| --- | --- | --- |
| Calculator | All Clear (it asked first), 2, +, 3, =, each 0.7 to 1.6 s. The display showed 5 | `add_two_and_three` in 2.9 s, and the display showed 5 again |
| Dictionary | Typed "accessibility" into the search field, 1.8 s | `look_up(word)` with "ventriloquist", 5.4 s: the results keep redrawing, so vent waits out its settle time |
| TextEdit | Typed into a scratch document, 0.4 s | `replace_text(text)` in 0.3 s, and reading the document back matched |

The first run of this test found two problems, both fixed: a look left
out Calculator's result (unlabelled text with a value), and a saved
Dictionary tool refused to replay because the window's title had changed
to the word looked up. Saved tools now leave window titles out, as every
shipped pack does.

## Check your own app

```bash
vent doctor "Your App" --report
```

It looks at the app the way an agent's look would, prints how ready it
is for live mode, and ends with a row for the table above. The row holds
counts and version numbers only, nothing from the app's content, so it's
safe to paste into an issue. Open the app first and let it finish
loading; numbers taken while an app is still starting are worse.

[Open an issue](https://github.com/ShaanGS/ventriloquist/issues/new) with
the row, and say what kind of app it is, to add it here.

## What this doesn't show

- It's one Mac and one run. Apps you sign in to, like Slack and Notion,
  show more once you're signed in and a workspace is open.
- Looking isn't acting. Only the three apps under "Acting" were driven
  for real. A press can still be ignored by an app that draws its own
  controls, and only a real act shows that.
- Several apps gave worse numbers while they were still starting or
  syncing, when their tree changes between a look and an act. An act
  then fails rather than pressing something else: the element's role is
  checked, and an element wearing a label its anchor never saw is
  refused. Look again once the app has settled.
- Apps that keep your messages and mail (Mail, Messages, Outlook,
  WhatsApp) were left out of the sweep on purpose. The Outlook and
  WhatsApp example packs were made on them by hand.

## What the sweep changed

The first pass found four problems, fixed in the same change as this
page: acts on lookalikes refused instead of using the element the agent
picked; "Visual Studio Code" didn't find the app that runs as "Code";
Finder's file list made a look take over 20 seconds; and apps still
starting got a raw AXError instead of "look again in a few seconds".
