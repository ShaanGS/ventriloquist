# Shared packs

Packs people have made for apps that don't ship with vent. Each folder
holds a `pack.json` and a README saying what the tools do, what they
never do, and which app and macOS version they were recorded on.

| App | Tools | Recorded on |
| --- | --- | --- |
| [Outlook](outlook) (new Outlook for Mac) | read the selected email, flag it, file it into a folder you list | 16.110.1, macOS 26 |
| [WhatsApp](whatsapp) | read the chat list and the open chat, leave a draft (never sends), move between chats, mark unread, archive | 26.37.76, macOS 26 |

## Add one to your Mac

From a clone of this repo:

```bash
vent add-pack examples/outlook
```

You see each tool's exact steps and approve them one at a time. Then open
the app and run `vent verify Outlook`: anchors are recorded on one Mac,
and a step that doesn't find its element here is reported, never
guessed. Restart your MCP client to see the tools.

## Share one

Make the tools, by hand or by doing the task once in
[live mode](../README.md#use-any-app-live) and saving it. Then open a pull
request that adds `examples/<app>/pack.json` and a README like the ones
here. [CONTRIBUTING.md](../CONTRIBUTING.md#sharing-a-pack) has the
checklist; the tests check every pack in this folder for window titles,
file names and email addresses left in the anchors.

Want a pack for an app nobody has done yet?
[Ask for one](https://github.com/ShaanGS/ventriloquist/issues/new?template=app-request.yml),
and describe the task you repeat.
