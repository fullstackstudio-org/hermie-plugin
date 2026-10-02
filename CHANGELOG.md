# Changelog

Notable changes per release. Capabilities are listed by the string the app tests
for, because that is what the app tests for — a version number here is for
people.

## Unreleased

### Added

- **`push.relay`: notifications reach the native Apple apps through a push relay.** A device running the
  native Hermie app on an iPhone, iPad or Mac registers with a relay and writes a row with
  `transport: relay`, its `handle` there and a `secret` that can send to that one device. This gateway
  now delivers to such rows by posting to the relay beside Expo and Web Push, which are unchanged.
  Requests are kept small — at most twenty messages and 7,680 bytes each, a conservative limit of
  the plugin's own that fits under every request cap the relay has had, and one handle once — and a
  request the relay refuses as a whole (400, 413) is sent again one message at a time, so a bad row
  costs only itself; when every message is refused on its own as well, the relay is left alone for a
  minute. A `gone` answer retires the row the way Expo's `DeviceNotRegistered` does. `retry` and
  `limited` are tried once more after the wait the relay asked for, unless it asked for more than
  30 seconds; a relay that failed twice, or a handle over its limit, is then left alone until its
  time, without a request — at most an hour for a relay and a day for a handle, whatever it asked
  for. The sender thread waits at most once per relay per notification. Mutes, per-chat switches, the 90-second `seen` window and the
  24-hour dedupe apply exactly as they do to every other transport.
- **The gateway posts only to relays on its own allow-list.** `push.relay_origins` defaults to exactly
  `https://push.hermie.dev`; an operator who runs their own relay names it there, which replaces the
  default. A row naming any other origin is not sent to, and is reported once in the log — a row is
  input, and posting wherever it points would let anybody who can write one aim the gateway at an
  address of their choosing. Requests go over https only, follow no redirect, use a proxy only when
  one is set in the environment, and time out after ten seconds. Neither the send secret nor a whole
  handle is ever logged.
- **One person's bad relay rows cannot spend everybody's allowance.** The relay counts `gone`
  answers — an unknown handle, a wrong secret — against the gateway that asked. After three in an
  hour for one person's rows, that person's rows that have never been delivered to are not sent
  until the hour has passed; rows that have been delivered to keep being served, and nobody else's
  rows are touched. A handle and secret the relay called `gone` are remembered and not sent again,
  even when a row is written again with them.
- **The advert says which relays this gateway posts to.** `relayOrigins` lists the allow-list,
  so an app can check its own relay before it moves a device onto a relay row. `push.relay` itself
  is claimed only while `https://push.hermie.dev` — the relay the Hermie apps register with — is on
  the list: an app that switched on seeing it, in front of a gateway that posts elsewhere, would go
  silent.
- **No message text crosses the relay.** A relay row is sent the bot's name and the kind of event,
  whatever its own `preview` says, until notifications are encrypted end to end to a key only the
  device holds. A row may already carry that key (`enc`); it is read and kept, and does not change
  this yet.

### Changed

- **A notification is titled with the bot's display name.** Where the profile has a `display_name`
  (the label `PATCH /api/plugins/hermie/profiles/{name}` sets), every transport shows it as the
  title instead of the profile name. `data.bot` is still the profile name, which is what a tap is
  resolved against. A label that is empty, longer than 60 characters or has control or formatting
  characters in it is ignored.
- **A display name may not carry invisible formatting characters.** `PATCH
  /api/plugins/hermie/profiles/{name}` now also refuses, with a 400, a bidi override (which makes a
  name read backwards), a zero-width space or joiner, and a line or paragraph separator — Unicode
  categories Cc, Cf, Zl and Zp. An emoji built with a zero-width joiner is refused with them.

### Fixed

- **An unloaded plugin starts no second sender.** A notification offered after the push module was
  stopped found no sender and started a new one while the old one could still be delivering. A
  stopped module now takes no more work.
- **A scheduled job's id is no longer shown on the lock screen.** It was appended to the body of
  every cron notification ("A scheduled job failed: <job>") on every transport. The push contract
  says `jobId` is carried, never shown: it still rides in the payload, and the body now names only
  the kind of event.
