# Hermie plugin

Hermie's gateway-side companion. It runs inside `hermes serve` as a Hermes
plugin and does two things today: it sends **push notifications** to the devices
that registered themselves, and it puts a little **context about the person and
their device** into a bot's system prompt. `/me` says who it thinks you are. It
also carries Hermie's **web client**, which the dashboard serves from its own
static route (see "Web client").

It has no inbound port, no account, and no credential of its own. The devices
that want notifications write themselves into the gateway's own profile
metadata; the plugin reads that from the inside and reaches each device the way
it registered: through Expo's push service for the Expo app, through the
browser's own push service for Web Push, and through the Hermie push relay for
the native app on iPhone, iPad and Mac.

That relay is the one service in the chain the Hermie project runs. Only an
app's publisher can talk to Apple's push service for that app, so notifications
for the native app pass through a relay we operate, where before they passed
through Expo's. It needs no account. It stores a device address and counters,
never a notification. It sees the bot's name and display name, the kind of
event, a cron job's name, and the ids the payload carries (session, request,
event, gateway key), as well as the sending gateway's IP address and the timing
of each notification; message text never crosses it in the clear — today it
does not cross it at all, and once notifications are encrypted end to end it
will cross only as ciphertext the device alone can open. A gateway posts only to the relays on its own
allow-list, which by default is that one.

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
| **A bot needs a secret, a password or a code** | a notification that says which kind and nothing about it, never suppressed (on a gateway that reports these; see below) |
| **A bot asks you to confirm something** | a notification that opens the app, never one you can answer from the lock screen, never suppressed (on a gateway that fires `pre_confirm_request`) |
| **A background task finished** | a notification, if the device asked for finished turns (on a gateway that reports it) |
| **A passkey was added or removed** | a notification on every device of that person, whatever they muted or switched off (on a gateway that fires `on_passkey_change`) |
| **A request stopped being open** | a silent message that tells the device to take the notification down, to a device that said it understands one |
| **A turn finished or failed** | a notification, if the device asked for those |
| **A cron job delivered** | a notification, recognised by the scheduler's own marker |
| **A cron job finished or failed** | a notification, if the device asked for those |
| **A chat whose switches you changed** | that chat's own answer, folded over your global one |
| **A bot you muted** | nothing, on any of your devices, until the mute lapses |

By default a notification says **who and what kind** — a bot's name and an event
type — and nothing about what was said. That is a deliberate default: a
notification is rendered on a lock screen by Apple, Google or a browser vendor.
Turn previews on per device in the app when you want the text. A device reached
through the relay gets the bot and the kind of event either way, until
notifications are encrypted end to end.

Tapping Allow does not approve anything by itself. The app opens, connects to
the gateway, re-reads the open requests, and answers only if that request is
still open and still says what the notification said it did.

### Requests that are not approvals

A request notification is the same `type: request` with a different `method`, and
the method decides what it may say and offer:

- **Secure inputs** (`secret`, `sudo`, `vault.*`) say *which kind* of thing is
  wanted ("Needs a secret", "Needs a verification code") and never what: no
  variable name, no site, no command, and no preview text even on a device that
  asked for previews. They get no Allow or Deny, because a password is typed.
- **`confirm`** gets no Allow or Deny at either level. At `passkey` only the app
  can run the ceremony; at `plain` a tap on a notification would be the very
  thing the request exists to ask for, from a screen that may be locked. It has
  no text either: the gateway never tells a plugin what is being confirmed. At
  `passkey` the request is bound to one person and so is the notification: only
  that person's devices are asked, and a device in the legacy shared bag, which
  names nobody, is not one of them.
