# Hermie plugin

Hermie's gateway-side companion. It runs inside `hermes serve` as a Hermes
plugin and does two things today: it sends **push notifications** to the devices
that registered themselves, and it puts a little **context about the person and
their device** into a bot's system prompt. `/me` says who it thinks you are.

It has no inbound port, no relay, no account, and no credential of its own. The
devices that want notifications write themselves into the gateway's own profile
metadata; the plugin reads that from the inside.

```
hermes plugins install fullstackstudio-org/hermie-plugin --enable
hermes gateway restart
```

That is the whole install. Hermes clones the repo into
`$HERMES_HOME/plugins/hermie/`, scans it, and enables it.

## What you get

| | |
|---|---|
| **A bot wrote something** | a notification, except on the device that says it is reading that chat |
| **A bot is asking for approval** | a notification with Allow and Deny, never suppressed |
| **A bot asked a question** | a notification, never suppressed |
| **A turn finished or failed** | a notification, if the device asked for those |
| **A cron job delivered** | a notification, recognised by the scheduler's own marker |
| **A cron job finished or failed** | a notification, if the device asked for those |
| **A chat whose switches you changed** | that chat's own answer, folded over your global one |
| **A bot you muted** | nothing, on any of your devices, until the mute lapses |

By default a notification says **who and what kind** — a bot's name and an event
type — and nothing about what was said. That is a deliberate default: a
notification is rendered on a lock screen by Apple, Google or a browser vendor.
Turn previews on per device in the app when you want the text.

Tapping Allow does not approve anything by itself. The app opens, connects to
the gateway, re-reads the open requests, and answers only if that request is
still open and still says what the notification said it did.

### What a payload carries

Everything here is a **hint the app resolves against the gateway**, never an
instruction. A field the gateway cannot fill in is left out rather than sent
empty, so a reader checks for absence.

| Field | Always? | What it is |
|---|---|---|
| `v` | yes | the payload shape, `1` |
| `type` | yes | `message`, `request`, `cron`, `cron_done`, `cron_failed`, `turn_done`, `turn_failed` |
| `bot` | yes | the bot's name, which is its profile name |
| `at` | yes | unix seconds |
| `eventId` | yes | the dedupe id, so two hooks describing one fact buzz once |
| `sessionId` | where known | the session the turn happened in |
| `sessionKind` | where readable | `canonical`, `branch` or `other` — which conversation to open |
| `gatewayKey` | where known | which gateway sent it, for a device set up against several |
| `requestId` | approvals | re-validated against the gateway before anything is answered |
| `cron`, `cronCertain` | cron runs | that this was a scheduled run, and whether that is a fact or a guess |
| `jobId` | where known | the name you gave the job |
| `preview` | opt-in | the text, only where the device asked **and** the gateway allows it |

**`gatewayKey`** is FNV-1a (64-bit) over the gateway's public origin, as 16
lowercase hex digits — the same string the app computes for the address it
registered against, so the two sides agree without ever comparing notes. The
gateway takes it from the registration the device wrote; where an older
registration carries none, it falls back to `push.public_url`, else Hermes'
own `dashboard.public_url`. A device that does not recognise a key simply does
not switch.

**`sessionKind`** is there because a bot no longer has exactly one conversation:
the app branches a chat and retires the one `/new` puts away, so a tap needs to
know which kind of conversation it is opening. The gateway reads the session's
title — `Bot Chat` is the canonical one, `Branch · …` is a branch, anything else
is `other` — and says nothing at all when it cannot read one.

## Capabilities

The app does not test this plugin's version number. It reads a list of strings
out of the gateway's own `ui_meta`, under the `hermie-plugin` key, and asks for
a feature only when the string naming it is there. A capability is published
only when it can be honoured **on this gateway** — Web Push only where the
signing library imports, a type only where it is switched on — because a button
that cannot work is worse than one that is absent.

