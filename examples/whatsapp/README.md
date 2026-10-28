# WhatsApp example pack

Tools for the WhatsApp Mac app (recorded on 26.37.76, macOS 26, English
interface). It was built by hand from anchors recorded on the live app,
like the Outlook example. None of its tools can send a message: the
closest one leaves a draft in the message box, and sending stays a key
you press yourself.

| Tool | What it does |
| --- | --- |
| `list_chats` | Reads the chat list on screen: the unread totals ("Unread, 12 unread chats"), then a line per chat with its name, last message, time and unread count. It opens no chat, so nothing is marked read. Read-only. |
| `read_open_chat` | Reads the open chat: its name, then the messages on screen, one per line, with their sender and time. Read-only. |
| `draft_message` | Puts text in the open chat's message box, replacing what is there, and says which chat it went to. Never sends. |
| `open_next_chat`, `open_previous_chat` | View > Next Chat and Previous Chat, then the name of the chat now open. Opening a chat marks it read and sends read receipts if they are on. |
| `mark_open_chat_unread` | Chat > Mark as unread, for the open chat. |
| `archive_open_chat` | Chat > Archive, for the open chat. WhatsApp also unpins it, then usually opens the next chat, which marks that one read. Nothing is deleted. |

Every tool that acts on a chat reads the chat's name first and returns
it, so you can see which chat it touched. All of them work with WhatsApp
in the background, without bringing it to the front.

To try it:

1. Copy `pack.json` to `~/.ventriloquist/packs/net.whatsapp.WhatsApp/pack.json`.
2. Restart your MCP client. The tools show up as `whatsapp_list_chats`
   and so on.

It needs vent 0.3.1 or later: WhatsApp keeps its message text in
labels, which earlier versions don't read.

What it can't do:

- Open a chat by name. The chat list's search box doesn't take text
  written through accessibility. The New Chat picker's does, but every
  result in it carries the same identifier, so no anchor can name "the
  first result", and a search with no match leaves other contacts where
  the result would be. The way to another chat is `open_next_chat` or
  `open_previous_chat`.
- Read past the screen. The chat list and the messages are only there for
  the rows in view, about a dozen chats and ten messages.
- Filter the list to unread chats. The filter buttons carry no identifier
  and their labels change with every new message, so no anchor can hold
  them. `list_chats` still returns the totals.
- Use the menus while WhatsApp is minimized. A minimized WhatsApp window
  reports itself as a dialog, so the menu tools (next and previous chat,
  mark unread, archive) refuse and say the window is minimized. Reading
  and drafting work minimized.
- Read or act with no chat open. The chat tools refuse rather than read
  some other part of the window as the chat; `list_chats` still works.

WhatsApp's terms don't allow automated or bulk messaging, which is one
more reason the pack stops at drafts. Anchors were recorded on one Mac, so
a tool may refuse on yours until you re-record it with `vent inspect` and
`vent anchor`.
