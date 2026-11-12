# Things worth doing, not yet done

Ideas that survived a conversation but not a schedule. Each entry says
what to build, and — more importantly — what evidence made it look
worth building, so a later reader can decide whether the reason still
holds.

---

## Run the group-config checks against a live server in CI

**What.** Wire `scripts/check_group_config.py` and
`scripts/check_group_session.py` into the release-readiness workflow,
against a throwaway igni__server stood up in the job: start Postgres and
the API, seed a Default group holding one entry of every kind, run both
scripts, tear it down. Both already exit non-zero on failure, so they
can gate a release as-is.

**Why.** These two scripts close the one class of bug that unit testing
here has never caught: **wiring between components**. Each subsystem's
loader has tests, and each of those tests passes by handing the loader a
directory. Nothing asserted that the session actually *hands it that
directory*. Twice now that gap has shipped a real fault:

- Group MCP servers never reached the session at all. `MCPClientManager`
  built its own loader and `apply_to_mcp` only merged plugin servers.
  Every MCP unit test passed. Booting a real `Session` found it in
  seconds.
- The ETag on `/me/group/pack` could never match, because it hashed
  `fetched_at` — which is `now()`. The revalidation tests passed; they
  compared the client's behaviour against a fixture, not against a
  server computing its own tag.

Both are invisible to a mock and obvious to a live boot. The scripts
exist because of them; running them by hand only helps on the days
somebody remembers to.

**Where it belongs.** `release-readiness.yml`, not `ci.yml`. Same
argument the existing onboarding smoke makes for itself: it needs real
infrastructure, takes minutes not seconds, and the thing it catches
drifts on its own schedule rather than per-PR. Nightly plus on-tag is
the right cadence.

**The awkward part.** The job needs both repos — igni for the
scripts, igni__server for the thing to point them at. Either check out
igni__server as a second path in the same job, or publish a server
container image the job can `docker run`. The image is cleaner and
matches how a customer actually gets the server; the two-checkout
version is faster to write. Start with the checkout, move to the image
if it gets fragile.

---

## Reshape where configuration lives

See **[CONFIG_LAYOUT_PLAN.md](CONFIG_LAYOUT_PLAN.md)** — four decisions
that turn out to be one change: rename `.igni` to `.igni`, stop the
server's pack landing in the project, let a project file of the same
name win, and move everything sensitive to `~/.igni`.

The plan's verification section is a stronger argument for the CI item
above than the one written there: the failures it describes are a loader
whose search order is right in a fixture and wrong in a real session, a
credential resolving from the wrong tier after the config split, and a
home-directory migration that by definition runs once on a machine that
already has data. None of those are reachable from unit tests.

---

## A person uses their cloud VM from the desktop app, not only the local backend

**What.** The desktop app (igni-app, Tauri + web) talks to exactly one
backend: the local one, over loopback. A person whose group gives them a
Fleet VM has no app to use it with. Build the switch:

1. **Discovery.** After sign-in, igni server tells the app whether the
   person has a cloud VM: the gateway's address and the VM's state
   (running, asleep, needs sign-in). A new small endpoint; nothing lists
   a person's VM today, and nothing reads the advertised `igni_gateway`.
2. **A Local / Cloud switch**, shown only when a cloud VM exists.
   Flipping reconnects every window to the gateway's `/v1/connect` and
   reloads state; the local backend keeps running.
3. **Auth.** The web client sends the person's own token the way the
   gateway expects from a browser (subprotocol `igni.bearer.<token>`) and
   refreshes it; the Tauri shell hands the token over.
4. **Real time.** The VM waking is shown, the gateway's "the VM needs
   sign-in" push is handled, and the client reconnects when the gateway
   moves the session.
5. **Per-backend state.** The `client_id` localStorage key gets a backend id.
6. **Group mode in the portal: local only, cloud only, or both.** Carried
   in the signed group pack so the shell enforces it offline, and
   enforced by the gateway: a local-only group's person is refused;
   cloud-only means the desktop never starts a local backend.

**Why.** Fleet exists for people on paper only: the gateway is reachable
through the `igni-client` SDK and the tests, and the portal is for
admins. A real person signing in is also the first end-to-end proof of
Fleet — wake, sign-in, the session moving between nodes, the group mode —
all of which is tested today only piece by piece against mocks.

**The awkward part.** Extension pages: the gateway relays the client
protocol, not the host object model (`CLOUD.md`, the client-protocol
section). Local-only ideas — attachments, the project folder, "open in
editor" — need a cloud meaning. And only one side may pull
`/v1/me/events` at a time.

**Order.** Discovery and the switch for chat only (extensions hidden in
cloud) → VM wake, sign-in and status → the group mode in the portal and
its enforcement → extensions over the gateway → maybe both side by side.
Repos: igni-server (endpoint, group mode, portal), igni-app (shell and
web), igni-core (the event-pull rule, cloud-only startup), igni-sdk
(shared discovery). Write the design doc before any code.

---

## Seeing what an app does, without signing in as the app

**What.** An app (a producer an org admin registers, signing in only with
its own private key) runs on its own VM, and its spaces are usually its
own customers. Who sees what:

- **The app's owner sees content, through the SDK, as the app.**
  `igni-client` already reads back sessions, their turns, live events and
  the app's spaces. Add a small read-only command to the SDK — sign in
  with the app's key, list sessions, show one, follow one — so looking
  does not mean writing a script.
- **The org admin sees metadata in the portal, never content.** VM state,
  wake times and node; runs (count, duration, outcome, error class,
  tokens, model); tool calls by name and outcome, never arguments or
  results; pending approvals and who answered them; the licence band
  count; the audit trail. The data leaves the VM through an **allow-list
  of fields**, so text, files and tool arguments cannot reach the portal
  by accident. Write the list down: air-gapped customers will ask for it.
- **A person may answer an app's pending approval as themselves**, seeing
  only the pending call, recorded in the audit as that person. Today the
  app answers in its own code.

**Why not a login as the app in the desktop app.** It needs the app's
private key on a laptop, or a "person acts as app" path that breaks the
rule that an app acts only as itself. Whoever signed in would see every
conversation of the app and of all its spaces — exactly the sensitive
data — and the audit could no longer tell the developer from the
automated app.

**Where it belongs.** The command in `igni-client` (igni-sdk); the
allow-list where the gateway and the VM report (igni__cloudvm, igni-core
telemetry); the view in igni-server's portal. A section of the same
design doc as the cloud switch above.