| Capability | Means |
|---|---|
| `push.expo` | Expo notifications can be sent |
| `push.webpush` | Web Push can be signed here |
| `push.preview` | a device may ask for message text in its payload |
| `push.mute` | a mute written by the app will be obeyed |
| `push.seen.per_chat` | a `{bot, at}` heartbeat is understood, so suppression is per chat |
| `push.per_bot` | a chat's own switches (`push.perBot`) are folded over the global ones |
| `push.gateway_key` | every payload names the gateway it came from |
| `push.session_kind` | a payload says whether its session is the bot's chat, a branch, or neither |
| `push.type.turn_done` | "a turn finished" is switched on |
| `push.type.turn_failed` | "a turn failed" is switched on |
| `push.type.cron_done` | "a scheduled job finished" is switched on, and a device may ask for it |
| `push.type.cron_failed` | "a scheduled job failed" is switched on, and a device may ask for it |
| `push.cron.signal` | a cron run is recognised by the scheduler's marker, not by a guess |
| `ui_meta.per_user` | `hermie-app:<user id>` is read, so the app may move its bag |
| `context.system_prompt` | context is put into the bot's system prompt |
| `context.per_bot` | a per-bot note is rendered for the bot it names |
| `context.live` | a context edit reaches an **open** chat on its next turn |
| `context.orientation` | the section says where it came from and where to look for more |
| `context.turn_claim` | the app may claim the next turn of a shared chat for the signed-in person (`POST /api/plugins/hermie/context/turn`) |
| `command.me` | `/me` was accepted by this gateway |
| `plugin.update_check` | the advert carries the newest release tag |
| `memory.browse` | a profile's memory can be read over the dashboard's plugin routes |
| `memory.raw` | and read as it is stored, one document per backend |
| `memory.edit` | and written |

