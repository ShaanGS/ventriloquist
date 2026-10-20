# Recording the demo

The README needs one short GIF at the top, and the launch posts need a
slightly longer video. Both should show the claim the project makes: an
MCP client drives a real Mac app through compiled tools, each call takes
under a second, and nothing looks at the screen. This file is the plan,
so the demo can be re-recorded whenever a pack or the UI changes.

## What the viewer should see

1. A chat client (Claude Desktop, or Codex CLI) gets a plain request.
2. Tool calls with readable names appear (`code_show_search`,
   `code_search_workspace`, `textedit_write_document`).
3. The real app reacts right away, with no cursor moving.
4. The pack JSON, briefly, to show the whole interface is a file.

Skip exploration and compilation. They are slow and model-bound, and the
point of the project is the part that runs without a model.

## Setup

- Record on the Mac that recorded the bundled packs. Anchors are per
  machine, and a refusal mid-demo is accurate but makes a poor first
  impression. Run `vent verify Code` and `vent verify TextEdit` first;
  both should resolve every anchor.
- Use a clean desktop: plain wallpaper, Do Not Disturb on, no menu bar
  extras that show names or messages. Window titles get recorded too.
- Start VS Code with accessibility on, opened on a small public repo
  (this one works):
  `open -a "Visual Studio Code" --args --force-renderer-accessibility`
- Open a scratch TextEdit document. `write_document` replaces the text
  of whichever TextEdit document is frontmost, so close any others:
  `echo "" > /tmp/vent-demo.txt && open -a TextEdit /tmp/vent-demo.txt`
- Arrange VS Code and TextEdit side by side, with the chat client on top
  or on a second space.
- Configure the client with `vent mcp-config`, and grant it
  Accessibility. Restart it so the server starts trusted.
- Leave Slack out of a public recording unless it's a throwaway
  workspace: its sidebar shows channel and people names.

## Shot list for the README GIF (20 to 30 seconds)

| Time | On screen |
| --- | --- |
| 0 to 3 s | Chat client, prompt typed: "Open the Search view in VS Code and search the workspace for TODO, then write how many results there are into my TextEdit document." |
| 3 to 12 s | `code_show_search` and `code_search_workspace` run; VS Code's Search view opens and fills. |
| 12 to 20 s | `textedit_write_document` runs; the TextEdit window updates. |
| 20 to 26 s | Cut to the pack file in an editor, scrolled to one tool's steps. |

Keep real speed for the tool calls. If the model's thinking time is cut
for length, say "edited for length" in the caption.

## Recording and converting

Record with the built-in screen recorder (Shift-Command-5, "Record
Selected Portion") at the size the GIF will be shown, then convert. Both
tools come from Homebrew (`brew install ffmpeg gifski`):

```bash
ffmpeg -i demo.mov -vf "fps=12,scale=900:-1:flags=lanczos" frames/%04d.png
gifski --fps 12 --width 900 --quality 80 -o docs/demo.gif frames/*.png
```

Aim for under 5 MB so the README loads fast; lower `--quality` or the fps
if it's bigger. Then replace the demo comment near the top of the README
with:

```markdown
![An MCP client driving VS Code and TextEdit through compiled tools](https://raw.githubusercontent.com/ShaanGS/ventriloquist/main/docs/demo.gif)
```

The absolute URL keeps the image working on PyPI, which renders the same
README.

## The longer cut (60 to 90 seconds)

For X, LinkedIn, and a Show HN comment. Same setup, plus:

- Open with one line of text: "Computer-use agents look at the screen
  for every click. This compiles a Mac app into tools once, then replays
  them with no model."
- End on the install command and the repo URL.

Export as MP4 (H.264) and upload it natively to each site rather than
linking to a file.

## Terminal-only fallback

If a screen recording isn't possible, record the CLI path with
asciinema and convert it with agg (both on Homebrew): `vent doctor`,
then the two `vent run TextEdit` commands from the README. It shows less,
since the app changing is the interesting part, so treat it as a
stopgap.