- **A passkey added or removed** is `type: security`. It reaches every device of
  that person whatever they muted, whichever types they switched off and
  whichever chat is open, and is never held back by `push.types`. The lock screen
  says "A passkey was added" or "A passkey was removed"; a passkey the person
  enrolled themselves after signing in again (the gateway's `via` is `self`) says
  "A passkey was added after a new sign-in". The credential's name appears only
  as preview text, on a device that asked for previews and a gateway that allows
  them.
- **A clearing push** follows an answer, a cancellation or a timeout. It carries
  no text and no category, and goes only to a registration row that says
  `clears: true`: a build that does not know the field would show it as a new
  request. Expo gets it as a content-available data message and Web Push as
  `{data}` alone with `data.clear` (a worker must show nothing for it). A relay
  row gets none yet.
- **Two registration-row keys steer this.** `clears: true` says the device
  understands a clearing push. `requestMethods: true`, on a Web Push row, says its
  worker reads a request's `method`; without it a `confirm` or a secure input is not
  sent to that row at all, because the worker shipped with the Expo web build adds
  Allow and Deny to every request. Approvals and clarifies go to every row.

Which of these a gateway raises depends on the hooks it fires. `pre_confirm_request`
and `on_passkey_change` are the fork's; `pre_server_request`,
`post_server_request` and `on_background_complete` are named in
[docs/DESIGN.md](docs/DESIGN.md) for the fork to implement and no gateway fires
them yet. The plugin listens to a hook only where the gateway's own list names it,
so on any other gateway nothing changes and nothing is logged.

### What a payload carries

Everything here is a **hint the app resolves against the gateway**, never an
instruction. A field the gateway cannot fill in is left out rather than sent
empty, so a reader checks for absence.

| Field | Always? | What it is |
|---|---|---|
| `v` | yes | the payload shape, `1` |
| `type` | yes | `message`, `request`, `cron`, `cron_done`, `cron_failed`, `turn_done`, `turn_failed`, or `security`, which is not a switch |
| `bot` | yes | the bot's name, which is its profile name; the notification's visible title is the profile's display name where it has one |
| `at` | yes | unix seconds |
| `eventId` | yes | the dedupe id, so two hooks describing one fact buzz once |
| `sessionId` | where known | the session the event happened in. For a `request` it is the **runtime** session id the open request is filed under, and is left out when this gateway cannot know it; for every other type it is the stored session id |
| `sessionKey` | requests | the stored id of the conversation a request is in, so a tap can open it |
| `sessionKind` | where readable | `canonical`, `branch` or `other` — which conversation to open |
| `gatewayKey` | where known | which gateway sent it, for a device set up against several |
| `requestId` | every request but a clarify seen through the tool hook | re-validated against the gateway before anything is answered |
| `method` | requests | `approval`, `clarify`, `secret`, `sudo`, `vault.unlock_prompt`, `vault.code`, `vault.save_login` or `confirm`; only an approval is posted under the `hermie.request` category, which carries Allow and Deny |
| `level` | `confirm` | `plain` or `passkey`; neither is ever offered as a notification action |
| `event` | background tasks | `background.complete`, on a `turn_done` |
| `change` | `security` | `added` or `revoked`; never which passkey |
| `clear`, `reason`, `replaces` | clearing pushes | `true`, why (`answered`, `cancelled`, `timeout`), and the `eventId` of the notification it withdraws |
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
| `push.relay` | a `transport: relay` row is delivered, and `https://push.hermie.dev` — the relay the Hermie apps use — is on this gateway's allow-list; `relayOrigins` in the advert lists every relay it posts to |
| `push.webpush` | Web Push can be signed here |
| `push.webpush.key` | this gateway's VAPID public key is in the advert as `webPush.publicKey`, and a browser subscribes with it (see "Web Push and its key") |
| `push.preview` | a device may ask for message text in its payload |
| `push.mute` | a mute written by the app will be obeyed |
| `push.seen.per_chat` | a `{bot, at}` heartbeat is understood, so suppression is per chat |
| `push.per_bot` | a chat's own switches (`push.perBot`) are folded over the global ones |
| `push.gateway_key` | every payload names the gateway it came from |
| `push.clear` | a request that stopped being open is followed by a clearing push, to an Expo or Web Push row that says `clears: true` |
| `push.clear.relay` | the same, to a relay row. **Not claimed yet**: the relay cannot carry a message that is not an alert |
| `push.request.confirm` | a `confirm` request raises a notification (this gateway fires `pre_confirm_request`) |
| `push.request.secure_input` | a secure input, and a clarify question with its id, raise a notification (this gateway reports its server requests) |
| `push.security` | a passkey change notifies that person's devices (this gateway fires `on_passkey_change`) |
| `push.background` | a finished background task notifies (this gateway reports it) |
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
| `web.client` | the web client in `dashboard/app/` matched its `build.json` at load and `modules.web` is on; the advert's `web` block says where it is |

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

### Web Push and its key

A browser subscription is made with one sender's VAPID public key, and its push
service refuses (403) a push signed with any other. So the advert carries this
gateway's key, beside `push.webpush.key`:

```yaml
webPush:
  publicKey: BNc…   # base64url of the uncompressed P-256 point, 87 characters
```

The key is loaded, or minted on a first run, when the plugin loads, and it is
published only when that worked: never when push is off, never where nothing can
sign, and never when the key file cannot be read (that file is refused, not
replaced, and said once in the log). A probe load (`hermes plugins validate`,
`doctor`) reads an existing key but never mints one. Minting writes the whole
file or nothing, and two processes minting at once end up with one key. It is
never rotated automatically; a new key is a gateway every browser has to
subscribe to again.

A `webpush` row may say which key it was made with, as `applicationServerKey`
(80 to 100 base64url characters; anything else reads as absent). A row that
names no key is tried, as every row was before. A row that names another key is
not sent to and is said once in the log, because its push service would refuse
it anyway.

A 403 from the push service retires the row in the plugin's own state
(`webpush-key`), like a 404 or 410, when it is about the key: the response says
so (Apple's `VapidPkHashMismatch`, FCM's "do not correspond to the credentials
used to create the subscription"), or the row names no key, which is every row
written for another sender's key. A retired row is not asked again until the
device writes it with a newer `updatedAt`, which a client does when it
subscribes again with the advert's key. A 403 for a row that names **this**
gateway's key, and does not say the key is wrong, cannot be a mismatch — Apple
answers `BadJwtToken` the same way for a `sub` it will not take (see
`vapid_contact`) — so that row stays live and the refusal is said once in the
log, with the push service's own words.

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
          memory: true
          # Advertise the bundled web client. Off withdraws the advert, not the
          # files: see "Web client".
          web: true
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

          # The push relays this gateway may post to, as https origins. A
          # device registered with any other relay is not sent to. Setting
          # this replaces the default; an empty list serves no relay at all.
          relay_origins: ["https://push.hermie.dev"]

          # Web Push only. Where the VAPID private key lives; empty means the
          # plugin's own data directory. Created when the plugin loads if it
          # is not there, published in the advert, never rotated.
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
you gave the job. It travels in the payload for the app and is never shown on
the lock screen.

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

### Continuous integration

Every pull request and every push to `main` runs `.github/workflows/ci.yml` on
GitHub-hosted runners. It needs no secret and asks for none.

| Job | Runs | Takes |
|---|---|---|
| `test` | the test suite on Python 3.11 against the package versions Hermes itself locks, a compile of every module, and a check that nothing was skipped for a reason that is not expected | about a minute |
| `guard-scan` | Hermes's plugin scanner, from the fork and from upstream, each at the commit named in `.github/scanner-pins.json`, over this checkout; the fork blocks on anything but `safe`, upstream is informational and fails only on `dangerous` | about a minute |
| `web-bundle-verify` | checks `dashboard/app/` against its `build.json` with the plugin's own rules and the import limits, checks the app repository out at the commit `build.json` names, fails unless that commit is on the app's `main`, rebuilds the client with the Node version the app pins (`npm ci`, `npm run client:build`) and compares every file of the rebuild, `build.json` included, with `dashboard/app/`. Passes with nothing to do when there is no `dashboard/app/` | a few minutes, most of it `npm ci` |

`guard-scan` is not a style check. Hermes scans the new version of this tree
before every `hermes plugins update` applies it, and a verdict short of `safe`
**refuses the update on that gateway** (a `caution` needs a person, and the
auto-update timer has none): the old version keeps running and nothing new
arrives until the tree passes. The job runs that same scan before a change is
merged. A daily run
(`.github/workflows/scanner-nightly.yml`) asks the same of the newest scanners and
only reports. `CONTRIBUTING.md` says how to run the scan on your own machine and
how to move a pin.

### Branch protection

`main` deploys itself to the gateways within about fifteen minutes of a push, so a
check that runs after the push is a report, not a gate. What makes the checks
binding is a rule on `main`, which only a repository admin can set (Settings,
Branches, or the API). The settings this repository is written for:

- **Require a pull request before merging.** Nothing is pushed to `main`
  directly, release commits included.
- **Require status checks to pass**, and require the branch to be up to date:
  `test`, `guard-scan` and `web-bundle-verify`. The last is what makes a client
  bundle the build of merged source rather than whatever a pull request put in
  `dashboard/app/`. Not the nightly run: it belongs to no pull request and
  cannot be required.
- **Do not allow bypassing the rule**, administrators included, and **block force
  pushes and deletion** of `main`.
- **Approvals**: one approving review is the right setting once a second person
  maintains the repository. With a single maintainer it makes every pull request
  unmergeable by its author, so leave it at zero and keep the review in the
  pull request itself: the checklist in `.github/PULL_REQUEST_TEMPLATE.md`,
  ticked, with the reviewer's comment.
- Under Settings, Actions: default workflow permissions **read**, and approval
  required before a first-time contributor's workflow runs.

The same rule as one API call, run by an admin. The checks are bound to GitHub
Actions (app id 15368, from `gh api /apps/github-actions`), so another app cannot
report a check of the same name and satisfy the rule:

```
gh api -X PUT repos/OWNER/hermie-plugin/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      { "context": "test", "app_id": 15368 },
      { "context": "guard-scan", "app_id": 15368 },
      { "context": "web-bundle-verify", "app_id": 15368 }
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0, "dismiss_stale_reviews": true },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

`enforce_admins: true` applies to the maintainer too: once it is set, their own
direct push to `main` is refused like anyone else's. The release flow in
`CONTRIBUTING.md` therefore goes through a pull request first, and only the tag
is pushed directly.

A web client bundle is imported only through a pull request, and the template
carries the checklist for one: only `dashboard/app/**`, the version and the
changelog change, `build.json` names a source commit on the app repository's
`main`, and `guard-scan` and `web-bundle-verify` are green. Reverting the import
commit is the rollback.

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

## Web client

The plugin carries a build of Hermie's browser client in `dashboard/app/`. The
plugin itself does not serve it: the dashboard serves every plugin's
`dashboard/` folder through its own static route, and this is a folder in it. So
there is no new listener and no new route, and the files sit behind the
dashboard's own sign-in like the rest of it.

```
https://<gateway>/dashboard-plugins/hermie/app/index.html
```

What is served: `index.html`, the hashed files under `assets/`, and
`build.json`; later builds add `manifest.json`, `icons/*`, `licenses.json` and a
service worker `sw.js`. The route serves only the extensions on the dashboard's
allow-list (`.js .mjs .css .json .html .svg .png .jpg .jpeg .gif .webp .ico
.woff2 .woff .ttf .otf .map`), never this plugin's Python, and answers
`Cache-Control: no-store` for each file.

Who gets them:

| Caller | Gated gateway (sign-in on) | Ungated gateway |
|---|---|---|
| Not signed in | `302 /login?next=…` for every file | the static files, the same exposure as the dashboard's own bundle; every `/api/*` call the client makes still needs the session token |
| Signed in | the file, `Cache-Control: no-store` | the file |

The client holds no credential of its own. On a gated gateway it uses the
dashboard's own `HttpOnly` cookie session and its sign-in page; every API call it
makes is checked by the gateway, exactly as the dashboard's are.

**It runs on the same origin as the dashboard, and that is the central fact
about it.** A script-injection bug in the client would act with the signed-in
person's session against every `/api/*` route of the gateway: its configuration,
its files, its terminal, every profile. That is operator access to the gateway
host. The client is built to leave no path for it (no raw HTML from bot output,
a content security policy in its document, no remote images), and `SECURITY.md`
says what is and is not claimed.

### The integrity check

`dashboard/app/build.json` names the app repository and the 40-character commit
the build was made from, the client version, and the size and SHA-256 of every
file. When the gateway loads the plugin, it checks the folder against it, once:
every listed file present with exactly that size and hash, nothing unlisted,
nothing outside `app/`, no symbolic link, no extension the dashboard would not
serve. The check gives up after 200 files or 8 MB, and it never runs on a hook.

Only when it passes does the advert carry the capability `web.client` and a
block that says where the client is:

```json
"web": {
  "path": "/dashboard-plugins/hermie/app/index.html",
  "version": "0.2.0",
  "commit": "7013e53b9308",
  "files": 35,
  "bytes": 1088140
}
```

When it fails, both are left out and the gateway's log has one warning naming how
many files differ. A plugin tree without `dashboard/app/` (an older checkout, a
partial clone) loads as usual and advertises no client.

In CI, `web-bundle-verify` goes further: it checks the app repository out at the
commit `build.json` names, requires that commit to be on the app's `main`,
rebuilds the client and compares every byte. A bundle that is not the build of
reviewed, merged source cannot be merged here.

### `modules.web: false`

Withdraws the advert: no `web.client`, no `web` block, `modules.web: "off"`, and
the files are not checked at all. The apps read that as "this gateway does not
offer the web client", and the client is meant to decline to run when it reads
`off` there.

It does **not** remove or block the files. The dashboard serves whatever is in
the plugin's `dashboard/` folder to whoever it lets in, and this switch is not
part of that decision. It is a courtesy, not a boundary. To make the files
unreachable, uninstall or disable the plugin, or delete `dashboard/app/` from the
installed tree (an update puts it back).

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