- **An approval arrives with its Allow and Deny buttons again.** Expo messages were posted under the
  bare type (`request`) as their category, which no app ever registered, so the buttons never
  appeared. An approval that names its request is now posted under `hermie.request`, the category
  the app registers; nothing else carries a category, because nothing else has an answer a button
  could send. The Android channel stays the type name.
- **A request says which kind it is.** Its payload now carries `method` — `approval` or `clarify` —
  which is how a sender decides whether the buttons apply. The request id still travels as
  `requestId`.
- **Web Push carries the payload where the browser looks for it.** The message was flat, and the
  app's service worker reads the payload under `data`, so it found no bot to open. It is now
  `{title, body, data}`, the same shape Hermie Web sends.

## 0.9.0 — 2026-09-24

### Removed

- **The bot is no longer told anything the app wrote about the person or their device.** The app has
  stopped sending it — the whole "Context about you" screen is gone on its side — and this plugin now
  ignores that payload outright rather than waiting for every device to update: a name, an "about me",
  a device model and OS, a timezone and locale, and a per-conversation note are none of them read from
  `ui_meta` any more, on any app build. `context.system_prompt`, `context.per_bot`, `context.live` and
  `context.orientation` are dropped from the advert to match — they claimed a feature that can no
  longer be honoured on any gateway. `/me` no longer prints a profile either; it now reports only the
  login the gateway resolved for the turn, which rung answered, and whether this build treats that rung
  as confirmed. On a gateway without the fork's own identity binding, that profile was the only thing
  telling the bot who it was talking to, so there the bot now knows only the login the gateway itself
  resolved for the turn, if any.

### Changed

- **Working out who sent a turn is unaffected.** Working out who the gateway thinks sent a
  turn — the hook's sender, the live session record, the session variables, a claim made over the
  dashboard — still works exactly as before; only the profile that used to be attached to that answer
  is gone. `context.turn_claim` is still advertised.
- The `context.max_chars` and `context.default_user` settings are gone; there is no more rendered
  section for either to bound or default. `context.session_vars` still fills in
  `HERMES_SESSION_USER_ID`, but no longer has a profile to fill `_ID_ALT` or `_NAME` from — it blanks
  them instead of leaving a stale name standing when the sender changes.

## 0.8.1 — 2026-09-23

### Changed