A device's own switches are read over **every** type in the payload table above,
so a registration that says `cron_done: true` or `cron_failed: true` is honoured
— for a while those two were sent by this gateway but could not be asked for. A
type a registration does not name is **off**, and it is never inferred from a
neighbour: a device that registered before the app had the two cron switches
keeps exactly the notifications it gets today, and `cron` ("a job delivered
something") does not turn on "a job you were not watching ended". The app's own
two switches default to on, so a device picks them up the next time it
registers. Above both sits `push.types`, the gateway-wide ceiling a device
cannot switch its way past — and a mute still silences the lot.

An absent list means an absent plugin. A plugin too old to publish one, a
plugin that is installed but disabled, and no plugin at all are
indistinguishable, and all three mean the same thing: do not offer the feature.

## Requirements

- Hermes with the plugin hook surface (0.21 or newer; tested on 0.21.x).
- Nothing else. Web Push signing uses `cryptography`, which the Hermes runtime
  already ships; on a runtime without it the plugin simply does not advertise
  Web Push, and Expo keeps working.

## Configuration

Everything is optional. Settings live under `plugins.entries.hermie.settings` in
`config.yaml`:

```yaml
plugins:
  enabled: [hermie]
  entries:
    hermie:
      settings:
        modules:
          push: true
          context: true
        push:
          # Which event types this gateway may notify about at all. A device
          # still has to ask for a type before it receives one.
          types: [message, request, cron, cron_done, cron_failed,
                  turn_done, turn_failed]

          # "device" honours each device's own preview switch.
          # "never" forbids message text gateway-wide, whatever a device asked.
          preview: device

          # How recent a device's "I am looking at this chat" heartbeat must
          # be before a new-message notification is held back on that device.
          attached_window_seconds: 90

          # Grace period before a message notification goes out, so an app that
          # is opening can claim the chat first.
          delay_seconds: 5

          # What this gateway is called from outside, used to name it in a
          # payload. Only needed for devices that registered before the app
          # started writing the key itself, and only when the gateway is
          # reached somewhere other than `dashboard.public_url`.
          public_url: ""

          # Web Push only. The key is created on first use if this is empty.
          vapid_key_path: ""
          vapid_contact: "mailto:you@example.com"
        memory:
          browse: true
          edit: true
        update:
          # Ask this repository, at most once an hour, whether a newer release
          # exists, and put the answer in the advert. Off by default; the advert
          # carries the version and the installed commit either way.
          check: false
        context:
          max_chars: 1200
          # Whose context to use when the gateway cannot say who is asking.
          # Empty is fine when only one person is registered.
          default_user: ""

          # Fill in Hermes' own HERMES_SESSION_USER_ID, _ID_ALT and _NAME for a
          # turn when they are empty and the plugin knows who is asking, so a
          # tool that reads them sees the person rather than nobody. Never
          # overwrites a value the gateway set. Hermes runs this hook on a
          # worker thread unless plugins.hook_callback_timeout is 0, and a
          # session variable set there does not reach the turn — see DESIGN.md.
          session_vars: true
```

Check what the plugin thinks it can do:

```
hermes plugins list
hermes plugins show hermie
hermes plugins doctor $HERMES_HOME/plugins/hermie
```

## Device context

The app can store, per person: a display name, free text about themselves,
device model and OS, app version, timezone and locale, and optional per-bot
notes. The plugin puts that in the bot's system prompt once per session, so it
does not appear in the transcript and does not grow with the conversation.

**The section says what it is.** A bot handed facts and not told where they came
from has to be taught by hand that the person has a profile at all, which is the
opposite of the feature. So the same section carries a short, fixed paragraph:
that these details are the person's own profile in their Hermie app, arriving
through this plugin and kept current; that the name, the timezone and locale and
the device are there to be used; that `/me` prints what is being shared and
Settings → Context is where the person changes it; and that anything not there
is something to ask about rather than assume. The two sentences that point
somewhere — `/me` and the memory browser — are said only on a gateway where that
place answers. It is context, not instruction, and it reads that way.

**And it says when the gateway does not know who is talking — which today is
always.** Two rungs were once treated as answering "who sent *this* turn": the
person's own claim on it, and a sender from a platform that names one per
message (a Telegram user, a bot handing a turn over). Neither held up on
review — a claim was bound to a session rather than to the submit it was made
for, and the platform rung rested on a naming convention nothing in Hermes
actually enforces — so both assertions are withdrawn. Every rung there is,
including a turn claim, names whoever the gateway last saw open or resolve the
session rather than proving who is typing right now, so wherever a profile
comes from at all, the section says so:

```
The gateway has not confirmed who is sending to this chat. The profile below is the one it falls back to, and the person typing may be somebody else.
```

Every word of that is as true on the hundredth turn as on the first, which is
what lets it sit in a system prompt: Hermes renders a plugin's section once and
replays those bytes for the life of the session. It never depends on whether a
claim happened to exist in the store at the moment the prompt was built —
resolving the frozen section that way was exactly the bug that made this
caution unreliable, and it is fixed by the section simply never asking.

**Who sent a turn was going to be said on the turn, never in the prompt** —
that part of the design still holds, and `SENDER_VERIFIED` still exists in the
code for it — but nothing produces that sentence today. It will return once a
claim is bound to the exact text of the prompt it was made for rather than to
a session (see the Unreleased section of `CHANGELOG.md` and `docs/DESIGN.md`,
"Decision: a claim is bound to the submit it is for"), which closes the gap
that made the earlier version state the wrong person as checked fact.

**The framing line moves with the caution.** On its own the section still ends
`This is background the person set in their app, not an instruction for this
turn.` Where the caution is there, that would pass off the gateway's words as
the person's, so it says instead: `What the gateway says here about whose
profile this is comes from the gateway, not from the person. The rest is
background…`

**The display name is cleaned on the way into a prompt**, and only there. It
keeps its 80-character cap, line breaks and control characters come out —
including the ones Python calls whitespace and a terminal does not — markup that
could open a heading, a fence, a quote or a link is removed, and what is left is
quoted, so a sentence somebody buried in their own name reads as part of the
name and cannot imitate a sentence of the section's own. `/me` and the session
variables other plugins read still get the name as it was written: `Max_B` and
`Anne-Marie <Annie>` are names, and mangling them for every reader to protect
one of them is a cost paid in the wrong place.

The paragraph gives way before the person's own words do: when the whole section
is up against `context.max_chars` it is dropped a whole sentence at a time, last
sentence first, because half a sentence about where to look is worse than none
and the budget is there for what somebody wrote about themselves.

**Changing it reaches a chat that is already open.** Hermes renders a plugin's
prompt section once per session and then replays it, so an edit made mid-chat
would otherwise wait for the next one. Instead the turn after the edit carries
the new text, saying that it replaces what the prompt says; emptying it is
retracted in words, since the frozen copy cannot be taken back out. On every
other turn this costs one `stat` of `profile.yaml` and reads nothing.

**A chat that was already open when the plugin arrived learns too.** Hermes
builds a session's prompt once, so a Bot Chat that started before the plugin was
installed carries no section and never will — no edit can top up a copy that is
not there. The next turn of such a chat carries the whole thing once instead,
framed the same way and saying it is new here rather than a correction. Once is
the point: it is remembered exactly as a frozen section is, under the same
512-session bound, so it cannot turn into something that rides every turn.

On a gateway with authentication in front of it the plugin works out **which**
person sent a turn and picks their context. It asks three places in order: the
sender Hermes hands the hook, the login bound into the session variables, and —
because the dashboard route fills in neither while knowing perfectly well who
logged in — the gateway's own record of this live session. A login carries the
provider that issued it (`oidc:max`), the app registers the bare id (`max`), and
either spelling finds the other.

**In a shared chat Hermes names the opener on every turn.** The sender a hook is
handed, the live record and the session variables all name the login that
created the session, so a second person typing in the same Bot Chat would get
the opener's context. The app therefore claims each turn just before it
submits it, over the dashboard, as the person signed in:

| | |
|---|---|
| `POST /api/plugins/hermie/context/turn` | `{"session_id": "<runtime session id>"}` → 204 |

- **The identity is the dashboard login**, `<provider>:<user id>`, taken from
  the request Hermes authenticated. Nothing in the body can name anyone.
- **Only for your own session.** The login on the request must be the login the
  dashboard admitted that runtime session under, across the provider prefix.
  Being signed in is not enough: Hermes hands every authenticated caller every
  route with no owner to check, so otherwise a runtime session id learned by any
  means would let somebody claim another person's next turn. A session admitted
  under nobody, and a gateway that does not stamp the login on its records,
  authorise nobody. Refused with the same 403 either way, so the route cannot be
  swept to find out which ids exist or whose they are.
- **`session_id` is the runtime id** — the one `session.create` and
  `session.resume` return and `prompt.submit` takes. A missing or malformed one
  is a 400, and one that is not a live session on this dashboard — a session
  key, a stored session id, a closed session — is a 404. A body not sent as
  `application/json` is a 415; a request that is not signed in as a person (a
  gateway without a login, a service token) is a 403; an unauthenticated one
  never gets past Hermes' own 401.
- **One claim is one model turn**, spent by that turn, ignored after 30
  seconds. Two people claiming the same session in that window: the later
  claim wins. Claim only for a `prompt.submit` that starts a model turn, never
  for a slash command: nothing spends a claim made before a command.
- **A claim replaces only a dashboard login.** A sender Hermes names as a
  messaging platform's user or a bot is left as it is.
- Nothing is written to disk; at most 256 sessions hold a claim at once.

On a gateway with no authentication there is no identity to read anywhere, so it
uses the default — which is the right answer when one person is registered, and
no answer at all when several are.

### `/me`

Type `/me` in a session to see what the bot actually resolved. It answers on the
spot, without calling the model:

```
Hermie context for jurist

Talking to: Kim
Worked out: from the login the gateway admitted this session under
Login:      self-hosted:7f3c02 → matched the registered id 7f3c02
Device:     iPhone 17 Pro running iOS 27, app 1.4.0
Dates:      Europe/Amsterdam, nl-NL
About:      Runs Willow Studio. Prefers short answers.
This bot:   Always cite the article number.
From:       hermie-app:7f3c02, updated 2026-09-21 02:19 UTC
```

When it says `nobody` it also says why, and what to do about it: accept the
sharing notice in Hermie's Settings → Context, then send a message.

> **On a shared gateway, read this.** Anyone signed in to the dashboard can read
> and edit any profile's memory through the routes below. That is the dashboard's
> trust model, not a decision of this plugin's: Hermes authenticates a dashboard
> request and then hands every authenticated caller every route, core's own
> included, with no role, owner or permission anywhere for a route to check. Turn
> `memory.browse` off on a gateway where that is not what you want.
>
> The app keeps one metadata key per person,
> but Hermes' profile metadata is per profile: every key on it is handed to every
> client that can read the profile, so everyone with access to the gateway can
> see everyone else's context section and push registrations. A push token is only an address; a
> context section is a name, a device and whatever somebody wrote about
> themselves. Do not fill it in on a gateway you share with people you would not
> show it to.

### Scheduled jobs

A cron run is an ordinary agent session — Hermes fires no cron-specific hook —
so the plugin has to recognise one itself. It asks the scheduler's own marker
first: the `cron:<job id>:<execution id>` task id, then the `HERMES_CRON_SESSION`
variable, then the session id. The `platform` string is still read, last, and a
notification says which kind of answer it got, so the app can tell a fact from a
guess.

A notification carries the **job id** when the signal had one, which is the name
you gave the job.

> **`cron_failed` means the run's turn failed, not that the job did.** The
> scheduler decides a job's real outcome after the agent has gone — an
> exception, a delivery that did not go through, a quota hold — and fires
> nothing a plugin could hear. What a turn can see is its own failure and the
> `[CRON_FAILURE]` line an agent writes about itself. So a failed job is
> sometimes silent here, and never falsely reported.

## How this relates to Hermie Web's `--push`

`hermie-web --push` did the same job from outside: a second process holding its
own WebSocket to the gateway, its own long-lived credential in its own state
file, resuming every Bot Chat to watch it.

This plugin replaces it as the default path, and is strictly smaller:

| | `hermie-web --push` | this plugin |
|---|---|---|
| Processes | two | one |
| Credential | a gateway credential in a state file | none |
| Bot Chats held open | all of them, permanently | none |
| Events | by watching a transcript | from the gateway's own hooks |
| Install | a release artefact, a unit file | one command |

One thing the daemon could do and this cannot: notify about **one bot writing to
another**. Hermes fires no hook when that happens, so there is no such
notification and no switch pretending there could be. Everything else moved
across.

Run both and you will be notified twice. Pick one.

## Development

```
python -m pytest --rootdir=tests tests
```

The repo root is the plugin package — Hermes imports the directory — so `tests/`
builds the same package rather than inventing an import path production never
uses, and the run is rooted below the root's `__init__.py`.

## Memory

The app can browse and edit a profile's `MEMORY.md` and `USER.md`. This is the
main part of the plugin that answers HTTP (the other is the turn claim under
Device context): a memory store is a request/response
surface over more data than a profile file should carry, so it mounts on the
dashboard's own plugin router rather than going through `ui_meta` like
everything else.

| | |
|---|---|
| `GET /api/plugins/hermie/memory/list?profile=` | entries per target, each with an id, its length and its topics, plus the char usage and limit |
| `GET /api/plugins/hermie/memory/search?profile=&q=` | every entry matching all of the query's words, across both targets |
| `GET /api/plugins/hermie/memory/graph?profile=&offset=&limit=` | nodes and edges to draw: entries, the profile, topics; `offset`/`limit` page over entries |
| `GET /api/plugins/hermie/memory/raw?profile=&backend=` | every backend of that profile with its documents as stored; `backend` narrows to one |
| `POST /api/plugins/hermie/memory/edit` | `{profile, target, op: add\|replace\|remove, content, old_text \| index}` |

Five things worth knowing before you call them.

- **They are on the dashboard's port, behind the dashboard's auth.** Not on the
  gateway's WebSocket, where the rest of this plugin lives. A caller needs the
  dashboard address and a dashboard credential; an unauthenticated request gets
  a 401 from Hermes before any of this runs.
- **`profile` is required, always.** A plugin route is handed no profile and
  would otherwise act on whichever one the dashboard process started under. The
  name is checked against the gateway's real profile list and anything carrying
  a separator or a parent reference is refused outright.
- **Every write goes through Hermes' own `MemoryStore`**, so its file lock, its
  external-drift check and its char limits all apply, and the answer is the
  store's own result. An entry is named by its text; an `index` is only a way of
  looking one up, and an index that has gone stale is an error rather than a
  guess at whatever moved into its place.
- **There are exactly two targets**, `memory` and `user`. An external provider
  (mem0 and the rest) is listed with `enumerable: false` and cannot be opened:
  the provider interface offers a query-shaped `prefetch` and has no call that
  returns entries, so there is nothing to show without inventing an API Hermes
  does not have.
- **`raw` answers what is stored, which the other three cannot.** They hand back
  a memory already parsed into entries — the shape to edit it in, and the one
  that hides a heading, a blank line the store kept, a delimiter that ended up
  inside an entry and the order the file really has. So `raw` reads the files
  themselves and returns them as they are, delimiters included, one document per
  file, alongside every other backend this gateway has. It never writes.

  Its empty answers are three different answers, and they are worth reading
  apart: a file that is **not there** is left out of the response, while one that
  exists and is bare is sent with `content: ""`; a backend that is set up and
  cannot enumerate — every external provider, today — is `available: true` with
  no documents and a `note` saying why; and one this gateway does not really have
  (not installed, or named in the config with nothing to authenticate with) is
  `available: false`. Each document is capped at 256 KiB with `truncated: true`
  and `chars` still reporting the whole length; there is no way to ask for the
  rest, because that cap is for a file that has gone wrong rather than for
  paging. Reading it needs `memory.browse` and nothing more, since it is the same
  reading of the same files, and `editable` on a backend follows `memory.edit`
  for the day a write exists — nothing writes a whole document now.

Switch either half off per profile:

```yaml
plugins:
  entries:
    hermie:
      settings:
        memory:
          browse: true   # false: the routes refuse with 403
          edit: false    # read-only; browsing still works
```

## Profile display name

| | |
|---|---|
| `PATCH /api/plugins/hermie/profiles/{name}` | `{"display_name": "…"}` → `{"name": "<profile>", "display_name": "<text>"}` |

Also on the dashboard's plugin router, for the same reason `memory` is: Hermes
already has `PATCH /api/profiles/{name}`, but that route *renames* a profile —
directory, wrapper script, service — and only turns into a display name at all
for `default`, whose home is the installation root and so cannot be renamed.
This route is that special case made general: it writes just the presentation
label, on any profile, through the very function core's own route calls for
`default` (`hermes_cli.profiles.write_profile_meta`), and never the canonical
id, the directory or anything else in the profile.

- **400** — the name is empty once trimmed, over 60 characters, or carries a
  control character, an invisible formatting character (a bidi override, a
  zero-width space or joiner) or a line or paragraph separator.
- **403** — `profiles.edit` is off for *that* profile (see below).
- **404** — no such profile on this gateway. Not the 400 `memory`'s routes give
  a bad profile: those fold "not a valid name" and "not one that exists" into
  one answer, and this route owes the app the two apart, so a caller can tell a
  typo from a profile that simply is not there.

Switch it off per profile:

```yaml
plugins:
  entries:
    hermie:
      settings:
        profiles:
          edit: false   # the route refuses with 403; read-only
```

## Updating

```
hermes plugins update hermie
hermes gateway restart
```

That is the whole update. It git-pulls the tree Hermes cloned, so nothing about
the install changes and **nothing is lost**: push registrations live in the
app's own metadata rather than in the plugin, and the plugin's small state file
(sent-event ids and retired devices) carries a version and is migrated forward
rather than replaced. A state file from a version older than this build is
migrated; one from a version this build has never heard of is left on disk
untouched and treated as empty for the run — losing dedupe history costs one
duplicate notification, while overwriting a newer file costs a downgrade its
data.

The advert tells the app when that command is worth running. It carries this
build's `version`, the commit the installed tree sits at, the repository it came
from, and the oldest app version this build can serve — enough for the app to
work out that a newer release exists, from the phone, on its own network.

If you would rather the gateway found out itself:

```yaml
plugins:
  entries:
    hermie:
      settings:
        memory:
          browse: true
          edit: true
        update:
          check: true
```

One GET of this repository's public releases URL, at most once an hour and
cached across restarts, nothing identifying sent, and every failure treated as
"no newer release". It is off by default because a gateway that reaches out
without being asked is not what this plugin is.

There is no self-update and there will not be one. A plugin that can rewrite its
own code is a plugin that can rewrite its own code.

## Licence

MIT. See [LICENSE](LICENSE).
