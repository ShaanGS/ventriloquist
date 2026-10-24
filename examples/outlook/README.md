# Outlook example pack

The pack behind the README demo, for new Outlook for Mac (recorded on
16.110.1, macOS 26). It was built by hand from anchors recorded on the live
app, not with `vent explore`, and it files mail into three folders that
exist only for the demo: Receipts, Meetings and Reading.

| Tool | What it does |
| --- | --- |
| `read_selected_message` | Reads the selected email: subject, sender line, received time and body. Read-only. |
| `flag_message` | Message > Flag > No Date. An email that is already flagged stays flagged. |
| `file_to_receipts`, `file_to_meetings`, `file_to_reading` | Message > Move > Choose Folder..., types the folder name, and moves the email only if exactly one folder matches. |

There is one filing tool per folder on purpose: the agent can only move
mail into folders you approved, and a typed folder name can never land in
a folder in some other account.

To try it:

1. Create folders named Receipts, Meetings and Reading in the account.
2. Copy `pack.json` to `~/.ventriloquist/packs/com.microsoft.Outlook/pack.json`.
3. Restart your MCP client. The tools show up as `outlook_read_selected_message`
   and so on.

It needs vent 0.2.3 or later, which reads Outlook's email body (a web view
with no value of its own). The flag and filing tools bring Outlook to the
front while they run, because Outlook ignores those presses in the
background. Anchors were recorded on one Mac, so a tool may refuse on yours
until you re-record it with `vent inspect` and `vent anchor`.