- **The bot can say when the gateway has not confirmed who is talking to it —
  the other half, stating who *has* sent a turn, is withdrawn.** The module
  resolved a rung all along and the rendering threw it away: a person named by
  their own turn claim and a person picked out of the app's `default` rendered
  byte for byte the same, and every section ended by saying it was background
  the person set in their app and not an instruction — right about what
  somebody wrote about themselves, and a reason to discount the one thing in
  the section a model could have relied on. An attempt at both halves shipped
  and was reverted before release: a turn claim was bound to a *session*
  rather than to the submit it was made for, so one left over from a slow
  agent build could be spent by a turn it was never made for, and a hook
  sender the dashboard did not admit was trusted on the provider-registry's
  own `name`, which nothing in Hermes actually pins to the login on the
  ticket. Neither proved what it needed to. See `docs/DESIGN.md`, "Decision:
  a claim is bound to the submit it is for", for the replacement (binding a
  claim to `sha256` of the exact prompt text), which is future work.

  **Which rungs answer "who sent *this* turn": none, today.** `VERIFIED_RUNGS`
  is empty and `asserted_sender` answers `""` for every rung there is. The
  hook's own `sender_id`, the live session record, the session variables and a
  turn claim all name whoever *opened* the session, on every turn of it, so on
  a shared chat they may name somebody who left hours ago — a claim included,
  until it is bound to the exact submit. A sender from a messaging platform
  that names one per message is real, but this plugin's own claim mechanism
  neither confirms nor doubts it, so it produces no caution and no assertion
  either.

  **Which scope a sentence belongs to.** Hermes renders a plugin's prompt
  section once and replays those bytes for the life of the session, so nothing
  in it may say "this turn" — and, independent of whether the turn-scoped
  assertion below ever fires, the section no longer asks the claim store to
  decide its own caution. A claim answers for a submit; the section renders
  before any turn of the session has run, so a claim sitting in the store at
  that moment proved nothing about that render, and resolving through it there
  is what let a dashboard session's caution depend on whether one happened to
  exist when the prompt was first built. The section now carries the cautious
  half, worded to be as true on the hundredth turn as on the first, wherever a
  profile resolves at all: `The gateway has not confirmed who is sending to
  this chat. The profile below is the one it falls back to, and the person
  typing may be somebody else.` The turn-scoped sentence — `The gateway
  verified that this turn was sent by the person signed in as
  <provider>:<user id>.` — and the code that would say it beside the message it
  is true of both still exist; nothing calls them today. On a gateway that
  confirms nobody, neither line is ever added.

  That sentence would name the login and not the person: a login is minted by
  the gateway, so the one line a model would be told to rely on holds nothing
  anybody typed, and it is what makes a claim checkable against `/me` or the
  log. It would need no profile either, so it would be said even for a login
  the app has never heard of. Where the section carries the caution, the
  framing line says that line is the gateway's rather than the person's.

  The display name is now cleaned on its way into a prompt — cap kept, line
  breaks and control characters out (including the ones Python counts as
  whitespace and a terminal does not, and the bidi overrides that reorder what
  is drawn), markup that could open a heading, a fence, a quote or a link
  removed, and what is left quoted, so a sentence buried in a name reads as part
  of the name and cannot imitate a sentence of the section's own. `You are
  talking to Ana.` is therefore now `You are talking to "Ana".` It is cleaned
  only there: `/me` and the `HERMES_SESSION_USER_NAME` shim still get the name
  as written, because `Max_B` and `Anne-Marie <Annie>` are names.

  How a person was resolved is kept out of the per-session record, which answers
  "has this chat been told this about this person?". It swings between turns —
  one is claimed, the next is not — and comparing it would read every swing as
  an edit and announce it in a note beginning "The person has changed this".

- **`POST /api/plugins/hermie/context/turn` now checks that the session is
  yours.** It checked that the runtime id was live and that the caller was
  signed in as somebody, and nothing tied the two together — so any signed-in
  user who learned another user's runtime session id could claim that session's
  next turn, re-claiming inside the 30-second window. The login on the request
  must now be the login the dashboard admitted that record under, compared
  across the provider prefix. A session admitted under nobody, and a gateway
  that does not stamp the login on its records, authorise nobody. All three are
  the same 403, so the route cannot be swept to learn which runtime ids exist or
  whose they are.

- **What became of a turn claim is now in the log**, which nothing said before,
  so a gateway where the feature had quietly stopped working looked exactly like
  one where nobody had claimed anything. Every line begins `hermie: turn claim`:
  `spent`, `refused` (with why it may not stand in for the turn) and `discarded`
  at `info`, `absent` at `debug`, because a turn with no claim is every turn on
  every gateway whose app does not claim. A line carries the provider half of a
  login (`oidc`, `basic`) and a short digest of the runtime session id — never
  the id itself, which is what somebody would need to aim a claim, never the
  user half of a login, never a name, and nothing from the message. The store's
  shape number is unchanged, so both copies of the plugin go on sharing one
  store.

## 0.8.0 — 2026-09-22

### Added

- `context.turn_claim` — **the person typing in a shared chat gets their own
  context, not the opener's.** Hermes names the login that created a session to
  every hook for the life of that session: the hook's `sender_id` is set once
  from the record's `auth_user_id`, a second window leaves the record naming
  nobody new, and the turn thread names nobody at all. So every rung the plugin
  had answered "whoever opened the chat", on every turn, whoever typed it.

  The app now claims the turn just before `prompt.submit`, with
  `POST /api/plugins/hermie/context/turn` and `{"session_id": "<runtime session
  id>"}`, and `pre_llm_call` asks for a claim before it asks anything else. The
  identity is the dashboard login the request was authenticated as, spelled
  `<provider>:<user id>` like the gateway spells it; nothing in the body can
  name anyone. 204 on success, 400 for a missing or malformed id, 404 for an id
  that is not a live runtime session on this dashboard (a session key or a
  durable id is refused, never stored), 415 for a body not sent as JSON, 403
  for a request that is not signed in as a person. A claim is spent by the one
  model turn that uses it and ignored after 30 seconds; two claims on one
  session inside that window leave the later one standing. The turn is matched
  by the runtime id Hermes binds for it, and only where none is bound by the
  durable key and agent session id the route read off the live record. A claim
  replaces a hook sender only when that sender is spelled as a dashboard login,
  so a messaging platform's user or a bot is never overridden, and it stands in
  for an empty sender only on a turn with a runtime id bound. Building the
  prompt (`render_section`) never reads the claim store at all — a claim
  answers for a submit, and the section renders before any turn of the session
  has run, so a claim sitting there at that moment was never evidence about
  that particular render; only `pre_llm_call`, firing for the actual turn,
  spends one. `/me` names the claim as the rung that answered where it can
  find one, and spends it too. The app claims for a
  `prompt.submit` that starts a model turn and never for a slash command, which
  is what keeps a claim from outliving its turn. The hooks and the dashboard
  run separate copies of the package, and the store is shared between them by
  its shape number, never by class.
  At most 256 claims are held, in memory, in one store both copies of the
  plugin share; nothing is written and nothing leaves the process.

## 0.7.2 — 2026-09-22

### Added

- `profiles.display_name` — the app can rename a bot's profile label from its
  own settings screen. `PATCH /api/plugins/hermie/profiles/{name}` with
  `{"display_name": "…"}` writes only that key, in that profile's own
  `profile.yaml`, through the same `write_profile_meta` Hermes' own
  `PATCH /api/profiles/{name}` calls for the `default` profile — never the
  canonical id, the directory or anything a real rename would move, so it works
  the same way on every profile rather than only on `default`. Refused with 400
  for a name that is empty after trimming, longer than 60 characters or
  carrying a control character; 404 for a profile this gateway does not have;
  403 per profile via the new `profiles.edit` setting, off means read-only the
  same way `memory.edit` does.

## 0.7.1 — 2026-09-22

### Added

- `memory.raw` — **a backend can be read as it is stored.** The three browsing
  routes all answer a memory the store has already parsed into entries, which is
  the shape to edit it in and the wrong shape for the question "what is in
  there": a heading, a blank line the store kept, a delimiter that ended up
  inside an entry and the order the file really has are none of them visible in a
  list of rows. And for an external provider there was nothing at all — `list`
  names it and marks it `enumerable: false`, which says it exists and says
  nothing about what it holds. `GET /memory/raw?profile=[&backend=]` answers one
  card's worth per backend: the built-in one with each file as it is held,
  delimiters included, and every other one with either its documents or the
  reason it has none.

  The empty answers are the part that had to be got right, because collapsing
  any two of them tells somebody their memory is empty when it is not. A file
  that is **absent** is left out of the answer, while one that exists and is
  bare is sent with `content: ""`. A backend that is set up and cannot be listed
  is available with no documents and its own sentence about why — which is every
  external provider, since the interface offers `prefetch(query)` and nothing
  that returns what it holds. A backend this gateway does not really have, either
  not installed or named in the config with nothing to authenticate with, is not
  available; `available` is therefore narrower on this route than on a `list`
  row, where it still means "the package imports", because that route reports
  what exists and this one is asked what is stored. A file that exists and cannot
  be decoded is named in the backend's note rather than shown as empty, since
  empty is a claim about its contents.

  It reads the files itself, from the directory Hermes resolves per call so the
  profile scope still moves it, and it writes nothing. Each document is capped at
  256 KiB with `truncated: true` beside the full `chars` — a ceiling on a file
  that has gone wrong, not a page, since there is no way to ask for the rest.
  Gated by `memory.browse` alone: it is the same reading of the same files, and a
  second switch for one permission is a switch somebody has to find before the
  feature works. `editable` follows `memory.edit` for the day a write exists;
  nothing writes a whole document today.

- `context.orientation` — the rendered section now says what it is. A bot was
  told the facts about the person and nothing about where they came from, so
  somebody had to sit and explain the plugin to their bot before the feature
  worked at all. The section carries a short fixed paragraph instead: that these
  details are the person's own profile in their Hermie app, arriving through
  this plugin and kept current; that the name, the timezone and locale, and the
  device are there to be used; that `/me` prints what is being shared and
  Settings → Context is where the person changes it; that earlier chats are in
  the profile's memory rather than here; and that anything missing is something
  to ask about. It is permissive throughout, because it is context and not
  instruction. The two sentences that point somewhere are said only where that
  place answers — `/me` needs the registration Hermes can refuse, the memory
  line needs the memory module switched on — which is the capability rule
  applied to prose. Written in one place, so every path renders the same
  paragraph.

- **A chat that was already open when the plugin arrived learns who it is
  talking to.** `context.live` tops up a frozen section that has gone stale, but
  a session whose prompt was built before the plugin existed has no section to
  top up and core will never build that prompt again — so the longest-running
  Bot Chat was the one place the person stayed a stranger. Its next turn now
  carries the whole section once, with a line saying it is reaching this chat
  for the first time and replaces nothing. Once is the point, since anything
  returned there rides the user message: it leaves the same record a frozen
  section leaves, under the same 512-session bound, even when nothing was said,
  so a gateway with nobody registered stops asking rather than resolving on
  every turn. A session that never had a section and then gets an edit is told
  that the newer copy beats what was said earlier *in the chat* — the system
  prompt, on that session, says nothing to beat.

- `push.session_kind` — a payload says whether its session is the bot's
  canonical chat, a branch, or neither, so a tap can open the right conversation
  now that a bot has more than one. Read from the session's title, the same
  signal and the same three titles the app classifies by, and advertised only
  where this gateway can actually read one. A session it cannot read carries no
  kind at all rather than a guess, and the title is read once per notification
  rather than cached, because a branch that gets promoted changes its title.

- `cron`, `cronCertain` and `jobId` now ride an **approval or a question raised
  inside a scheduled run** as well as the cron deliveries and turn endings that
  already carried them. They stay `request` notifications — somebody is still
  being asked — but "this is a job you are not watching" is the most useful
  thing a lock screen can add to a question. Neither hook carries a `task_id`,
  so the answer there comes from `HERMES_CRON_SESSION` or a `cron_…` session id.

- `push.per_bot` — the `push.perBot` overrides the app writes beside the
  registrations are read and folded over each device's own switches, by the same
  rule (and the same function name) the app's switch screen uses. The bag is
  partial on purpose: a type nobody overrode keeps following the global switch
  as it moves. A mute still outranks all of it — an override is a preference
  about a type, a mute is somebody saying no to the bot. The section version is
  not bumped, for the reason ADR-0016 gives: `v` is checked per row and an
  unreadable row is dropped, so a bump would unregister the device rather than
  protect the key.

- `push.gateway_key` — every payload names the gateway it came from, as FNV-1a
  (64-bit) over the gateway's public origin in 16 lowercase hex digits. A device
  can be set up against several gateways, and a notification saying only
  "researcher" leaves an app with two of them to choose between. The algorithm
  is specified rather than shared — it exists in the app's
  `packages/gateway-client/src/gateway-key.ts`, in Hermie Web's zero-dependency
  copy and now here — and all three pin the vector
  `https://gateway.example.com:8443` -> `bf796761db84e312`. The key is taken
  from the `gatewayKey` the device itself wrote on its registration, because
  that is the string that device will compare against and the two sides then
  agree by construction; a row written before the app carried one falls back to
  the new `push.public_url` setting, else Hermes' own `dashboard.public_url`.
  A row's claim is checked rather than copied, and a payload that can name no
  gateway simply carries no key — which is how every notification before this
  behaved.

- `memory.browse` and `memory.edit` — a memory browser for a profile's
  `MEMORY.md` and `USER.md`, served at `/api/plugins/hermie/memory/`
  (`list`, `search`, `graph`, `edit`). This is the plugin's first HTTP surface
  and the exception to the rule in DESIGN.md §1: `ui_meta` cannot carry it and
  the gateway's WebSocket has no memory method to borrow. It runs behind the
  dashboard's own authentication, which is all-or-nothing — any signed-in caller
  is treated as an operator, as on every core route — and the README says so in
  the shared-gateway warning. `profile` is required and validated against the
  gateway's real profile list; the store is opened under that profile's home
  through the public `set_hermes_home_override`, so Hermes' own file lock, drift
  check and char limits apply. External providers are listed with
  `enumerable: false`, because the provider interface has no call that returns
  entries.

- `plugin.update_check`, and an advert that says which build is installed —
  `version`, `maxContract`, `minAppVersion`, and a `source` naming the
  repository and the commit the installed tree sits at (read from `.git`, no
  subprocess, no network). That is enough for the app to say "a plugin update is
  available" without the gateway reaching anywhere. A gateway-side check of the
  newest release tag exists behind `update.check: true` — one GET, cached an
  hour across restarts, nothing identifying sent — and is off by default.

### Changed

- The docs now carry the exact shape of Hermes' own `PATCH /api/profiles/{name}`
  (DESIGN.md §8), which is how a display name is set. The plugin does not
  duplicate it: any authenticated dashboard caller can already reach core's
  route, and a plugin route could not have applied the per-user check that would
  have justified a second one.

- State is version 2, adding the update-check cache. `sent` and `retired` come
  through a migration unchanged; a state file from an unknown future version is
  still left on disk untouched.

- `push.type.cron_done`, `push.type.cron_failed` and `push.cron.signal` — a cron
  run is now recognised by the scheduler's own marker (the `cron:<job id>:…`
  task id, then `HERMES_CRON_SESSION`, then the `cron_<job id>_<stamp>` session
  id) rather than by looking for "cron" in a free-text `platform` string, which
  stays as the last resort. A notification carries the job id and says whether
  the signal was a fact or a guess. `cron_failed` covers the agent's own
  `[CRON_FAILURE]` line and a cron turn that failed; the scheduler's verdict on
  the job itself is decided after the agent is gone and fires no hook, so it is
  out of reach and the README says so.

