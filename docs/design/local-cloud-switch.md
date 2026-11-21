# Design: the desktop's Local / Cloud switch and the group mode

Status: **approved 2026-11-20; built in full (no phases); extensions in
cloud mode included; recommendations Q1–Q11 accepted.**
Tracker row R691 ("Switch the desktop app between a local backend and a
cloud (Fleet) backend"). The work item is the `docs/TODO.md` entry "A person
uses their cloud VM from the desktop app, not only the local backend",
agreed on 2026-11-08 and written down in PR #15.

This doc is in igni-core because the TODO, `CLOUD.md` and `SECURITY.md` are
here. Most of the code lands in igni-app and igni-server, so each of those
PRs links back to this file rather than copying it.

Every "today" statement below was read from the code on 2026-11-20:

- igni-core origin/main `51aa1b89`;
- igni-server origin/main `997ba78`;
- igni-app origin/main;
- igni-sdk origin/main `80e29ee`;
- igni__cloudvm `10a8e5b` (crate `crates/gateway`).

§15 (extensions in cloud mode) was read on the same day against igni-core
`51aa1b89`, igni__cloudvm origin/master `17528d7` (crates `gateway`,
`node-agent`, `guest-agent`) and igni-app origin/main `b27c81bb`.

Paths are given relative to each repo.

---

## 0. Decisions at a glance

| # | Question | Decision |
|---|---|---|
| D1 | Discovery | igni server gets a new `GET /v1/me/cloud`. It answers **entitlement**: is there a cloud igni for this person, the gateway URL, the group mode and who owns the events. The gateway's existing `GET /v1/me` gains the **live VM state** (asleep, running, signed in). igni server never asks the gateway for state. |
| D2 | Auth hand-off | **The token never enters the webview.** The Tauri shell runs a loopback **relay**: windows connect to it exactly as they connect to a local backend (per-launch token, Origin check), and the relay dials `wss://<gateway>/v1/connect` with `Authorization: Bearer <session>` read from `~/.igni/credentials.json`. "Refresh" means re-dialling with the current token. The CSP stays loopback-only. |
| D3 | Switch UX / state | One app-wide choice, owned by the shell and remembered per account. It is a two-way control, *This computer · Cloud*, in the account menu, with a backend chip in the status line. Flipping re-points every window, the same way a backend restart does today. The local backend keeps running. |
| D4 | Group mode | A first-class column, `org_groups.mode ∈ {local, cloud, local_and_cloud}`, defaulting to `local_and_cloud` (today's behaviour). It is carried as a top-level `mode` in the signed group pack. It is enforced in four places:<br>• the shell, offline, from the verified pack;<br>• igni-core's run gate, offline;<br>• igni server: the producer feed marks a local-only person inactive, and the `vm-sign-in` grant refuses them;<br>• the gateway, which refuses and kicks inactive producers. |
| D5 | Cloud mode, phase 1 (*superseded 2026-11-20: extensions work in cloud mode, D12*) | Chat works: sessions, runs, approvals, models, attachments (uploaded as bytes). Hidden: extensions, the folder picker, New Window's folder choice, "open in editor" and "reveal" for paths, the hooks panel's file links. The cloud layer also refuses `pick_dir_native` on its own side. |
| D6 | Events | **igni server picks one owner per person** (`local` or `cloud`), so the two sides never race for the lease. Owner by mode: `local` → local; `cloud` → cloud; `local_and_cloud` → the person's last switch, stored server-side, `local` until they first switch. A desktop pull while the owner is `cloud` gets `409`. A gateway pull while the owner is `local` gets nothing. |
| D7 | Errors | Each error state gets one shell state and one banner. Waking is driven by the client, because the gateway sends nothing while it wakes a VM. `needs_sign_in` gets a **Sign in** button (the shell calls `POST /v1/sign-in`). An unreachable gateway retries with backoff and offers a way back to local. A mode change applies live (a kick, or a drain then a switch). |
| D8 | Contracts | One schema, `cloud-discovery.schema.json`, lives in igni-sdk `contracts/`. igni-server, igni-client and the shell are tested against it. The pack gains `mode`. The feed gains `inactive_reason`. **The client protocol does not change.** |
| D9 | Security | The session token stays in Rust memory and on disk at 0600, where it already is. The relay accepts only the webview's origins plus its own per-launch token. The gateway gains an Origin allow-list (no browser origins by default). TLS is always verified. |
| D10 | Model contract | **No model-facing text changes.** None of this touches prompts, tools, injected blocks or result formats. One trap is named in §11 so nobody falls into it later. |
| D11 | Phasing (*superseded 2026-11-20: one build, §13*) | Five phases, one PR per repo per phase, merged igni-server → igni__cloudvm → igni-core → igni-sdk → igni-app. Phases 1–3 come to about **25 engineer-days** plus about **4 days** of live E2E (§13). |
| D12 | Extensions in cloud mode | **Built with the rest.** The relay dials with `?pages=1`; the node agent attests that stream with `X-Igni-Gateway: pages`; the cloud igni honours `caller` only on a connection with that mark. Page files come through a new gateway route `GET /v1/pages/{extension}/{path}` and a loopback origin per extension in the shell, so the CSP stays loopback-only. The host object model and `extension_service_token` ride the client protocol unchanged (§15). |

---

## 1. What exists today

### 1.1 The desktop

- **One backend per app.** The shell's supervisor
  (`clients/tauri/src-tauri/src/backend/supervisor.rs:45-81`) does one of
  three things:
  - attaches to `<project>/.igni/backend.lock`;
  - refuses a backend that speaks another wire version;
  - spawns `python -m igni_core.backend --ws-port 0 --project-dir <dir>`
    and waits for `{"status":"ready","ws_port":N}`.

  There is no health check and no automatic restart. File › Restart
  Backend re-points every window.
- **How a window finds its backend.** The window URL carries it:
  `index.html?ws=ws%3A%2F%2F127.0.0.1%3A<port>&host=tauri&…#igni_ws_token=<t>`
  (`backend/conn.rs:67-84`). The web client offers the subprotocols
  `igni.v1, igni-token.<t>` (`clients/web/src/protocol/wsEndpoint.ts:37-63`).
  It keeps one WebSocket per window
  (`clients/web/src/chat/windowServices.ts:77-88`), and on every `connected`
  it re-binds and hydrates the window model
  (`chat/useSessionBinding.ts:49-52`, `chat/sessionSwitcher.ts:190-217`).
- **Sign-in lives in the local backend.** The `login` RPC runs the
  browser hand-off: portal `/cli-auth?port=N`, then
  `POST /v1/auth/exchange`. The result is a **30-day session token**
  (`token_use=session`, `aud` = the issuer, no refresh token) in
  `~/.igni/credentials.json`, mode 0600. The shell already reads that file
  for `sub` (`src/group_pack/local_state.rs:8-27`). No Tauri command hands
  any token to the webview.
- **The shell verifies the signed group pack itself.**
  - Where: `src/group_pack/mod.rs`, which checks RS256, `typ`, `iss`,
    `sub`, `version` and the pins.
  - What it reads: four settings, before any backend exists.
  - Parsing is lenient: unknown fields are ignored (`policy.rs`).
- **CSP is loopback-only.** `tauri.conf.json:16` names only loopback
  sources, and `tests/config_contracts.rs:79-146` fails if any source
  points off this machine.
- **The client id key.** It is `ember-code:client-id`, with `:<label>`
  appended for every window but `main` (`clients/web/src/clientState.ts:49,72-75`).
- **Nothing about cloud, Fleet or the gateway exists** in igni-app.

### 1.2 The gateway (igni__cloudvm `crates/gateway`)

- **The connect route.** `GET /v1/connect[?sign_in=1]` takes the bearer
  in one of two ways (`auth.rs:284-303`):
  - an `Authorization: Bearer` header;
  - **or** the subprotocol `igni.bearer.<t>`, sent next to `igni.v1`.

  It echoes only `igni.v1` (`api.rs:140`).
- **Token checks.** RS256 against igni server's JWKS, with `iss` = `aud` =
  the issuer and 30 s leeway. A person's token must be `token_use=session`,
  `kind=person`, with an `org` (`auth.rs:244-280`). **The desktop's
  session token is exactly that.**
- **Sign-in.** On a person's first connection, or with `?sign_in=1`, the
  gateway passes that same session token to igni in the VM
  (`vm_sign_in{session_token, organisation}`, `control.rs:100-125`). igni
  exchanges it for the VM credential.
- **While a VM wakes, the gateway sends nothing.** It buffers up to 256
  client messages, holds for at most 20 s, then closes with 1011. igni's
  `welcome` is the first frame the client sees.
- **Moving a session.** When a session moves between nodes, the client is
  **not** told to reconnect. The gateway reconnects behind the same socket
  (up to 3 times). It answers in-flight `rpc_request`s with an error, and
  igni sends a **new `welcome`** on the same socket.
- **Token expiry.** `exp` is checked only at the upgrade. Nothing closes a
  socket when its token expires.
- **Close codes:**
  - 1013: busy, retry;
  - 1008 with a reason: not an active producer, kicked by the feed, or a
    licence refusal;
  - 1011: anything else.
- **Origin.** No Origin check and no CORS anywhere.
- **Group.** `group` is read, returned by `GET /v1/me`, and decides
  nothing. Whether a person may wake comes from igni server's producer feed
  (`active` and `removed`, `waker.rs:161-181`).
- **Public routes.** `GET /v1/me` (producer, org, group, expires_at),
  `POST /v1/sign-in`, `POST /v1/commands`, `GET /healthz`. VM state lives
  in the gateway's own PostgreSQL (`gw_producers`). Only the mTLS admin
  route `/v1/admin/producers/{p}` shows it.
- **Relaying.** The relay is transparent: `host_model` and `caller` are
  not filtered there. igni in the VM refuses `caller`
  (`Layer.connections_speak_for_extensions() == False`).

### 1.3 igni server (igni-server)

- **The gateway URL.** `igni_gateway` is advertised in
  `/.well-known/openid-configuration` (`app/api/base_v1/endpoints/identity/discovery.py:46,71`)
  from `FLEET_GATEWAY_URL`, and left out when Fleet is not configured.
  Nothing reads it.
- **The group pack.**
  - Fetched with `GET /v1/portal/me/group/pack`; unchanged packs answer
    304 against an ETag.
  - Signed by `app/services/portal/group_pack_signature.py`: RS256 with the
    token key ring, `typ: igni-group-policy+jwt`.
  - Claims: `iss, sub, org, iat, version` (epoch ms), `token_use=group_policy`
    and `policy`. There is no `exp`, so the pack works offline.
  - Payload fields: `group_id, group_name, fetched_at, entries,
    default_model, theme`. The order of fields is part of the contract.
- **Groups.**
  - `org_groups` has no mode or cloud field.
  - A person is in exactly one group (`uq_org_group_members_user_one_group`).
  - The admin edits a group with `PATCH /v1/portal/groups/{id}`
    (`UpdateGroupRequest`), from the portal's `GroupDetailPage`, whose
    `SettingsTab` cards save on change.
  - Audit is per request (`app/middlewares/request_audit.py`).
- **Every active member is an active person producer**
  (`services/cloud/producer_kinds.py:39-69`). Nothing opts a person out of
  the cloud. The feed trigger `producer_feed_on_group` fires on
  `vm_profile_id` and `is_default`.
- **Events share one lease.** `/v1/me/events/pull` (desktop) and the
  gateway's platform pull take items from **one queue with one 2-minute
  lease**. `docs/service-identity.md:1500-1507` says it directly:
  "whichever of the gateway and the desktop pulls first has the event for
  two minutes".

### 1.4 igni-core and igni-sdk

- **Desktop event pull.** igni-core pulls `/v1/me/events` itself
  (`extensions/event_pull.py`) when `Layer.fetches_own_events()` is true.
  It is true on the desktop and false in the cloud layer.
- **What dedupes deliveries.** `delivered_events` is kept per database:
  SQLite on the desktop, PostgreSQL in the VM. So when both sides run,
  the same event can be delivered twice, once on each side.
- **What the pack verifier checks.** `core/config/group_policy/signature.py`
  checks `iss`, `sub` and `org`, and refuses rollback by `version`. The
  pack is cached in `<data_dir>/group-policy/`. `IGNI_GROUP_POLICY_REQUIRED`
  closes the `RunGate` until a pack verifies. `GroupPolicyPack` ignores
  unknown fields.
- **`welcome`** carries only `client_id`.
- **igni-client's `Fleet(url, auth=)`**
  - Sends the bearer as a header with subprotocol `igni.v1`.
  - Reads the `vm_credential` push: `needs_sign_in` raises `SignInRequired`
    and `needs_reissue` raises `ReissueRequired` (PR #38).
  - Has no `igni_gateway` discovery.
  - igni-sdk has no TypeScript gateway client.

---

## 2. Discovery (D1)

### 2.1 Contract: `GET /v1/me/cloud` on igni server

**Auth:** the person's session token. App, space and VM tokens get `403`,
because this endpoint describes a person's desktop.

**200 response:**

```json
{
  "cloud": "available",
  "reason": null,
  "gateway_url": "https://fleet.acme.example",
  "mode": "local_and_cloud",
  "events_owner": "local",
  "group": { "id": "7f3e…", "name": "Engineering" },
  "producer": "person:2b9c…"
}
```

| Field | Values | Meaning |
|---|---|---|
| `cloud` | `available` \| `unavailable` | Whether this person has a cloud igni they may use from the desktop |
| `reason` | `null` \| `fleet_not_configured` \| `group_local_only` \| `inactive` \| `not_licensed` | Why it is `unavailable`. `null` when available |
| `gateway_url` | https URL or `null` | The same value as `igni_gateway`. `null` when Fleet is not configured |
| `mode` | `local` \| `cloud` \| `local_and_cloud` | The group's mode. The same value the pack carries, sent here so the switch need not wait up to 5 minutes for the next pack poll |
| `events_owner` | `local` \| `cloud` | Who pulls this person's service events (§7) |
| `group` | `{id, name}` | For display |
| `producer` | string | The gateway's producer id, for logs and support |

**Rules:**

- **Additive only.** Clients ignore unknown fields. An unknown `cloud` or
  `mode` value counts as `unavailable` or `local`, which is the safe
  direction. `reason` is an open set: unknown values are shown as
  "unavailable".
- `Cache-Control: private, max-age=60` and an `ETag`.
- **When the shell asks:**
  - at launch;
  - after sign-in or sign-out (it watches `credentials.json`);
  - every 5 minutes while the app is focused;
  - and at once when the gateway closes with a 1008 reason.

**Companion: `PUT /v1/me/cloud/events-owner`**

- Body: `{"owner": "cloud" | "local"}`.
- Answers `200`, with the same body as `GET`.
- Answers `409 {"error":"mode_forbids"}` when the mode does not allow that
  owner.
- Audited.

### 2.2 Live state: the gateway's `GET /v1/me` gains `vm`

```json
{ "producer": "…", "org": "…", "group": "…", "expires_at": 1795000000,
  "vm": { "state": "asleep", "signed_in": true } }
```

- `state` is one of `none | asleep | waking | running`. It is read from
  `gw_producers`, for the caller's own producer only.
- **The route never wakes the VM.**
- `signed_in` is the gateway's flag. The authoritative credential state is
  still the `vm_credential` push from igni, which igni replays to any
  client that attaches late (`CLOUD.md`, VM credential).

### 2.3 Why this split, and what was rejected

- **Rejected: igni server reports VM state by asking the gateway.**
  - The gateway owns runtime state and does not report it back.
  - The only way in is its mTLS admin API, which would put an admin
    credential and a new failure mode into igni server's public request
    path.
  - The live state matters only when the person is about to use the
    gateway, and the client already holds a token the gateway accepts.

  The owner's wording was "igni server tells the app … the VM's state".
  This design splits that sentence: the server answers *whether*, the
  gateway answers *how it is right now*. → Open question Q1.
- **Rejected: discovery from `igni_gateway` and the pack alone, with no new
  endpoint.**
  - `igni_gateway` belongs to the server, not the person, so it cannot
    express `group_local_only` or `inactive`.
  - The pack does not carry `mode` until phase 3, and refreshes every
    5 minutes.
  - Neither one carries the events owner.

  `igni_gateway` keeps its job: it is how a program finds the gateway
  (igni-client discovery, §10).
- **Rejected: putting `gateway_url` in the signed pack.**
  - The gateway address is deployment config, not group policy.
  - Changing it would re-sign every pack.
  - The trust argument is the same either way: the URL comes over TLS from
    the same `api_url` the token already goes to.

---

## 3. Auth hand-off and refresh (D2)

### 3.1 Decision: a loopback relay in the shell

```
 window (webview)                   Tauri shell (Rust)                         Fleet gateway
 ws://127.0.0.1:<relay>  ───────▶  relay: checks Origin + igni-token.<t>
   subprotocols igni.v1,            reads ~/.igni/credentials.json   ──────▶  wss://gw/v1/connect[?sign_in=1]
   igni-token.<relay token>         Authorization: Bearer <session>            subprotocol igni.v1
                         ◀───────   frames passed through unchanged  ◀──────
```

- **Windows see a local backend.** The relay listens on `127.0.0.1` on a
  port the OS picks, with its own per-launch token (32 random bytes, like
  `backend/ws_token.rs`). It runs the same Origin allow-list as the backend:
  `tauri://localhost`, `http(s)://tauri.localhost` and loopback. Switching
  to the cloud means pointing windows at `ws=ws://127.0.0.1:<relay>`. The
  web client's connection code does not change, and the **CSP stays
  loopback-only**: `csp_is_set_and_names_no_host_off_this_machine` keeps
  passing as written.
- **Upstream.** There is one gateway connection per window connection, so
  each window keeps its own `client_id` and window model, exactly as it
  does locally.
  - TLS goes through `rustls`. rustls and ring are already in the build;
    the platform root store is added so enterprise CAs work.
  - `HTTPS_PROXY` and `NO_PROXY` are honoured.
  - The bearer goes in the `Authorization` header, not a subprotocol,
    because Rust can set headers.
- **Re-dialling is the refresh.**
  - The relay reads `credentials.json` **on every dial**, never caching
    the token beyond one connection.
  - It watches the file: a sign-in or sign-out closes every upstream, and
    the windows reconnect through their normal backoff.
  - Session tokens last 30 days and have no refresh token, so nothing
    needs renewing mid-connection. A dial answered `401` moves the shell to
    *needs desktop sign-in* (§8).
- **Sign-in stays where it is.** In phases 1–2 the local backend runs the
  existing `login` flow. In `cloud` mode (phase 3) a stripped-down
  **account-only** backend does it (§6.4).

### 3.2 Rejected alternatives

1. **Rejected: the webview connects straight to the gateway with
   `igni.bearer.<token>`.** This is what the TODO literally says, and what
   the gateway already supports for browsers.
   - The token the gateway accepts from a person is their **30-day session
     token**. It can act on igni server for 30 days and mint a VM
     credential through `vm_sign_in`. Handing it to JavaScript puts it
     within reach of any script in the webview, such as a markdown or
     preview injection.
   - It needs a Tauri command that returns the token.
   - It needs the CSP opened to an unknown `wss:` host, which also opens a
     path for exfiltration.

   The gateway keeps the subprotocol for a future **hosted web UI**, which
   must not use a 30-day token either (Q3). → Open question Q2, because
   this departs from the TODO's wording.
2. **Rejected: the local igni-core backend proxies to the gateway.**
   - It ties the cloud connection to a process that `cloud` mode does not
     run.
   - It mixes two standings on one socket.
   - It needs protocol work in igni-core for no gain.
3. **Rejected: a short-lived token minted for the webview.**
   - The gateway accepts only `token_use=session` with `aud` = the issuer.
     igni server could mint a short session token, but that is new server
     surface, and the relay makes it unnecessary for the desktop.
   - It remains the right answer for a hosted web UI (Q3).

---

## 4. The switch: UX and state model (D3)

### 4.1 UX

- **Where.** A two-way control, **This computer · Cloud**, at the top of
  the account menu (`components/AccountMenu.tsx`), which is the one menu
  that exists today: there is no Settings page. A **backend chip** in the
  status line (`components/StatusLine.tsx`, next to `GroupIndicator`)
  shows the active side and, in the cloud, its state: `Cloud`,
  `Cloud · waking…`, `Cloud · sign in`, `Cloud · offline`.
- **When it shows.** Only when `cloud == available` **and**
  `mode == local_and_cloud`.
  - In `cloud` mode the chip shows `Cloud` with no control.
  - In `local` mode, or with no cloud igni, nothing new appears.
- **It is app-wide.** Every window flips together. A window per side is
  phase 5.
- **It is remembered per account** in the shell's own state file, keyed
  by `iss|org|sub`. The next launch starts on the side last chosen if it
  is still allowed, and otherwise falls back to local.
- **Flipping to Cloud:**
  1. The shell checks `/v1/me/cloud`.
  2. It sends `PUT events-owner {cloud}`.
  3. It starts the relay.
  4. It re-points every window.
  5. The windows hydrate from the VM's igni.

  The **local backend keeps running**: a run in progress there finishes.
- **What the person sees afterwards.**
  - Local runs still going or **approvals waiting locally** show as a
    badge on the control ("1 waiting on this computer"). Clicking it
    switches back.
  - Sessions are per side: the cloud's list is the VM's. Nothing is
    copied between sides.
- **Flipping to This computer** is the reverse: `PUT events-owner {local}`,
  re-point the windows, keep the relay idle.

### 4.2 State model (owned by the shell, broadcast to windows)

The shell is the single source of truth for which side is active. It
pushes `igni-backend-state` (a Tauri event) to every window and answers
`backend_state()`.

```
          ┌──────────── switch(local) / mode→local / kick(group_local_only) ─────────────┐
          ▼                                                                              │
     ┌─────────┐ switch(cloud) ┌──────────┐ upgraded ┌────────┐ welcome ┌─────────────┐  │
     │  Local  │──────────────▶│  Dialing │─────────▶│ Waking │────────▶│ CloudReady  │──┤
     └─────────┘               └──────────┘          └────────┘         └─────────────┘  │
                                 │  │ 401                │ 1011/timeout   │  ▲  push      │
                                 │  └─▶ NeedsDesktopSignIn               │  │ needs_sign_in
                                 │ net error / 503 / 1013                ▼  │ / ready     │
                                 └────────▶ CloudUnreachable(backoff) ◀─ CloudNeedsSignIn ┘
                                            (retry 1s→30s, "use this computer")
```

| State | What the windows show |
|---|---|
| `Local` | The normal UI, as today |
| `Dialing` / `Waking` | The previous content stays, dimmed under a "Waking your cloud igni…" overlay. The overlay appears 800 ms after the socket opens with no `welcome` yet, so a running VM never flashes it. Typing is allowed: the gateway buffers. |
| `CloudReady` | The normal UI. Cloud-hidden features are hidden (§5) |
| `CloudNeedsSignIn` | A banner: "Your cloud igni needs you to sign in again", with **Sign in**. Chat input is disabled. |
| `NeedsDesktopSignIn` | The existing sign-in wall (`SignInWall.tsx`): the desktop's own session expired or was revoked |
| `CloudUnreachable` | A banner: "Can't reach your cloud igni. Retrying…", plus **Use this computer** when the mode allows |
| `Refused(reason)` | A banner naming the reason (`group_local_only`, `inactive`, `not_licensed`, licence), then an automatic return to `Local` when local is allowed |

### 4.3 Reconnect and resync

Re-pointing uses the mechanism Restart Backend already uses
(`bootstrap.rs:55-63,142-148`): each window gets `app_url(dir)` with the
new `ws=`. One web-client change is needed for the cloud:

- **A second `welcome` on the same socket means a resync.** After a node
  move the gateway reconnects behind the socket, and igni sends a new
  `welcome` with a new `client_id`.
  - Today `bind()` runs only on `connected`, so the window would keep a
    stale `connection_id` and no attached session.
  - `IgniClient` must emit `connected` again on any `welcome` after the
    first.
  - The in-flight RPC errors the gateway sends ("igni's connection was
    moved…") are shown as retryable, not as failures.

### 4.4 Per-backend client state (TODO item 5)

- **The new key.** `ember-code:client-id[:<label>]` becomes
  `igni:client-id:<backend>[:<label>]`, where `<backend>` is `local` or
  `cloud-<first 12 hex of sha256(gateway_url|org|sub)>`.
  - Local migrates once: read the old key, write the new one, keep the old
    one for a release.
  - Hashing the key means a second account or gateway never collides, and
    no address is written in clear to localStorage.
- **Other per-window localStorage** (drafts, view keys) gets the same
  prefix. A sweep of `localStorage` uses in `clients/web/src` is part of
  the phase 1 PR.
- **Server-side state needs nothing.** The window model is kept by each
  backend, so it follows the backend automatically.

---

## 5. Cloud mode in phase 1: what works, what is hidden (D5)

| Feature | In cloud mode | Why |
|---|---|---|
| Chat, sessions, runs, approvals, model switch, slash commands | **Works** | Plain client protocol, relayed |
| Attachments (composer upload, drag and drop) | **Works.** The bytes go through `upload_attachment{filename, content_base64}` and land on the VM | Already an upload, not a path. The 5 MiB composer limit holds |
| File pills / code pills: open in editor, reveal in folder | **Hidden.** The in-app preview is kept where the backend serves the content | The path is on the VM. `plugin:opener` would open a missing or wrong local file |
| Project folder picker, New Window's folder choice | **Hidden.** New Window opens on the VM's workspace | `pick_dir_native` would open a dialog on a VM without a display |
| Extensions (pages, indicators, list) | **Works** (§15) | Pages are framed from a loopback origin the shell serves per extension; their calls speak for the extension on a connection the node agent marks `pages`. Nothing about extensions is hidden in cloud mode |
| Hooks panel's "open file" | **Hidden** | Local path |
| File › Restart Backend / Reinstall | **Kept, labelled "(this computer)"** | They act on the local backend, which keeps running |
| Account menu, Log out | **Works.** Log out signs the desktop out, which also ends the cloud connection (the relay sees `credentials.json` change) | One identity |
| Updater, theme, group indicator | Unchanged | Shell-side |
| Backend version chip | Shows the VM's igni version | See below |

**Version skew.**

- The VM's igni and the desktop's bundled igni-core can differ. The web
  client already tolerates additive protocol changes (zod, unknown frames
  dropped).
- Locally the shell refuses a backend that speaks another wire *major*.
  The relay cannot see a version before `welcome`, and `welcome` carries
  none.
- Phase 1 therefore relies on additive compatibility, and **does not add
  a version to `welcome`**, which would be a client-protocol change. If
  the owner wants a hard refusal, see Q6.

**Defence on igni-core's side.** The cloud layer refuses, as an RPC
error, the RPCs that make no sense on a VM: `pick_dir_native` and any
other that opens a native dialog. A client other than igni-app, or an old
one, then gets an answer instead of a hang. This is a small igni-core
change in `packages/igni-cloud`.

---

## 6. Group mode (D4)

### 6.1 Storage

**The column.**

- `org_groups.mode`: a text column checked to `local | cloud |
  local_and_cloud`, NOT NULL, default `local_and_cloud`.
- The default keeps today's behaviour: every member is a producer, and
  the desktop runs locally.
- It is a tenant migration (`alembic_tenant`).

**The trigger.** `producer_feed_on_group` must add `mode` to its
`UPDATE OF` list. Without it, the gateway never hears that a group
changed mode.

**The API and portal.**

- `UpdateGroupRequest` and the group read model gain `mode`, edited with
  the existing `PATCH /v1/portal/groups/{id}` (admin only).
- In the portal it is a new card in `components/group/SettingsTab.tsx`:
  "Where igni runs for this group", with three choices and one line each
  on what that means for people. The card shows a warning before saving a
  change that would kick people out of a running cloud session.
- The middleware audits the request. The handler adds
  `add_detail({"mode": {"from": …, "to": …}})` so the audit export shows
  the change.

**Rejected: a `settings` entry such as `{"desktop":{"mode":"cloud"}}`.**
The shell can already read settings entries, so it is tempting. But:

- entries are free-form JSON, merged across entries;
- server-side enforcement (the feed and the sign-in grant) would have to
  parse policy JSON;
- the portal would have no typed control.

### 6.2 Signing

- `mode` becomes a **top-level field of the signed pack payload**,
  appended after `theme`. Appending keeps the documented field order.
  Signing, keys and claims are otherwise unchanged
  (`group_pack_signature.py`), and the ETag covers it automatically
  because it hashes the payload.
- **Older clients ignore it.** igni-core's `GroupPolicyPack` and the
  shell's serde struct ignore unknown fields, so an old client treats
  every group as `local_and_cloud`. Server-side enforcement (§6.3) still
  holds for `local`. For `cloud`, an old desktop keeps running locally
  until it updates (Q5).
- **A pack without `mode`** (an older server) means `local_and_cloud`.

### 6.3 Enforcement points

| Where | `local` | `cloud` | `local_and_cloud` |
|---|---|---|---|
| **Portal / API** | `PUT events-owner {cloud}` → 409 | `PUT events-owner {local}` → 409 | Either owner |
| **igni server, producer feed** | The person's row is `active: false, inactive_reason: "group_local_only"`. **Not** `removed`: no erasure, and the VM's data is kept under the profile's idle-erasure rule (Q4) | `active: true` | `active: true` |
| **igni server, `vm-sign-in` grant and VM-credential renewal** | Refused (`invalid_grant`, reason `group_local_only`). This is defence in depth: a VM somehow still awake cannot renew | Allowed | Allowed |
| **Gateway** | A wake is refused with **1008** `group_local_only`. A live connection is **kicked** by the existing feed path (`gw_kick`) within the feed's long-poll latency (seconds). `POST /v1/sign-in` → 403 with the same reason | — | — |
| **Shell (offline, from the verified pack)** | No switch, never dials | **Does not spawn a full local backend.** Starts the account-only backend (§6.4) and connects only through the relay | The switch, as in §4 |
| **igni-core full backend (offline, from the verified pack)** | — | The **`RunGate` closes** with a person-facing reason: "Your organisation runs igni for you in the cloud." This also stops a hand-started `igni` CLI. Sign-in and status still work | — |

**`cloud` mode on the desktop is a policy, not a sandbox.** A person can
always install an old igni-core that ignores the field. The run gate
makes the supported paths honour it. The guarantee that matters for
`local` mode (no cloud compute) is server-side and does not depend on the
client at all.

### 6.4 `cloud` mode's local process: an account-only backend (igni-core)

A desktop in `cloud` mode still needs four things:

- sign-in (the browser hand-off and `credentials.json`);
- server discovery;
- the group pack sync, which is how it learns the mode;
- `status_update` for the sign-in wall.

All four live in igni-core today. The decision is a new startup mode:

```
python -m igni_core.backend --account-only --ws-port 0
```

- It serves only the sign-in, discovery, plan and status RPCs.
- It runs the pack sync.
- It starts **no** agent, tools, MCP servers, extensions, watchers or
  event pull.
- Its `RunGate` is permanently closed. `user_message` gets the same
  refusal as §6.3.
- Its standing is the window's, and nothing else is reachable.

The account backend's socket is never what the chat windows point at.
The sign-in wall and the account menu reach it through the shell. In
phase 3, a window in `cloud` mode connects its chat to the relay and its
account calls to the account backend through a Tauri command
(`account_rpc(method, args)`), which forwards only the allow-listed
methods.

**Rejected: re-implementing sign-in and the pack fetch in Rust.** It
would duplicate tested Python: the callback server, `credentials.json`'s
format, JWKS caching and `min_version` state. Two implementations of
anti-rollback are worse than one. → Q7 offers the cheaper and stricter
variant if the owner wants no local Python at all in `cloud` mode.

### 6.5 Offline behaviour

| Situation | Behaviour |
|---|---|
| Never signed in, no pack | As today: a full local backend and the sign-in wall. Once signed in, the pack decides |
| Offline with a verified cached pack | The pack's mode holds. It has no `exp`, and anti-rollback stops an older pack from being replayed |
| `cloud` mode and the gateway unreachable | `CloudUnreachable`. **No fallback to local** (Q8), because the admin chose cloud, often for data-residency reasons |
| `local_and_cloud` and the gateway unreachable | `CloudUnreachable`, with **Use this computer** |
| The pack fails to verify (wrong pin, tampered) | The shell treats it as no group layer, as today, so the mode is `local_and_cloud`. igni-core's `group_policy.required` still applies where an admin set it |

### 6.6 The mode changes while the app runs

The shell learns the new mode three ways:

- the backend's pack sync rewrites `pack.jws` (every 5 minutes), and the
  shell watches the file;
- `/v1/me/cloud` polling;
- an immediate 1008 kick from the gateway.

| Change | What happens |
|---|---|
| any → `local`, while in the cloud | The gateway kicks (1008 `group_local_only`). The shell shows "Your organisation now runs igni on this computer", re-points the windows to local and hides the switch. Unsent drafts stay in the composer |
| `local_and_cloud` → `cloud`, while local | A banner: "Your organisation moved igni to the cloud. Switching when the current run ends." The shell waits until no local run is in flight (at most 10 minutes, then switches anyway with a notice), then switches, stops the owned full backend and starts the account-only one. Local approvals still waiting are reported first ("1 approval on this computer will be dropped") |
| `cloud` → `local_and_cloud` / `local` | The shell spawns a full local backend. `local_and_cloud` stays on the cloud with the switch shown. `local` re-points to local |
| A group change that only alters `vm_profile` | Nothing on the desktop. The gateway handles profile changes |

---

## 7. Who pulls `/v1/me/events` (D6)

**Today's race (§1.3–1.4).**

- Both sides lease from one queue, so the first to pull wins.
- A side without the extension acknowledges the event as `not_handled`,
  and the other side never sees it.
- Lost acknowledgements deliver the event twice, because the dedupe
  tables are separate.

**Rule: igni server holds one owner per person and serves only the owner.**

- **Storage.** A small table `person_event_owner(org_id, user_id, owner,
  updated_at)`. It is kept apart from `org_group_members` so that moving a
  person between groups does not reset it.
- **The effective owner.**
  - `local` mode → local;
  - `cloud` mode → cloud;
  - `local_and_cloud` → the stored value, `local` until the person first
    switches (Q9).
- **The desktop's pull** (`POST /v1/me/events/pull` with a session token)
  answers `409 {"error":"events_owned_elsewhere","owner":"cloud"}` when the
  owner is cloud. `ack` and attachment fetches for items already leased
  stay allowed: acknowledgements are idempotent by id, so an in-flight
  delivery finishes cleanly.
- **The gateway's platform pull** for that person returns `[]` when the
  owner is local. `/v1/platform/wakes` stops listing the person for
  pending events, so the VM is not woken for events it will not get.
- **On a change of owner, leases are left alone.** Items the old side
  leased and did not acknowledge come back after the 2-minute lease, to the
  new owner. At-least-once delivery is unchanged.
- **The switch sets the owner** (`PUT /v1/me/cloud/events-owner`) before it
  re-points the windows.

**igni-core change (`extensions/event_pull.py`).** A `409` with
`events_owned_elsewhere` is not an error. The pull idles and asks again
after `INTERVAL_SECONDS` (60 s), logging once at info. It needs no new
RPC and no shell call: the server is the single arbiter, so the two sides
never have to coordinate.

**Rejected alternatives:**

- **The shell tells the local backend to stop pulling.** It does not cover
  the gateway, which pulls for a sleeping VM. It does not survive the app
  being closed. And it needs a new RPC.
- **The desktop pulls only while it is open, and the cloud the rest of the
  time.** Events would follow whether the app happened to be open, not the
  person's choice, so they would land in a different place depending on
  timing.
- **A longer or per-side lease.** It still lets an event land on the side
  without the extension.

---

## 8. Error states (D7)

| Condition | Detected by | Shell state | Recovery |
|---|---|---|---|
| VM asleep or waking | The upgrade succeeded but no `welcome` arrived yet | `Waking` | Automatic. After 20 s the gateway closes with 1011, and the shell moves to `CloudUnreachable` with **Retry** |
| VM needs sign-in | The `vm_credential` push with `needs_sign_in` (replayed on attach) | `CloudNeedsSignIn` | **Sign in** → `cloud_sign_in()` (Tauri) → the shell calls `POST {gateway}/v1/sign-in` with the session → `credential.state`. When igni pushes `ready`, the banner clears |
| VM credential unreachable | `vm_credential` `unreachable` | `CloudReady` with a warning chip | Informational: the VM cannot reach igni server. Runs that need the server fail with their own errors |
| `needs_reissue` | Never seen by a person (an app-only state) | — | Logged. Nobody signs in as an app on the desktop (decided 2026-11-12) |
| Desktop session expired or revoked | Dial → 401 | `NeedsDesktopSignIn` | The existing sign-in wall. The relay re-dials when `credentials.json` changes |
| Gateway unreachable | DNS, TCP or TLS failure, or 503 (JWKS) | `CloudUnreachable` | Backoff from 1 s to 30 s, **Use this computer** where allowed |
| Gateway busy | 1013 | `CloudUnreachable("busy")` | The same backoff |
| Could not start | 1011 | `CloudUnreachable("could not start")` | **Retry**. The reason text is shown |
| Not an active producer, licence refusal | 1008 with a reason | `Refused(reason)` | Back to local if allowed. `/v1/me/cloud` re-checked |
| Group moved to `local` | 1008 `group_local_only`, the pack, `/v1/me/cloud` | → `Local` | §6.6 |
| Session moved between nodes | A second `welcome` on the same socket | Stays `CloudReady` | Re-bind and hydrate (§4.3) |
| TLS failure (bad certificate) | rustls | `CloudUnreachable("certificate")` | **No bypass in a release build.** A debug-only `IGNI_DEV_GATEWAY_INSECURE`, following `IGNI_DEV_NO_WS_TOKEN` |

**How the relay closes the window's socket.**

- **Transient upstream failures** close it with **1012 (service restart)**.
  The web client reconnects with its normal backoff, and the relay
  re-dials.
- **Final states** close it with **1001** after the shell has already
  re-pointed or flagged the window.
- **The relay never forwards 1008 to the window.** The web client reads
  1008 as "replaced", and the meaning would be lost.

---

## 9. Security (D9)

1. **The token in the webview: none.**
   - The session token is read by Rust from `credentials.json`. It is
     already there at 0600, and the shell already reads it for `sub`.
   - It is sent only in the upstream `Authorization` header, and is never
     logged. The relay's log lines name the gateway host and close codes
     only.
   - No Tauri command returns it. A test asserts that no command result
     and no window URL contains it.
2. **Where the token may go.**
   - igni-core's rule is that "igni server's token goes only to the origin
     of `api_url`" (`SECURITY.md`). The relay extends that trust to
     exactly one more origin: the `gateway_url` that `api_url` itself
     returned over verified TLS.
   - It must be `https`. A plain `http` gateway is refused outside a debug
     build (Q10 covers the lab's plain-HTTP setups).
   - A managed setting `fleet.gateway_url` may **pin** the gateway, and a
     discovered URL that differs is then refused. This is for air-gapped
     or regulated deployments.
3. **The relay's own door.**
   - It accepts connections only on `127.0.0.1`, with a webview or
     loopback `Origin` and the relay's per-launch token, compared in
     constant time. This is the same model as the backend's handshake
     (`transport/ws_auth.py`).
   - Without the token, a page in the person's browser cannot use the
     relay to reach their cloud igni.
4. **Origin checks at the gateway (igni__cloudvm).**
   - New config: `public.allowed_origins` (a list, empty by default).
   - A request **with** an `Origin` header must match the list, or it gets
     403.
   - A request **without** one comes from a program (igni-client, the
     relay) and is allowed.
   - With bearer-only auth there is no ambient credential, so cross-site
     WebSocket hijacking is not possible today. The check closes off a page
     that was handed a token, and gives the future hosted web UI an
     explicit allow-list.
5. **Token expiry at the gateway** (Q3). Today an expired token's socket
   lives on. The recommendation is that the gateway closes at `exp` (plus
   leeway) with 1008 `token_expired`. For the desktop this is harmless:
   the relay re-dials with the current token.
6. **The group pack** keeps its existing protections: signature, pins,
   `iss`/`sub`/`org` binding and anti-rollback.
   - A local user who can write `~/.igni` cannot forge `mode`.
   - They can delete the pack, which means `local_and_cloud` on the shell.
     That is acceptable, because the server enforces `local` (§6.3).
7. **Extension iframes** are shown in cloud mode. What keeps that safe —
   the page mark, the shell's page origins and the token's path — is
   §15.6.
8. **The session token reaching the VM** through `vm_sign_in` is existing
   behaviour (`CLOUD.md`). This design adds nothing to it.

---

## 10. Contracts and SDK changes (D8)

| Repo | Contract change |
|---|---|
| igni-sdk `contracts/` | **New `cloud-discovery.schema.json`**: the `/v1/me/cloud` response and the `PUT` body. It is the single source: igni-server has a test that its OpenAPI model matches it, igni-client validates against it, and the shell has a test (fixture copy, checked by `scripts/sync_contracts.py --check`). This is the "shared discovery" the TODO assigns to igni-sdk, as a contract rather than a TS library, because the desktop's discovery client is Rust |
| igni-server OpenAPI | `GET /v1/me/cloud`, `PUT /v1/me/cloud/events-owner`, `mode` on group read/patch, `409 events_owned_elsewhere` on `/v1/me/events/pull`, the platform feed row's `inactive_reason` |
| igni-server pack | Top-level `mode` appended to `OrgGroupPack`. The field order stays documented |
| igni__cloudvm | `GET /v1/me` gains `vm{state, signed_in}`. `public.allowed_origins`. 1008 reasons become stable tokens (`group_local_only`, `inactive`, `licence`, `token_expired`) followed by human text, documented in `docs/gateway.md` |
| igni-core | `GroupPolicyPack.mode` (optional, default `local_and_cloud`). `--account-only`. The cloud layer refuses native-dialog RPCs. Event pull treats 409 as idle. **No wire-schema or RPC-contract change is required** (`make contracts` re-run to prove it) |
| igni-client | `discover(server_url, auth) -> CloudIgni(gateway_url, mode, cloud, reason)`, which reads `igni_gateway` and `/v1/me/cloud`. `Fleet.from_server(server_url, auth=Person(...))`. A typed `GroupIsLocalOnly(NotAllowed)` for 1008 `group_local_only`. Existing `Fleet(url, auth=)` is unchanged |
| igni-app | New Tauri commands `backend_state`, `switch_backend`, `cloud_sign_in`, and in phase 3 `account_rpc`. The event `igni-backend-state`. The new localStorage key scheme |

---

## 11. The model contract (D10)

Nothing here changes what the model reads or writes. The client protocol
is relayed byte for byte. Sessions, prompts, tools, `<attached-files>`
and the other injected blocks are built by the igni that runs the
session, local or in the VM, exactly as today. Attachments in the cloud
use the same upload path and `@path` reference. Service events are
delivered by whichever side owns them, through the same inbox.

The new person-facing texts are not model-facing:

- the `RunGate` refusal in `cloud` mode;
- the banners;
- the 1008 reasons.

None of them reaches a prompt.

**The trap to avoid.** Do not "tell the agent it runs in the cloud" by
adding a system-context line such as "you are in a cloud VM, local files
are unavailable" or similar. That **would** be a model-facing change:
**QUESTION: MODEL-CONTRACT.** It is out of scope here. If phase 4 or 5
ever seems to need it, it goes to the owner first.

---

## 12. Test plan

The rules are those in the memory "tests must be exhaustive":

- each behaviour gets a test that fails on the old code;
- mutation-proven where it is cheap;
- and the full CI run locally before each push.

### 12.1 Per repo

**igni-server (pytest, real Postgres)**

- `/v1/me/cloud`:
  - Fleet not configured → `unavailable/fleet_not_configured`;
  - each mode;
  - an inactive member;
  - an app, space or VM token → 403;
  - ETag and 304.
- events-owner:
  - `PUT` per mode, with 409 on a forbidden owner;
  - the default by mode;
  - it survives a group move;
  - it is audited.
- Event pulls:
  - a desktop pull answers 409 when the owner is cloud;
  - ack and attachments still work for leased items;
  - the platform pull is `[]` when the owner is local;
  - `/wakes` drops the person;
  - the existing test `test_the_gateway_and_the_desktop_share_one_lease` is
    **replaced** by `test_only_the_owner_is_served`.
- Feed: a group set to `local` → its rows go `active:false,
  inactive_reason`. The trigger fires on `mode` (this test fails without
  the trigger change).
- The `vm-sign-in` grant and renewal refuse a local-only person.
- The pack carries `mode` after `theme`. The signature still verifies with
  igni-core's verifier (a cross-repo fixture). The ETag changes when the
  mode changes.
- The group PATCH writes `mode` and the audit detail.

**igni-server portal**

- Vitest for the card: three choices, the warning before a kicking
  change, MSW handlers.
- Playwright functional run against `tools/portal-e2e-stack.sh`.
- New visual baselines for the card, and an axe check.

**igni__cloudvm (cargo, `GATEWAY_TEST_DATABASE_URL`)**

- `/v1/me` `vm` for asleep, running and none, and the route never wakes
  the VM.
- `allowed_origins`:
  - no Origin → allowed;
  - a listed Origin → allowed;
  - any other Origin → 403;
  - the default (empty list) refuses every Origin.
- 1008 `group_local_only` on wake and on kick (a `cluster.rs` case).
- `POST /v1/sign-in` → 403 for an inactive person.
- Token-expiry close, if Q3 is accepted.

**igni-core (pytest)**

- `GroupPolicyPack.mode`: absent → `local_and_cloud`; an unknown value →
  `local_and_cloud` with a warning.
- The `RunGate` closes under `cloud`, with the person-facing reason. Sign-in
  RPCs still work.
- `--account-only` serves its allow-list and refuses `user_message` and
  every agent, tool and extension RPC. It starts no event pull or MCP.
  Startup memory is measured and recorded.
- The event pull idles on 409 (no busy loop, one log line) and resumes
  when the owner comes back.
- The cloud layer refuses `pick_dir_native`.
- `make contracts` shows no diff.

**igni-sdk (pytest)**

- `discover()` against a fake igni server (FakeIgni grows
  `/.well-known/openid-configuration` with `igni_gateway` and
  `/v1/me/cloud`).
- `Fleet.from_server` with FakeGateway.
- `GroupIsLocalOnly` mapping.
- `cloud-discovery.schema.json` validated against recorded server
  responses.
- The contract sync check.

**igni-app (cargo, vitest, Playwright)**

Rust:

- The relay:
  - an upstream `Authorization` header and `igni.v1`;
  - refuses a foreign Origin, no token, or a wrong token;
  - reads `credentials.json` on every dial and closes all upstreams when it
    changes;
  - maps 1013, 1011, 1008 and 401;
  - never writes the token to logs (a log capture test);
  - refuses an `http` gateway in release.
- The state machine: a table test of every transition in §4.2.
- The mode from the verified pack: `local` hides the switch; `cloud`
  spawns `--account-only` and never the full backend; a tampered pack →
  `local_and_cloud`.
- The CSP contract test is **unchanged and still passes**.

Web (vitest):

- a second `welcome` re-binds;
- the key migration;
- the switch shows per `cloud`/`mode`;
- banners per state;
- cloud-hidden features are hidden;
- the waking overlay appears only after 800 ms.

Playwright (mock backend):

- a `cloud` fixture that emits the states through a test hook;
- new visual baselines for the switch, chip and banners
  (`scripts/baselines-linux.sh`).

**Desktop CI (igni-app `tauri-smoke`, xvfb).** igni-app has no
tauri-driver yet (`ci.yml` defers it). The CI proof is a Rust integration
test that:

1. runs the supervisor, the relay and a **stand-in gateway** (igni-sdk's
   FakeGateway, relaying to a real igni-core backend started by the test);
2. switches to cloud through the `switch_backend` command;
3. asserts a `welcome` and one turn through the relay with a fake model;
4. asserts the local backend's PID is still alive;
5. switches back.

The web half runs in Playwright with `ws=` pointed at the relay. A
tauri-driver job stays the recommended follow-up, and is not a blocker.

### 12.2 The live E2E that turns R691 green (the ember-dev KVM host)

**Stack.** Everything is built from the phase branches:

- igni-server;
- a real gateway and node agent (igni__cloudvm);
- a Firecracker guest with the branch's igni-core;
- a real model.

The desktop is the Linux build under xvfb on the host, or a macOS build
reaching the host's gateway through a tunnel. One script,
`r691_live.py`, writes its evidence under the campaign directory.

1. **Discovery.** Org, group G in `local_and_cloud`, person P signed in on
   the desktop. `/v1/me/cloud` is `available`. The switch shows.
2. **Switch and wake.** P's VM is asleep (checked with the admin route).
   Flip to Cloud: the "waking" overlay shows, then a `welcome`, then a
   real-model turn answers PONG. The local backend PID is unchanged. A
   local session is still listed after flipping back.
3. **Per-backend state.** The localStorage keys for local and cloud
   differ, and each side hydrates its own windows.
4. **Events owner.** A service posts an event. It is delivered in the
   cloud only (the VM's `delivered_events` has it, the desktop's SQLite
   does not). Flip to local: the next event is delivered locally only.
   No event lands on both sides.
5. **Needs sign-in.** Revoke P's VM credential (portal
   `/portal/vm-credentials`). The banner appears. **Sign in**, and the
   state goes `ready`, then PONG.
6. **Node move.** Drain P's node (two nodes, as R708 did). A second
   `welcome` arrives, the client re-binds, and the next turn answers PONG
   with no window reload.
7. **Gateway unreachable.** Stop the gateway unit: the banner shows and
   **Use this computer** works. Start it again: the app recovers.
8. **Mode to `local`.** The live cloud socket is kicked within 10 s, the
   app goes back to local and the switch hides. A new connect gets 1008
   `group_local_only`. igni-client raises `GroupIsLocalOnly`.
9. **Mode to `cloud`.** After the local run ends, the shell stops the full
   backend and starts `--account-only`, and the chat runs in the cloud.
   Restart the app offline (a dead proxy, as in "air-gap proof without
   disk"): it stays in `cloud`, shows unreachable, and spawns no full
   backend. A hand-started `igni` CLI run is refused by the `RunGate`.
10. **Security probes.**
    - No session token in the webview: devtools search of window globals,
      localStorage, URLs and the network panel.
    - The CSP is unchanged.
    - The relay refuses a foreign Origin.
    - The gateway refuses an unlisted browser Origin.

11. **Extensions in the cloud.** §15.8.

This run also closes **R321's live proof** (step 5, against a real
gateway). R691 goes green on steps 1–4 and 6–7 at the end of phase 2,
and is re-run in full after phase 3.

---

## 13. Phasing, merge order and estimates (D11)

> **2026-11-20: the phases are now one build.** The owner asked for the
> switch to be built in full, extensions included. Phases 1–4 land
> together, as one branch per repo, merged in the order below; the
> tables stay as the breakdown of the work. Phase 4 is specified in §15.
> Phase 5 (a window per side) is out of scope.

One PR per repo per phase, merged in this order inside each phase:
**igni-server → igni__cloudvm → igni-core → igni-sdk → igni-app.**

- The server endpoint and contract come first.
- The gateway and igni-core accept them.
- The SDK carries the shared schema.
- The app consumes it all.

Each repo PR runs its full CI locally before its push. Estimates are
engineer-days, and include tests and docs.

### Phase 1: discovery and the switch, chat only (extensions hidden)

| Repo | PR | Days |
|---|---|---|
| igni-server | `/v1/me/cloud` (mode reported as `local_and_cloud` until phase 3), `person_event_owner` with the `PUT`, pull enforcement (409 / `[]` / wakes) | 2.0 |
| igni__cloudvm | `/v1/me` gains `vm{state, signed_in}` | 0.5 |
| igni-core | Event pull idles on 409. The cloud layer refuses native-dialog RPCs | 0.5 |
| igni-sdk | `cloud-discovery.schema.json`, `discover()`, `Fleet.from_server` | 1.0 |
| igni-app | Relay, discovery client, state machine, Tauri commands, switch and chip, key migration, second-`welcome` re-bind, waking overlay, cloud-hidden features, tests, baselines | 6.0 |
| **Total** | | **10.0** |

### Phase 2: VM wake, sign-in and status

| Repo | PR | Days |
|---|---|---|
| igni__cloudvm | `allowed_origins`, stable 1008 reasons, close at token expiry (if Q3 is accepted) | 1.0 |
| igni-app | `cloud_sign_in` and its banner, unreachable, busy and could-not-start states, the badge for local runs and approvals, TLS and proxy handling | 2.5 |
| igni-sdk | Docs: Fleet discovery and the sign-in story | 0.5 |
| Live E2E | Steps 1–7 on the ember-dev host. **R691 green**, R321's live proof | 1.5 |
| **Total** | | **5.5** |

### Phase 3: group mode in the portal, and its enforcement

| Repo | PR | Days |
|---|---|---|
| igni-server | `org_groups.mode` migration and trigger, PATCH and read model, pack `mode`, feed `inactive_reason`, grant refusal, `/v1/me/cloud` real mode, audit detail, portal card with vitest, Playwright and visual tests | 3.5 |
| igni__cloudvm | Feed `inactive_reason` → 1008 `group_local_only`, `/v1/sign-in` 403 | 0.5 |
| igni-core | `GroupPolicyPack.mode`, `RunGate` in `cloud` mode, `--account-only` | 2.5 |
| igni-sdk | `GroupIsLocalOnly`, contract sync | 0.5 |
| igni-app | The shell reads `mode` from the verified pack, `cloud` mode's no-spawn plus account backend plus `account_rpc`, live mode changes (§6.6) | 3.0 |
| Live E2E | Steps 8–10, then a full re-run | 1.5 |
| **Total** | | **11.5** |

### Phase 4: extensions over the gateway (specified in §15)

Phase 4 needs three things:

- **A page mark the node agent attests**, so that the cloud igni can let
  one connection speak for an extension (`SECURITY.md` L555).
- **Hosting extension pages from somewhere the desktop can frame.** Today
  the CSP `frame-src` allows only loopback. The likely shape is that the
  relay also proxies page origins.
- **`extension_service_token`** in the cloud.

§15.7 estimates it at about 9 engineer-days across igni-core,
igni__cloudvm and igni-app, plus half a day of live E2E.

### Phase 5: both side by side (out of scope, 2026-11-20)

A window per side, so `local_and_cloud` people can keep a local chat
next to a cloud one. Most of the groundwork comes earlier: the per-window
`ws=`, the per-backend keys and the per-window relay connections. The
missing piece is a per-window choice in place of an app-wide one, plus
the events-owner question, because ownership stays per person. Estimate
on request.

### Totals for phases 1–3

| Repo | P1 | P2 | P3 | Sum |
|---|---|---|---|---|
| igni-server | 2.0 | — | 3.5 | 5.5 |
| igni__cloudvm | 0.5 | 1.0 | 0.5 | 2.0 |
| igni-core | 0.5 | — | 2.5 | 3.0 |
| igni-sdk | 1.0 | 0.5 | 0.5 | 2.0 |
| igni-app | 6.0 | 2.5 | 3.0 | 11.5 |
| Live E2E | — | 1.5 | 1.5 | 3.0 |
| **Sum** | **10.0** | **5.5** | **11.5** | **27.0** |

Add about one day per phase for the merge chain and CI round trips:
**about 30 days end to end** for phases 1–3.

---

## 14. Open questions for the owner

Each question carries a recommendation. Approving the doc as written
means accepting every recommendation.

| # | Question | Recommendation |
|---|---|---|
| Q1 | Discovery is split: igni server answers whether there is a cloud igni (and the gateway URL, mode and events owner); the gateway's `/v1/me` answers the live VM state. Is that acceptable, against "igni server tells the app the VM's state"? | **Yes, split.** The server has no VM state, and fetching it would couple the server to the gateway's admin API |
| Q2 | Token hand-off: the shell relays, with the token kept out of the webview, instead of the webview sending `igni.bearer.<token>` as the TODO says | **Relay.** The gateway's token is the 30-day session token, and the CSP stays loopback-only |
| Q3 | Should the gateway close sockets at token `exp`? And should a future hosted web UI get short-lived person tokens? | **Yes to both, later.** Closing at `exp` lands in phase 2 (cheap, harmless for the relay). Short tokens are needed only when a browser UI exists |
| Q4 | When a group switches to `local`, are its people's VMs kept (inactive) or erased? | **Kept**, under the profile's idle-erasure rule. Flipping a group back must not lose work. An admin can reset a VM explicitly |
| Q5 | Older desktops ignore `mode`. Do we force an update for `cloud` groups? | **No force.** `local` is enforced server-side regardless. For `cloud`, document "requires desktop ≥ X". Optionally the portal shows members on older versions later |
| Q6 | Should the app refuse a cloud igni with a different wire major? That needs a version field in `welcome`, a client-protocol change but not a model-facing one | **Not in phase 1.** Rely on additive compatibility. Add `welcome.wire_version` only if a breaking protocol change is ever planned |
| Q7 | In `cloud` mode, may the desktop still run a local Python process (the account-only backend), or must sign-in move into the Rust shell? | **The account-only backend.** It reuses the tested sign-in and pack code and runs no agent. Moving sign-in to Rust is about 3 extra days in the shell and duplicates anti-rollback |
| Q8 | In `cloud` mode with the gateway unreachable, fall back to local? | **No.** The admin chose cloud. Show unreachable and wait |
| Q9 | Who owns events in `local_and_cloud` mode before the person first switches? | **Local** (no change for people who never use the cloud). People who use Fleet only through igni-client can set the owner with `PUT events-owner` (a one-line SDK call `set_events_owner("cloud")`) |
| Q10 | The lab gateway on the KVM host is plain HTTP. Allow `http` gateways? | **Only in debug builds** (`IGNI_DEV_GATEWAY_INSECURE`). The live E2E uses TLS on the host's gateway, or the debug build if TLS is not set up yet |
| Q11 | Is igni-core the right home for this doc? | **Yes for the doc** (TODO, CLOUD.md and SECURITY.md are here). Each PR links to it. The gateway parts get a short section in igni__cloudvm's `docs/gateway.md` once they are built |

**Out of scope.** The TODO's companion item, "Seeing what an app does,
without signing in as the app", is meant to become a section of this
design. It is left out, because the decision of 2026-11-12 settles the
part that touches the switch: **the switch only ever connects as the
person**, never as an app. That item's allow-list and SDK command can
follow as their own section once this one is approved.

---

## 15. Extensions in cloud mode (D12)

Owner's decision of 2026-11-20 (option A): an extension's pages, its
indicators and its place in the list work in cloud mode exactly as they
do locally. This section replaces "Phase 4" in §13 and the "Hidden" row
in §5. Each part names its wire contract, its security argument, the
files that change in each repo, its tests, and where it plugs into the
relay (§3) and the state machine (§4).

### 15.1 What stands in the way today

Read from the code on 2026-11-20:

1. **`caller` is refused in the cloud.**
   - `MessageAdmission._caller_refusal` (`src/igni_core/backend/message_admission.py`)
     refuses any `RPCRequest` with a `caller` when
     `Layer.connections_speak_for_extensions()` is false.
   - `CloudLayer` answers false (`packages/igni-cloud/src/igni_cloud/layer.py:95`).
     The comment there, and `docs/SECURITY.md` ("A Fleet UI that hosts
     extension pages will need a mark the node agent attests, as it attests
     the control stream"), name the fix.
   - Everything a page or an indicator does goes through `caller`:
     - `host_model`;
     - `extension_service_token`;
     - `ext.<id>.*`, which includes the indicators' `source` calls
       (`StatusLine.tsx` → `client.asExtension(id).rpc`).
2. **The page files are out of reach.**
   - Each extension's UI is served by `ExtensionOrigin`
     (`src/igni_core/extensions/origin.py`) on its own **ephemeral**
     `127.0.0.1` port.
   - In the VM, the node agent can reach only the ports the guest agent
     forwards: `--forward-port`, which defaults to 8765, igni's WebSocket
     (`crates/guest-agent/src/main.rs:20`, `docs/guest-protocol.md`).
   - `list_extensions` reports that VM-internal address as `origin`. On the
     desktop the address means nothing, or worse: it means some other
     local port.
3. **The desktop can frame only loopback.** The CSP is
   `frame-src http://127.0.0.1:* http://localhost:*` (`tauri.conf.json`),
   and §3 keeps it that way.

**What already works and needs no change:**

- The relay (§3) and the gateway (`crates/gateway/src/relay.rs`) pass
  frames through unchanged. So `host_model`, `host_signal`,
  `extension_service_token` and `list_extensions` already travel, once
  igni accepts the caller.
- Replies that carry a secret or one extension's data are addressed to
  the connection that asked (`Message.address_to`). One gateway client
  connection is one window connection.
- **The host object model "over the gateway"** for pages is the
  `host_model` RPC on the client protocol (`pool_rpcs/host_model.py`). No
  host-model endpoint is added to the gateway.
  - `CLOUD.md` §3's typed `client.host` for *programs* (a host-model
    endpoint without a `caller`) is a different feature. It stays out of
    scope.
- **The service token's exchange works from a VM.**
  - `ServiceTokenExchange` trades the VM's own access token
    (`EnvironmentCredentials`) as `subject_token`.
  - igni server's exchange (`app/services/identity/token_exchange.py`)
    takes any token with a `user_id` as a person's session, and the
    holder of a person's VM token gets a person token. App and space VMs
    already do this (`packages/igni-cloud/tests/test_an_apps_vm_gets_its_services_tokens.py`).
  - Only the `caller` refusal stands in the way.

### 15.2 The page mark

**Decision.** A connection speaks for an extension in the cloud only if
the node agent marked its stream `pages`. The mark is attested exactly as
the control mark is: the node agent strips every `X-Igni-Gateway` header
from a client's upgrade, then adds the one value the gateway asked for.
igni reads the mark off the connection's handshake, never off a message.

**Wire contract, client → gateway (igni__cloudvm).**

- **The query.** `GET /v1/connect?pages=1[&sign_in=1]`, WebSocket.
  - `pages=1` or `pages=true` asks for a page-host connection. Any other
    value, or no value, means no.
  - It is parsed like `sign_in`, in `ConnectQuery::pages()`.
- **Who may ask.** A person's token only. An app or a space has no
  windows.
  - `pages=1` with any other kind of token is refused **before the
    upgrade**: HTTP `403 {"error":"not_a_person","error_description":"only a person's desktop hosts extension pages"}`.
- **The rest is unchanged.** Auth, the Origin rule (§9.4), the close codes
  and the subprotocol echo (`igni.v1`) all stay as they are.
- **Reconnects keep the mark.** The gateway remembers `pages` for the
  life of the client connection. Every `igni_ws_timed` it makes for that
  connection asks for the mark, including the reconnects behind the
  socket after a node move.
- **Old nodes.** If the node does not advertise the capability
  `page_host_mark`:
  - the gateway connects **without** the mark;
  - it counts `page_mark_unsupported_total`;
  - it logs once per connection.

  Chat still works. igni refuses the extension calls (below), and the
  frame shows that refusal. Merging igni__cloudvm before igni-app (§13)
  keeps this to mixed-version clusters.

**Wire contract, gateway → node agent.**

- **The query.** `GET /v1/producers/{id}/stream?port=8765&mark=<m>`, where
  `m` is one of:
  - `control`: the gateway's own stream;
  - `pages`: a page host's stream.
  - `control=1` stays as an alias of `mark=control`, for gateways older
    than this change.
- **Refusals.** Each is `400` from the node agent:
  - `control=1` together with `mark=pages`;
  - an unknown `mark`;
  - two `mark` parameters.
- **What the node agent writes.** In `rewrite_upgrade_head`, every
  `X-Igni-Gateway` header from the client is dropped, as today. Then
  exactly one header is added:
  - `X-Igni-Gateway: control` for `control`;
  - `X-Igni-Gateway: pages` for `pages`;
  - none for no mark.
- **The capability.** `page_host_mark` joins `Node::capabilities()`.
- **A page-file stream (§15.3) never takes a parked warm stream.**
  - The stream query gains `warm=0`. Absent, it means `1`, which is
    today's behaviour.
  - `open_stream` gains `take_warm: bool`, read from that parameter.
  - With `take_warm=false`, a parked stream is neither taken nor
    aborted. Today a port mismatch **aborts** it (`node.rs:856`), so the
    page route must not touch it at all; the parked stream stays for the
    relay.

**Wire contract, inside igni (igni-core).**

| Piece | Change |
|---|---|
| `WebSocketTransport` (`transport/websocket.py`) | `_is_control` becomes `_mark_of(conn) -> StreamMark`, where `StreamMark` is `NONE`, `CONTROL` or `PAGES`. It reads `X-Igni-Gateway`, trimmed and lowercased: `control` gives `CONTROL`, `pages` gives `PAGES`, anything else gives `NONE`. `_handler` calls `msg.mark_control()` or `msg.mark_page_host()` from it |
| `Message` (`protocol/schemas/envelope.py`) | Adds `_via_page_host: bool` as a `PrivateAttr`, the `via_page_host` property and `mark_page_host()`, beside `_via_control`. It is never serialised and never read from a message |
| `Layer` (`core/layer.py`) | Adds `extension_calls_need_page_mark() -> bool`. The desktop answers `False`. The docstring of `connections_speak_for_extensions` says the mark may narrow it |
| `CloudLayer` | `connections_speak_for_extensions()` → `True`, and `extension_calls_need_page_mark()` → `True`. The comment says why: the node agent attests the mark |
| `MessageAdmission._caller_refusal` | After the existing check, when `extension_calls_need_page_mark()` is true and `not message.via_page_host`, it refuses with `"{method}: only a connection that hosts extension pages speaks for an extension"`. A control stream carries `CONTROL`, never `PAGES`, so the gateway's own stream still never speaks for an extension. `authorise_caller` runs unchanged after the check |

On the desktop nothing changes. The layer does not ask for the mark, and
an `X-Igni-Gateway` header a desktop client sends is read and ignored by
admission, as `control` already is.

**Security argument.**

1. **Unforgeable from outside the VM.** No client can set the header: the
   node agent removes it from every client stream and adds it only on the
   gateway's word. The gateway gives that word only after it has verified
   a person's session token, the same check every connection already
   passes.
2. **No new authority.**
   - Whoever holds the person's session token can already:
     - exchange it at igni server for a token for **any** service URL
       (`token_exchange.py`: "the session is the credential, and whoever
       holds it has everything already");
     - drive the person's igni as a window. Every Fleet connection is a
       window today: the cloud backend takes no token.
   - What the mark unlocks is a subset of that:
     - an extension's own `ext.*` methods;
     - the host model under that extension's grants;
     - a token for that extension's own service.
   - The mark makes "speaks for an extension" a deliberate, attested
     property of a connection. It does not hand out power the token did
     not already carry.
3. **Programs that did not ask stay out.** igni-client, the gateway's
   control stream and any other Fleet client never send `pages=1`, so a
   `caller` on their connections is still refused. That keeps the
   desktop's rule ("a program decides nothing for the person, and speaks
   for no extension") as close as the cloud allows.
4. **`authorise_caller` still decides what a page may do.** The extension
   must be installed, and its page may call only its own methods, the
   host model, its service token, and the base methods its manifest
   grants.
5. **The known gap stays known, and gets no wider.**
   - A process inside the VM can reach `127.0.0.1:8765` directly and send
     either header itself (`docs/SECURITY.md`, "Known, not yet closed").
   - Such a process is already a window, which is more than a page.
   - The fix named there (the guest bridge holds a launch secret) closes
     both marks at once, because both are read the same way, off the
     handshake.
   - The igni-core lane adds the page mark to that bullet.

**Plugs into §3.** The relay's upstream dial becomes
`wss://<gateway>/v1/connect?pages=1` (`&sign_in=1` when §8 asks for it),
for every window connection. All windows host pages, so there is no
per-window choice.

**Plugs into §4.** There is no new state. A `403 not_a_person` on the
upgrade cannot happen for a desktop session. If it does, it is
`Refused("not_a_person")`.

### 15.3 Page files: the gateway route and igni's page answer

**Decision.**

- The page files travel over the one port the guest already forwards:
  **igni's own WebSocket port, 8765.** A plain `GET` under a reserved
  prefix is answered as HTTP by the transport's `process_request` hook,
  before any upgrade.
- The gateway fetches them on a stream marked `pages`, behind a new
  public route.
- The shell serves them to the frame from a loopback origin per
  extension (§15.4).

**Why port 8765 and not a second forwarded port.** A second port (say
8766, with the guest agent forwarding it) would serve whatever listens
there. If igni ever restarted, a process the agent started could take the
port first. Its pages would then be framed by the desktop and handed the
bridge as that extension. On 8765, pages come from the process that holds
igni's socket: the same trust anchor as the chat. No guest-agent or image
change is needed.

**Wire contract, the gateway's public route (igni__cloudvm).**

```
GET /v1/pages/{extension_id}/{path…}
Authorization: Bearer <person session>
```

| Rule | Answer |
|---|---|
| No bearer, or a bad one | `401` / `503`, as `authenticate` already answers |
| A token that is not a person's | `403 {"error":"not_a_person"}` |
| An `Origin` header not in `public.allowed_origins` (§9.4) | `403`, as the connect route |
| `extension_id` does not match `^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$` (igni-core's `EXTENSION_ID`) | `400 {"error":"bad_extension_id"}` |
| The raw path, once percent-decoded, has a `..` or `.` segment, a NUL, a `\`, or `//`, or the raw path is over 2048 bytes | `400 {"error":"bad_page_path"}` |
| The producer is not running (`gw_producers`, the same read as `/v1/me`'s `vm.state`, §2.2) | `409 {"error":"not_running"}`. **The route never wakes a VM.** A page is fetched only while the window's own connection holds the VM awake |
| The node does not advertise `page_host_mark` | `501 {"error":"pages_unsupported"}` |
| The node answers 404 on the stream (the VM went away) | `409 {"error":"not_running"}` |
| Stream or HTTP failure inside | `502 {"error":"page_failed"}` |
| igni's response is over `public.max_page_bytes` (default 32 MiB) | The body is cut and the connection reset. If the over-size `Content-Length` is known up front, it is `502 page_failed` |
| Any method but `GET` | `405` (axum) |

**What the gateway sends and returns.**

- **Upstream.** The gateway opens `nodes.stream(url, p, stream_port,
  Mark::Pages)` with `take_warm=false`, then writes exactly:
  ```
  GET /.igni/pages/{extension_id}{raw path} HTTP/1.1\r\n
  Host: 127.0.0.1:{stream_port}\r\n
  Connection: close\r\n
  User-Agent: igni-gateway\r\n
  \r\n
  ```
  - The raw path keeps the client's percent-encoding. The query string is
    dropped.
  - **No client header is forwarded**: no cookie, no authorisation, no
    Origin.
  - The response is read with `hyper::client::conn::http1` over the
    upgraded stream.
- **Downstream.** The status and only these response headers are passed
  on, and the body is streamed:
  - `Content-Type`;
  - `Content-Length`;
  - `Content-Security-Policy`;
  - `X-Content-Type-Options`;
  - `Cross-Origin-Resource-Policy`;
  - `Cache-Control`;
  - `Referrer-Policy`.
- **Metrics:** `page_requests_total{status}` and `page_ms`.

New config, both with defaults:

- `public.max_page_bytes` (32 MiB);
- `public.page_timeout_s` (30).

**Wire contract, igni's answer (igni-core).**

- **Where it lives.** `WebSocketTransport` takes an optional `pages:
  PageFiles` (new, `src/igni_core/extensions/page_files.py`).
- **The hook.** Its `process_request` wraps the guard's:
  1. If `pages` is set, **and** the request has no `Upgrade` header,
     **and** its path starts with `/.igni/pages/`, then:
     - the request must carry `X-Igni-Gateway: pages`. Without it the
       answer is a plain `404`, the same answer as an unknown file;
     - the request must name a `Host` of `127.0.0.1:<bound port>`, or the
       answer is `421`;
     - `PageFiles.answer(extension_id, target)` gives the reply.
  2. Every other request goes to `HandshakeGuard.process_request`,
     unchanged.
- **`PageFiles.answer`:**
  - looks up the extension's `ExtensionOrigin` with a new
    `ExtensionOrigins.get(extension_id)`. No origin gives `404`;
  - calls `ExtensionOrigin.answer("GET", target)`. That method is
    extracted from `_reply_to`: the same file resolution, the same refusal
    of escapes and symlinks out of the root, the same content-type table
    and the same CSP. `_reply_to` keeps the Host check and the head
    parsing, then calls it;
  - turns the `HttpReply` into a `websockets` `Response`. The headers are
    exactly `HttpReply.headers()`, so the CSP's `frame-ancestors` is
    still `frame_ancestor_sources()`: the Tauri origins and loopback;
  - holds the body in memory, because `websockets` cannot stream one. A
    file over `PageFiles.MAX_BYTES` (32 MiB) answers `404`, and the log
    line says why.
- **Only `GET`.** `websockets` never hands a `POST` or `HEAD` to the hook
  (`origin.py`'s note), so nothing else is answerable.
- **Which layers serve pages.** A new `Layer.serves_page_files() -> bool`
  decides whether `PageFiles` is built at all. The desktop answers
  `False`, so a desktop backend's port never serves files. `CloudLayer`
  answers `True`.
  - `TransportBuilder` (`backend/transport_builder.py`, called from
    `BackendApp._listen`) builds the transport with `pages=PageFiles()`
    when the layer says so.
  - The transport listens before the extension runtime exists, so
    `PageFiles` starts unbound and answers `404`.
  - `BackendApp._assemble` binds it to `ExtensionsRuntime.origins` with
    `PageFiles.bind(origins)` once the runtime is built.
- **Unchanged:** the per-extension listeners in the VM. They keep running
  and keep answering `127.0.0.1:<ephemeral>` inside the VM, for anything
  in the VM that uses them.

**Security argument.**

- **The files are public to the person.** They are the bundle the
  extension's distribution shipped: not a secret. The gateway serves them
  only to the producer's own person token, on the producer's own VM. The
  route never wakes, so it cannot be used to run up compute.
- **Resolution is the tested code.** File resolution stays in
  `ExtensionOrigin`, whose escape, symlink, `//` and NUL refusals are
  already covered.
- **Defence in depth.** The gateway refuses traversal and bad ids before
  opening a stream.
- **The mark is required on page-file requests too.** Fetching pages and
  speaking for an extension are the same attested property of a stream.
- **No header crosses.** The gateway forwards no client header up and
  only the allow-list down, so no cookie, credential or hop header
  crosses either way.

### 15.4 The shell's page origins (igni-app)

**Decision.**

- In cloud mode the shell serves each extension's page from a loopback
  origin of its own, `http://127.0.0.1:<port>`, one listener per
  extension.
- Each listener proxies `GET` to the gateway route of §15.3, with the
  session token added in Rust.
- This is the same "one origin per extension" isolation `origin.py`
  argues for. The CSP stays loopback-only (`config_contracts.rs`
  unchanged).

**Tauri command.**

```
cloud_page_origin(extension_id: String) -> Result<String, String>
```

- `Ok("http://127.0.0.1:<port>")`. The listener for that extension is
  started on first ask, and the same port is returned for the life of
  the cloud side.
- `Err(…)` in three cases:
  - the extension id does not match the `EXTENSION_ID` pattern;
  - the active side is not the cloud: `"not in cloud mode"`;
  - the listener could not bind.
- It never returns or takes a token.

**The listener** (`src/cloud/pages.rs`, next to the relay of §3):

| Rule | Behaviour |
|---|---|
| Bind | `127.0.0.1:0`, one per extension id. Never off-host |
| Method | `GET` only. Anything else gets `405` with `Allow: GET` |
| `Host` | Must equal `127.0.0.1:<port>`, or the answer is `421`. This stops DNS rebinding, as `ExtensionOrigin._host_is_ours` does |
| Head | At most 16 KiB, read within 10 s, then `431` or `408`. One request per connection, `Connection: close` |
| Path | The query is dropped. The raw path is checked by the same rules as the gateway's (`..` or `.` segments, NUL, `\`, `//`, over 2048 bytes give `404`). It is then forwarded as `GET {gateway_url}/v1/pages/{extension_id}{raw path}` |
| Upstream headers | `Authorization: Bearer <session>`, read from `credentials.json` **on every request**, as the relay reads it on every dial. Plus `User-Agent: igni-desktop/<version>`. **No header from the frame is forwarded.** No `Origin` is sent, so the gateway's Origin rule (§9.4) lets it in as a program |
| TLS, proxy | The relay's own client settings: rustls with the platform roots, `HTTPS_PROXY` and `NO_PROXY`, and an `https` gateway only outside a debug build (§9.2, Q10). One shared `CloudHttp` value builds both |
| Response headers | Only the allow-list of §15.3 is copied. **A `200` without a `Content-Security-Policy` is refused** with `502`: the shell never serves a page whose CSP igni did not write. `X-Content-Type-Options: nosniff`, `Cross-Origin-Resource-Policy: same-origin`, `Cache-Control: no-store` and `Referrer-Policy: no-referrer` are always set, whatever came back |
| Status mapping | `200` and `404` pass through. `401`, `403`, `409`, `5xx` and network errors become `503` with `Retry-After: 2` and a one-line text body. **A page fetch never moves the state machine**: the relay's dial is the one source of state (§4.2) |
| Size and time | The body is streamed. A body over 32 MiB is cut. 10 s to connect, 60 s in all |
| Logs | The extension id, the path length, the status and the time. **Never the token, never the path's content** (a log-capture test) |

**Lifecycle (plugs into §4.2).**

- `PageOrigins` is owned by the shell's backend-state owner, beside the
  relay.
- `cloud_page_origin` answers only while the active side is the cloud:
  `Dialing`, `Waking`, `CloudReady`, `CloudNeedsSignIn` or
  `CloudUnreachable`.
- On any transition **into** `Local`, `Refused(_)` or
  `NeedsDesktopSignIn`, and whenever `credentials.json` changes to
  another `iss|org|sub`, every page listener is stopped with
  `PageOrigins::stop_all()`.
- So a port never outlives the account and gateway it served. The next
  cloud side gets new ports, which means new origins and a fresh
  page-side `localStorage`. That matches local, where `ExtensionOrigin`'s
  ports change on every backend start.
- A node move (a second `welcome`, §4.3) leaves the listeners alone: the
  origin is the shell's and stays stable.

**Plugs into §3.** The relay and the page listeners share the gateway
URL, `CloudHttp` and the credentials reader. They are started and
stopped together.

**Security argument.**

1. **The token stays in Rust**, as in §9.1. It goes only to the
   `gateway_url` that `api_url` returned (§9.2). No command, URL or
   response header carries it.
2. **Origin per extension.** Two cloud extensions are two origins, and a
   cloud extension and the same extension locally are two origins too.
   The frame keeps `SANDBOX = "allow-scripts allow-forms allow-same-origin"`
   and is never same-origin with the app.
3. **What another local process can do with the port.** It can read the
   extension's bundle files, which it can do locally too through
   `ExtensionOrigin`. It cannot read anything else: the path is confined
   to `/v1/pages/{this extension}`, and igni resolves it inside the
   bundle's root.
   - A web page in the person's browser cannot read responses across
     origins: there are no CORS headers and CORP is `same-origin`.
   - Rebinding is closed by the Host check.
4. **The CSP is igni's, as locally.** The person's own igni writes
   `frame-ancestors`, `connect-src 'self' <service> <egress>` and the
   rest. The shell refuses a page without one. `'self'` is now the
   shell's origin, which serves only that extension's files.

### 15.5 The extension list, indicators and frames (igni-app web client)

**The list.**

- `list_extensions` and `set_extension_settings` are plain RPCs. They
  travel through the relay unchanged.
- **In cloud mode the backend's `origin` is discarded, unconditionally,
  before anything stores it.** It is a VM-internal address. Framed on the
  desktop, it would load whatever local server owns that port, and the
  bridge would then talk to it as the extension.
- New file `clients/web/src/extensions/cloudPageOrigins.ts` exports
  `localiseOrigins(listed, side)`:
  - locally, it returns the list as it is;
  - in the cloud, every non-empty `origin` is replaced by
    `invoke("cloud_page_origin", {extensionId})`;
  - a failed invoke gives `origin: ""`. `ExtensionPage` already shows
    "is READY but its UI is not being served" for that case.
- `useExtensions.ts` passes every list through it: the one fetched on
  connect, **and** every `setExtensions` the settings panel makes. The
  returned `setExtensions` localises before it stores, so no path
  bypasses it.

**Frames.**

- `ExtensionPage` mounts `ExtensionFrame` in the cloud only while the
  backend state is `CloudReady`. In other states the §4.2 overlay or
  banner covers the page, and no frame loads against a VM that cannot
  answer.
- `ExtensionPageHost.tsx` keys each frame by the client's connection
  generation (incremented on every `connected`, including the
  second-`welcome` resync of §4.3).
  - After a node move, the frame remounts with a new nonce, sends `hello`
    again and re-subscribes. Its subscriptions lived in the igni process
    that went away.
  - Locally the same key remounts frames after a backend restart, which
    is correct there too.
- `ExtensionFrame`'s four checks are unchanged:
  - the window;
  - the origin, which is now the shell's origin;
  - the nonce;
  - the method.

**Indicators, menu items, `host_signal`.** No change in the web client.
They go through `client.asExtension(id)`, so they work as soon as §15.2
lands. The phase-1 code that would have hidden extensions in cloud mode is
**not written**: §5 says "Works".

**The service token** (`bridge.service`). There is no change in the web
client or the bridge:

1. The window forwards `extension_service_token` with the frame's
   extension as `caller`.
2. The relay and the gateway pass it through.
3. The VM's igni exchanges its own access token for a token whose
   audience is the service recorded on that extension's origin.
4. The answer is addressed to the asking connection only, so it is never
   broadcast to the person's other Fleet clients.
5. The page then reaches its service **directly from the desktop
   webview**, as locally, under the `connect-src` its CSP admits.

### 15.6 Security summary

| Threat | Closed by |
|---|---|
| A Fleet client (igni-client, the control stream) speaks for an extension | No `pages=1`, so no mark, so `caller` is refused (§15.2) |
| A client forges `X-Igni-Gateway: pages` | The node agent strips it from every client stream (§15.2) |
| An app or a space asks for a page host | `403 not_a_person` before the upgrade |
| A web page drives the person's cloud extensions | The relay's door, an Origin and per-launch token (§9.3). The page origins give only static bundle files, with no CORS (§15.4) |
| The desktop frames a VM-internal port | The web client discards the backend's `origin` in cloud mode (§15.5) |
| A squatter in the VM serves pages | Pages are served only by the process holding igni's socket (§15.3) |
| A page reaches another extension's data | One origin per extension, and `authorise_caller` with the frame's `caller` (unchanged) |
| The session token reaches a page | It stays in Rust: the relay header and the page proxy header only (§15.4) |
| A service token reaches another client | It is addressed to the asking connection (`Message.address_to`, unchanged) |
| `host_signal` reaches other Fleet clients | Broadcast to every connection of the person's own igni, as on the desktop. Values are filtered by the extension's grants, and every recipient is the same person. `docs/SECURITY.md`'s "in the cloud nothing can subscribe" is rewritten to say this |
| A process inside the VM forges a mark | Known and unchanged (`docs/SECURITY.md`). Such a process is already a window, and the launch-secret fix closes both marks |

**No model-facing text changes.** The refusal strings, HTTP error bodies
and banners are person- or program-facing. Prompts, tools, injected
blocks and result formats are untouched. `make contracts` must show no
diff: no RPC, no wire message and no host-schema member is added.

### 15.7 Files, by repo, and the tests

#### igni-core

**Files.**

| File | Change |
|---|---|
| `src/igni_core/protocol/schemas/envelope.py` | `_via_page_host`, `via_page_host`, `mark_page_host()` |
| `src/igni_core/transport/websocket.py` | `StreamMark`, `_mark_of`, a `PAGE_HOST_VALUE = "pages"` constant beside `CONTROL_VALUE`. `pages: PageFiles \| None` and the `process_request` wrapper |
| `src/igni_core/extensions/page_files.py` (new) | `PageFiles`: the `/.igni/pages/` prefix, the mark and Host checks, the size cap, `HttpReply` → `Response` |
| `src/igni_core/extensions/origin.py` | Extract `ExtensionOrigin.answer(method, target)` out of `_reply_to`. Add `ExtensionOrigins.get(extension_id)` |
| `src/igni_core/core/layer.py` | `extension_calls_need_page_mark()` and `serves_page_files()`, both `False` |
| `packages/igni-cloud/src/igni_cloud/layer.py` | `connections_speak_for_extensions()` → `True`, plus the two seams → `True`. The module docstring gains an "extension pages" bullet |
| `src/igni_core/backend/message_admission.py` | The page-mark refusal (§15.2) |
| `src/igni_core/backend/transport_builder.py`, `src/igni_core/backend/app.py` | The builder gives the transport a `PageFiles` when `serves_page_files()` is true. `_assemble` calls `PageFiles.bind(extensions.origins)` |
| `docs/SECURITY.md` | The table row "in the cloud, none" becomes "in the cloud, only a connection the node agent marked `pages`". The sentence about a Fleet UI needing a mark becomes a description of the mark. The `host_signal` paragraph changes. The known-gap bullet names both marks |
| `docs/CLOUD.md` | §3: "igni's web UI … extension pages load here" now says how. The VM-credential paragraph's "No connection speaks for an extension" becomes the mark rule |

**Tests (pytest).** Each fails on today's code.

- `packages/igni-cloud/tests/test_in_the_cloud_no_connection_speaks_for_an_extension.py`
  is **replaced** by `test_in_the_cloud_only_a_page_host_speaks_for_an_extension.py`:
  - `host_model`, `extension_service_token` and `ext.acme.reviewer.search`
    with a `caller`, on an unmarked connection, are refused with the new
    text, addressed to that connection, and no handler is reached;
  - the same on a control-marked connection: refused;
  - the same on a page-marked connection: `handle_page` is reached, the
    token RPC answers, and the `ext.*` call is routed;
  - page-marked, with an extension that is not installed: refused by
    `authorise_caller`;
  - page-marked, with `attach_session` and a `caller`: refused as
    pool-level;
  - `CloudLayer().extension_calls_need_page_mark() is True`, and the
    desktop's is `False`.
- `packages/igni-cloud/tests/test_a_page_host_is_marked_by_its_handshake.py`,
  beside `test_platform_calls_come_on_the_control_stream.py`:
  - `X-Igni-Gateway: pages` → `via_page_host`, not `via_control`;
  - `control` → the reverse;
  - `Pages`, ` pages `, `page` and `pages,control` → `pages`, `pages`,
    neither and neither;
  - no message field can set either mark.
- `tests/backend/test_a_desktop_caller_needs_no_mark.py`: on the desktop
  layer an unmarked window's `caller` is honoured as today, and a
  `pages` header changes nothing.
- `tests/extensions/test_page_files.py`, against a real
  `WebSocketTransport` with a real `ExtensionOrigins`:
  - `GET /.igni/pages/acme.reviewer/index.html` with the mark and the
    right Host answers `200`. It carries the extension's CSP, with
    `frame-ancestors` equal to `frame_ancestor_sources()`, plus
    `nosniff`, CORP and `no-store`;
  - the same without the mark: `404`;
  - a wrong Host: `421`;
  - an unknown extension, `..`, a symlink out of the root, `//x`, or a
    NUL: `404`;
  - a file over the cap: `404`;
  - a directory: its `index.html`;
  - with the layer's `serves_page_files()` false, the same request gets
    the guard's answer (`403` or a failed upgrade), never a file;
  - a normal WebSocket upgrade on the same port still connects;
  - a reinstall that changes the root serves the new files.
- `tests/extensions/test_extension_origin_answer.py`: the extracted
  `answer()` gives byte-identical replies to `_reply_to` for the existing
  origin cases. This is a parametrised table over the current origin
  tests' fixtures.
- `packages/igni-cloud/tests/test_a_persons_vm_gets_its_pages_service_tokens.py`:
  - a person's VM token in `IGNI_ACCESS_TOKEN_FILE` and a fake igni
    server token endpoint;
  - a page-marked `extension_service_token` answers `ok: true`, with
    `service_url` equal to the origin's service;
  - the exchange posted `subject_token` = the VM token and the service as
    the audience;
  - `refresh: true` exchanges again;
  - no service → `ok: false`.
- `make contracts`: no diff.

The full gate is `make lint` plus the Linux core-only test job in a
container (memory: "full CI locally before pushing").

#### igni__cloudvm

**Files.**

| File | Change |
|---|---|
| `crates/node-agent/src/api.rs` | `StreamQuery.mark`, an `enum Mark { None, Control, Pages }` parsed from `mark` and the `control` alias, the 400 rules. `rewrite_upgrade_head(r, mark)` writes the one header |
| `crates/node-agent/src/node.rs` | `open_stream(p, port, take_warm)`: when `take_warm` is false, the parked entry is neither taken nor aborted. `"page_host_mark"` joins `capabilities()` |
| `crates/gateway/src/node_client.rs` | `stream(base, p, port, mark: Mark, take_warm)` sends `&mark=control` or `&mark=pages`, and `&warm=0` when `take_warm` is false. `control` still goes as `&control=1` to a node without `page_host_mark`, so a new gateway works with old nodes |
| `crates/gateway/src/control.rs` | `igni_ws_timed(url, p, mark)` |
| `crates/gateway/src/api.rs` | `ConnectQuery::pages()`, the `not_a_person` refusal, `pages` handed to `relay`. The new `GET /v1/pages/{extension_id}/{*path}` handler (`pages.rs`) |
| `crates/gateway/src/pages.rs` (new) | The route of §15.3: checks, the running check, the stream, the HTTP/1 request, the header allow-list, the cap and the metrics |
| `crates/gateway/src/relay.rs` | `relay(…, pages: bool, …)`. Every `igni_ws_timed` asks for `Mark::Pages` when the node has `page_host_mark`, otherwise `Mark::None` plus the metric |
| `crates/gateway/src/config.rs` | `public.max_page_bytes`, `public.page_timeout_s` |
| `docs/gateway.md`, `docs/node-agent.md` | The route, `pages=1`, `mark=`, the capability, the new 400s and the metric names |

**Tests (cargo; `GATEWAY_TEST_DATABASE_URL` for the gateway).**

- Node agent, `api.rs` unit tests:
  - `rewrite_upgrade_head` with `Mark::Pages` adds exactly one
    `X-Igni-Gateway: pages` and drops a client's `control` and `pages`;
  - `Mark::None` adds none;
  - the `mark` parse table: `control`, `pages`, the `control=1` alias,
    both together → 400, unknown → 400, repeated → 400;
  - `capabilities()` contains `page_host_mark`.
- Node agent, `node.rs`: with a parked warm stream, `open_stream(…,
  take_warm=false)` leaves it parked, and the next `take_warm=true` takes
  it. This test fails on today's abort.
- Gateway, `tests/api.rs` and `tests/cluster.rs` with the fake node:
  - `connect?pages=1` with a person's token: the fake node sees
    `mark=pages`;
  - with an app token: `403 not_a_person` and no stream;
  - without `pages`: no mark;
  - after a node move (the existing reconnect case) the second stream is
    `mark=pages` too;
  - a node without `page_host_mark`: no mark, and the metric counts.
- Gateway, the pages route:
  - no bearer → 401;
  - an app token → 403;
  - a bad id → 400;
  - `..`, `%2e%2e`, `%00` and `//` → 400;
  - a producer not running → 409, with **no `ensure-running` call** on
    the fake node;
  - a running producer: the fake node gets `mark=pages&warm=0`, the
    request head on the stream is exactly the four lines of §15.3, with
    no client header (the test sends a cookie and an `X-Igni-Gateway`
    and asserts neither arrives), and only the allow-listed response
    headers come back;
  - an over-cap body → 502 or a cut;
  - an unlisted `Origin` → 403;
  - a node without the capability → 501.

The gate is the repo's own `cargo fmt --check`, `cargo clippy -D
warnings` and `cargo test` for the three crates, run locally (CI minutes
are a budget).

#### igni-app

**Files.**

| File | Change |
|---|---|
| `clients/tauri/src-tauri/src/cloud/relay.rs` (the §3 relay) | The dial URL gains `pages=1`, and `403 not_a_person` maps to `Refused` |
| `clients/tauri/src-tauri/src/cloud/pages.rs` (new) | `PageOrigins`: the listener of §15.4, `start_for(id)`, `stop_all()`, and the header allow-list and CSP rule |
| `clients/tauri/src-tauri/src/cloud/http.rs` (new, or wherever the relay keeps its client) | `CloudHttp`: rustls, platform roots, proxy and the https rule, shared by the relay and the pages |
| `clients/tauri/src-tauri/src/cloud/state.rs` (the §4.2 owner) | Holds `PageOrigins`, and calls `stop_all()` on the transitions of §15.4 |
| `clients/tauri/src-tauri/src/lib.rs` | Registers `cloud_page_origin` |
| `clients/tauri/src-tauri/Cargo.toml` | `hyper` 1 and `hyper-util` (server, http1) for the listeners, if the relay lane has not added them. The HTTP client is the one `CloudHttp` uses |
| `clients/web/src/extensions/cloudPageOrigins.ts` (new) | `localiseOrigins` |
| `clients/web/src/extensions/useExtensions.ts` | Localises on fetch and in `setExtensions` |
| `clients/web/src/extensions/ExtensionPage.tsx` | Mounts the frame in the cloud only in `CloudReady` |
| `clients/web/src/extensions/ExtensionPageHost.tsx` | Keys frames by the connection generation |
| `clients/tauri/src-tauri/tauri.conf.json` | **Unchanged.** `config_contracts.rs` is unchanged and still passes |

**Tests.**

- Rust (`cargo test`), `pages.rs` against a stand-in gateway
  (`hyper`, in the test):
  - `GET /index.html` reaches `/v1/pages/acme.reviewer/index.html` with
    `Authorization: Bearer <token from a temp credentials.json>`, with
    no `Origin`, cookie or other frame header forwarded;
  - the token is re-read: change the file between two requests, and the
    second carries the new token;
  - `POST` → 405;
  - a wrong `Host` → 421;
  - `..` and `%2e%2e` → 404, and nothing reaches the gateway;
  - a `200` without a CSP → 502;
  - an extra upstream header (`Set-Cookie`) is not copied;
  - the forced headers are present;
  - `401`, `409` and `503` upstream → `503` with `Retry-After`;
  - an over-size body is cut;
  - a log capture never contains the token;
  - `cloud_page_origin` refuses a bad id and refuses outside cloud mode,
    and returns the same port twice;
  - `stop_all()` closes the port: connecting fails afterwards.
- Rust, the state-machine table test of §12.1 gains rows for each
  transition into `Local`, `Refused`, `NeedsDesktopSignIn` and an account
  change. Each asserts `stop_all()`. A second-`welcome` row asserts the
  ports are kept.
- Rust, relay: the upstream URL carries `pages=1` (with and without
  `sign_in=1`), and `403 not_a_person` → `Refused`.
- Web (vitest):
  - `localiseOrigins` in the cloud never returns the backend's origin.
    A failing invoke gives `""`, and locally the list is untouched;
  - `useExtensions`' `setExtensions` localises too;
  - `ExtensionPage` mounts no frame in the cloud outside `CloudReady`;
  - the frame remounts, with a new nonce, on a new connection
    generation.
- Playwright (mock backend, `cloud` fixture): an extension page renders
  in a frame whose `src` is the shell stub's origin, and an indicator
  shows. New visual baselines are made with `scripts/baselines-linux.sh`.
- Desktop CI (the §12.1 Rust integration test) grows by one step. The
  stand-in gateway relays to a real igni-core backend running with a test
  layer whose `extension_calls_need_page_mark()` and
  `serves_page_files()` are true. The step installs a test extension with
  a page, then asserts:
  - the frame's file arrives through `cloud_page_origin`;
  - a `host_model` `get` answers;
  - `extension_service_token` answers `ok`.

**Estimate.**

| Repo | Days |
|---|---|
| igni-core | 2.0 |
| igni__cloudvm | 2.5 |
| igni-app | 4.0 |
| Live E2E | 0.5 |
| **Total** | **9.0** |

igni-server and igni-sdk change nothing for §15. The exchange that a
person's VM token goes through already exists. The live step below
proves it against the real server.

### 15.8 Live E2E (step 11 of §12.2)

On the ember-dev host, with the person in `local_and_cloud`. igni server
lists the starter extension (`igni__extension-starter`), built with:

- a page;
- an indicator;
- a `sessions` host grant;
- a service.

Steps:

1. **The extension shows.** Switch to Cloud. The extension is listed and
   its indicator shows. The page opens in a frame whose origin is
   `http://127.0.0.1:<shell port>`, not the VM's port.
2. **The host model answers.** `bridge.host` `get` on `sessions` answers,
   and a subscription's `host_signal` arrives after a chat turn.
3. **The service token works.** The page's service accepts the token,
   and the service's log shows `aud` = its URL and the person's `sub`.
4. **Programs still speak for no extension.** An igni-client connection
   to the same gateway (no `pages=1`) sending `host_model` with a
   `caller` is refused with the page-mark text.
5. **A forged header is stripped.** A hand-made connect that sends
   `X-Igni-Gateway: pages` without `pages=1` is refused in the same way.
6. **Node move.** Drain the node. The frame remounts, re-subscribes, and
   the next signal arrives.
7. **Back to local.** Flip to This computer. The shell's page port no
   longer answers, and the local extension's page loads from igni-core's
   own origin.
8. **No token in the webview.** The devtools search of §12.2 step 10 also
   covers frame URLs and the page proxy's responses.
