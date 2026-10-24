# Benchmarks against built-in computer use

Two comparisons with Claude's built-in computer use, run by the author on
one Mac through Claude Code, with the same model making the decisions on
both sides. Times and tokens come from the session log. These are small
samples, not a study; they show where vent helps and where it doesn't.

## A one-step task: TextEdit (2026-10-22)

Replace the text of a TextEdit document, then confirm it. Three rounds per
method, alternating.

| Per run, median of 3 | Computer use | vent before the fixes | vent with the tree-walk fix |
| --- | --- | --- | --- |
| Tool calls | 2 | 2 | 2 |
| Total time | 8.6 s | 8.9 s | 7.5 s |
| New context tokens | about 2,600 | about 410 | about 410 |

The first measurement went against vent: every call walked TextEdit's
whole accessibility tree, most of it the menu bar. Fixing that gave the
right-hand column. 0.2.2 also removed a 0.6 s wait from every write, which
brought a TextEdit write through Claude Code to about 1.1 s and a read to
about half a second. Even so, on a task this short the two are about even
on time, and most of it is the model thinking. vent's edge is the context:
it returns text, not screenshots.

## A multi-step task: Outlook triage (2026-10-23)

Six test emails in a folder of new Outlook for Mac 16.110.1, in a personal
Gmail account. For each one: read it, flag it if it needs a reply or has a
deadline, and file it into Receipts, Meetings or Reading. Run 1 used vent's
[Outlook example pack](../examples/outlook); run 2 used computer use in
full-screen mode (screenshots, zooms, clicks, typing). The folder was reset
between runs, and both were scored against a fixed answer key.

| | vent | Computer use |
| --- | --- | --- |
| Final result | 6/6 folders, both flags right | 6/6 folders, both flags right |
| Wrong actions on the way | 0 | 2 |
| Wall time | 1:59 | 2:31 |
| Model time | 67 s | about 103 s |
| Tool calls | 14 | 12 |
| New context tokens | 5,063 | 18,089 |
| Output tokens | 1,198 | 8,018 |

Computer use's two wrong actions came from Outlook's Move menu, which lists
recently used folders above Choose Folder... in an order that changes after
every move: two clicks moved an email into the folder it was already in.
After that it checked the menu before each click. vent's filing tools always
go through the folder picker and require exactly one match.

Caveats:

- One run each, so the times will move around between runs.
- The same model ran both sides and knew the answer key, so this compares
  doing the work, not judging the emails. (The demo video is a separate run
  by a fresh session that had never seen the answers; it also filed 6 of 6.)
- Computer use had a shortcut: the recent-folder list only existed because
  the vent run had just used those folders. On a fresh mailbox it would need
  the folder picker, with more steps.
- Most of vent's tool time is Outlook coming to the front and handing focus
  back, 4 to 6 s per flag or filing call.

Context tokens are the cache-creation tokens of the request that follows
each model response, counted once per response.