- A device's own `types` are read over **every** type this plugin can send, so a
  registration that says `cron_done: true` or `cron_failed: true` is honoured.
  The reader and the sender kept separate lists of type names and the reader's
  was the shorter one, which left the two cron endings above sendable but
  impossible to ask for. There is one list now. A type a row does not name is
  still **off** and is never inferred from `cron` — no device starts receiving
  something it never asked for because a gateway was updated — and the app's own
  two switches, which default to on, reach a device when it next registers.
  `push.types` remains the gateway-wide ceiling and a mute still outranks all of
  it.

- `context.live` — a context edit made while a chat is open reaches that chat on
  its next turn. Hermes renders a plugin's system prompt section once per
  session and replays the bytes it persisted, and a plugin cannot ask for a
  re-render, so the per-turn hook now carries the change instead: it compares
  the app's `profile.yaml` stamp against the one taken when the section was
  frozen and, when the rendered text has actually changed, sends the new copy
  saying it replaces the frozen one. A cleared context is retracted in words.
  On a turn where nothing changed this costs one `stat` and no read.

### Fixed

- **A shared Bot Chat describes the person sending the turn, not the one who
  opened it.** The session variables are bound once, when a session is created,
  and were asked before the gateway's live session record; on a chat opened by a
  login the app has no row for, every later turn resolved to that login, found
  nothing and said nothing — while Hermes' own "User:" line went on naming the
  opener. A sender is now worked out per turn from the hook, then the live
  record, then the session variables, then the section's `default`. When that
  person is not the one the chat was told about, the next turn carries them
  once, with a line saying somebody else is sending this turn; a record that
  described nobody no longer stands in the way, and the first person who does
  resolve is introduced. A sender without a row gets nothing about anybody, and
  "the only registered person" answers only where the gateway named nobody at
  all. With `context.session_vars` on, `HERMES_SESSION_USER_*` is rewritten for
  the resolved sender when it names somebody else, so "User:" agrees with the
  section.
