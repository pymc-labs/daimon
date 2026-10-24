# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Watch turns show their status card until the answer is posted; a brief Discord outage no longer pauses the watch.
- Preserve unattended sandbox restrictions across native providers: refuse workers and wakes that require automatic sandbox/custom tools or other ungated capabilities before a provider message; retain conversation-only workers, attended native execution and guarded selfhosted tools.

- Hide saved routine triggers/results from MCP, CLI and Discord controls when
  their source or destination becomes reserved for recruitment, including
  unknown legacy provenance. Restore this bot's `/recruitment` status, refresh,
  retry and discard commands inside reserved channels with live recruiter checks.
- Document the Discord reader registry as a syntax review ratchet with known
  limits and `TurnPrivacy` as a trusted-caller declaration contract; prepared
  turns prefer the admitted tenant over a repeated caller argument.

- Reserve configured recruitment parent channels and their threads for human
  staffing. Replace dynamic privacy filters and database configuration with
  restart-bound tenant settings; reject general classifiers, DM transfers and
  Discord MCP readers before content access. Fence confirmations with durable
  event watermarks, including new messages, and exclude own-card echoes.

- On the mux turn path, a turn that reaches a thread whose lease another running turn holds now waits for that turn instead of failing with `LeaseBusy`. The wait ends at the turn's own ceiling, honors Stop, and a holder that dies frees the thread when its lease expires. A turn whose session the holder replaced adopts the replacement.

- Reject crossed known Azure model/deployment aliases regardless of letter case.

- Derive Azure model versions from Sol/Luna selection, expose azd model selection, verify Luna-only live probes against response models, and allow manual judge model overrides.

- Parameterize Azure model deployments with a `gpt-6.1-sol` default and DataZoneStandard SKU; document `gpt-6-luna` smoke tests and align selfhosted profile, driver and CLI defaults.

- Hardened harness tool processes, caller authorization and MCP credential scoping; cache binary skill bundles and honor per-tool approval policies.

- Harness tool policies accept explicit denial and validated built-in, MCP tool and server wildcard keys.

- Validate binary harness skill bundles with shared SHA-256 digests and decoded size limits; require stable assistant message identities.
- Azure deployment templates now wire MCP chart storage to the provisioned Blob account and explicit managed identity, with account-scoped delegation-key permission and container-scoped data access.
- Azure deployment templates now default MCP chart storage to the provisioned Blob account and explicit managed identity, with account-scoped delegation-key permission and container-scoped data access. Set `chartStorage=external` for S3 installations to omit Blob defaults and roles; Blob mode refuses shared or MCP S3 secret mappings early.

- Hosted MCP chart links can use private Azure Blob Storage with managed identity and read-only user-delegation SAS. Select `DAIMON_ARTIFACTS__BACKEND=azure_blob` and set `DAIMON_ARTIFACTS__AZURE_CLIENT_ID` for an explicit managed identity, with no developer or service-principal fallback; S3 remains the default. A loopback-only Azurite connection string is supported for local development.
- Added selfhosted tariff admission and request settlement APIs with dated Azure model tariff history and frozen admission snapshots. Runtimes must call the admission gate before paid work; these APIs alone do not wire turn refusal. Unknown usage stays pending with reasons in the paginated `daimon usage pending` report. Deployment/model aliases share the admitted underlying-model identity across corrections. Corrupt tariff context keeps corrections durable and pending; accepted clock lag across midnight uses the frozen admission date. Native settlement requires measured completeness.
- Added host-side preparation for self-hosted MCP servers and complete text/binary skill bundles, with scoped credential references, explicit unsupported-skill skips, and revocable caller-bound MCP credentials kept outside turn bodies. Runtime integration remains opt-in.

- Added opt-in self-hosted harness chat turns with exact tenant/channel grants, persistent caller sessions, price admission before HTTP, request-level metering, MCP tools and turn credential cleanup.
- Added offline-validated Azure Bicep infrastructure and an azd entrypoint for staged Container Apps deployment, PostgreSQL, Key Vault, Blob, ACR and Azure OpenAI/Foundry.
- Added the opt-in `selfhosted.loop` HTTP driver with direct and Foundry invocation transports, durable event polling, approvals, cancellation and measured model-request usage. Resource ports are supplied by the host. MCP permissions are scoped by server, narrowed tool sets deny omitted tools, and credentialed MCP servers require a scoped host resolver. Completed turns remain readable with the default cursor, and pending approvals recover after connection errors. `DAIMON_SELFHOSTED__ADMISSION_ENABLED` defaults to false; enabling it preserves required usage checks.

- The GitHub repo page for an agent, its Add repos dialogs and the saved page are back on Daimon's own colours and header instead of a grey, black-button look. The working repo, added repos and checked rows are teal, repos to add are green, repos to remove are red, and anything GitHub could not load or show is amber with a warning icon and, where it helps, a Try again button. The dim layer behind a dialog covers the whole screen, and the Add 0 repos button reads as disabled without fading.

- **New setting `DAIMON_TURN__STATUS_LOG` (off by default): the "Working on it…" card shows an agent's last three messages.** With it off nothing changes: the card shows only the latest line the agent wrote, and consecutive messages are joined as before. With it on (Slack, Teams and Discord), the card keeps the last three messages the agent wrote, oldest first, one line each (cut at 120 characters), instead of replacing one line each time. An answer made of several messages keeps every one, set apart by a blank line, so a short line written just before the answer is no longer glued onto its first word. The headline, tool lines and Stop button are unchanged.

- **No other chat app in what Daimon posts, even when the model names one.** The instruction not to name another app was not enough: on Teams the model still repeated "Discord or Slack" back from a quote. Everything a model writes for a person is now scrubbed before it is posted, on all three platforms: the reply, the progress card's draft and tool lines, a reply recovered after a restart, a routine's result, the Discord thread title, and the text of `send_message`, `create_thread`, `edit_message`, `rename_thread`, `send_direct_message`, `post_wizard`, `set_display_identity` and the GitHub install card's reason. Another app's name, and its links, become "another app" ("Discord or Slack" reads as "another app", "Discord and Slack" as "other apps"); a platform's own name stays. A URL is never edited inside: one on another app's host is replaced whole, any other is kept byte for byte and only a link's label is scrubbed, so `github.com/discord/...` still works (and still shows in the URL itself). Only the capitalised "Slack" and "Teams" are replaced, so "cut some slack" and "our teams" stay, and a sentence that opens with "Slack" or "Teams" in its other sense is replaced too. Code blocks are scrubbed like the rest. Stored transcripts, logs and what the model reads are unchanged. A link is judged by its parsed host (port, userinfo, query and uppercase included), and a malformed one never blocks the reply. If the scrub itself ever fails, the reply is posted with the names removed bluntly, and `outbound_scrub.failed` is logged with the error type only. The one exception is the approval card for a tool call, which shows exactly what will run.

- **No other chat app, even in a quote.** When Daimon quotes or sums up what someone wrote, the per-turn note now has it put "another app" in place of another app's name, so the meaning stays the same.

- Nothing a person sees on one chat platform names another, or says Daimon runs on several. Refusals, privacy panels, tool errors and notices, and the tool descriptions, seeded skills and per-turn instructions the model reads, name only the platform they are on, or none; the per-turn controls now tell the model never to mention another chat app. Each chat platform's sessions run their own system prompt, the shipped one with the other platforms' formatting and file passages replaced by that platform's wording and any line that still names another platform dropped; it is sent as the session's own override, so every existing Discord, Slack and Teams conversation starts a new workspace once. The participation tool names the workspace tier in the platform's own word (organisation on Teams, server on Discord), and `request_github_access` is no longer offered on Teams, where it never worked.

- Refusals, notices, panels and tool errors use everyday words instead of internal terms: a reply or message for a turn, a conversation for a session, a model provider for a backend, a connection for an MCP server, access settings for the access policy, and "Daimon-wide default" for the deployment default. The setup panel's **Model backend** is now **Model provider**, and the privacy receipt lists what was removed in the same words as its preview. `tests/parity/test_plain_words.py` keeps those terms out of person-facing text.
- On Teams, a guest from another organisation is no longer told to use a 1:1 chat with Daimon, which Teams does not open with them. Their per-turn words, refusals, `help` and `here` cards send setup and billing to someone in the organisation that added Daimon.
- The same holds for the tools and notes a guest can reach: Microsoft 365 sign-in and file links, the Connect GitHub link, add-skill fallbacks, forms and files in a group chat, and the group-chat files note now say the action is for people in the organisation that added Daimon, not a 1:1 chat. Where the sender can't be told (no origin passed), they name no chat. Teams' system prompt tells the model to defer to the turn's files note about suggesting a 1:1 chat, so Teams sessions restart once; Discord and Slack prompts are unchanged. The origin of each turn records whether its sender is a guest (`turn_origins.sender_foreign`), and `tests/parity/test_guest_one_to_one_sweep.py` accounts for every text that names a 1:1 chat.

- **Third-party MCP tools that say they only read can stop asking for
  approval, on servers you trust.** List a server in
  `DAIMON_TOOL_SAFETY__TRUSTED_ANNOTATIONS` (e.g. `["bridge"]`) and, with tool
  safety on, Daimon reads that server's own tool annotations (once per turn,
  on the first paused call to it) and runs a tool marked `readOnlyHint: true`
  as a read, so a noun-first read such as `memory_flag_list` no longer needs a
  `DAIMON_TOOL_SAFETY__EFFECTS` pin; each such allow is logged. A tool also
  marked `destructiveHint: true`, one marked `readOnlyHint: false`, and one
  without the hint are never let through by annotations; an `effects` pin or a
  `denied` entry still wins, and when the server cannot be read the tool name
  decides as before. The list is empty by default: an unlisted server's
  annotations are never fetched and its tools are classified by name only.
- **Routines in plain words first.** The Slack and Teams "New routine" forms open with "Easiest: ask me in a channel, like \"every weekday at 9am, post the sales numbers\"." and label the cron field "Schedule (advanced: cron, like 0 9 * * 1-5)". Schedules were already shown in words.
- **Setup panel for members.** The setup button reads **⚙️ Set up …** instead of **Manage …**, and its conversation is titled "Setting up …", on Discord, Slack and Teams. A member's agent Details no longer offers **➕ Add skill** or **🧰 Use from your coding tools**, which only refused them; Add skill is also hidden on built-in agents. The member opener says they can add a key to any agent, not "keys or tools".
- **Each platform's own words in cards and notices.** Discord's routing panel says "server default", Slack "workspace default", Teams "organisation default"; environment, routing and thread-participation notes name the platform's space and admin. Text with no platform in hand says "an admin" and "here". "Fork" is now "copy" in setup text, billing reads "about N answers", busy notices read "too many chats at once", and the key button reads "Add connection token".
- **First use.** Discord's install posts one message that turns from "Getting ready" into a short welcome: what Daimon does, one thing to try, "help me set up" for setup, and the starting credit for a prepaid tenant. Slack's install page says to `/invite` Daimon, and on a first install the installer gets the same welcome as a DM. Teams welcomes teach "help me set up", and Teams refusals that send someone to an admin name the admin team when Daimon knows it. A DM to Daimon on Discord or Slack when private chats aren't available gets a one-time reply instead of silence. The Slack account-connect tip no longer appears on a first mention; the link shows when a read needs it.
- **The model uses each platform's words.** Every chat turn and routine run tells the model, in `<turn_controls>`, what this platform calls things and how to reach its commands from where the message was sent (for example `setup` in a 1:1 chat on Teams, `/agent-setup` in a server channel from a Discord DM). The workspace-setup skill and tool descriptions shown on more than one platform no longer name Discord-only terms ("Manage Server", `/agent-setup`) and point at those words instead. `set_thread_participation` and `get_thread_participation` are listed only on Discord and Teams, where they work. No live conversation moves to a new workspace.
- **Each platform's own words.** Help cards, refusals, usage-limit and routine failure notices, the here card and admin-only tool results now take their nouns and commands from one table (`daimon.core.platform_words`): a Discord server admin and `/agent-setup`, a Slack workspace admin and `/agent-setup`, a Teams admin and `setup` in a 1:1 chat. Teams text no longer names `/agent-setup`, `/billing` or a server admin, and from a Discord DM a server-only command says to use it in a server channel. Command descriptions are identical on Discord, Slack and Teams; Discord's `/privacy` description no longer includes the bot's name.
- **Teams `help` shows what works where it was sent.** A channel card lists the commands answered there and points to the 1:1 chat for `setup` and `support`, a group chat card also lists `new`, and the card no longer says to type `/`. The Teams app manifest's descriptions match the other platforms (manifest 0.6.2; re-uploading is optional).
- **Teams first use.** Shorter install welcomes, with an **Open chat** button on the team welcome; the setup card points to the chat for GitHub changes instead of a button it doesn't have; a setup conversation in the 1:1 chat opens with "This chat is about…"; the bot's name no longer carries stray spaces.

- The agent GitHub repo picker groups accounts and orgs, keeps selections across owners, and saves additions and removals together. A GitHub owner whose repos cannot be listed leaves other owners available; stale grants remain removable.

- One agent can now use repos from several GitHub accounts or orgs (up to 12); each gets its own token covering exactly the granted repos.

- The GitHub repo editor now refuses saves over 256 KiB, gives up listing your GitHub repos after 60 seconds, and keeps your ticked repos when Try again follows a GitHub error.
- A Teams channel command whose "answered in your 1:1 chat" notice Teams refuses is still answered in the 1:1 chat. Failed sends now log Teams' error code.
- Live GitHub tokens have their own index, so the scheduler no longer reads every issued token, revoked ones included, on each tick.
- Revoked GitHub token records are now deleted 7 days after they expire and unused ones 1 hour after, while live tokens and the security audit log are kept.
- `/dm` is now `/dm-me` on Slack and Discord, including `/dm-me enable` and `/dm-me disable`. Slack reserves `/dm` for its own command, so a Slack app could not offer it. Its replies read as one line for what happened and one for what to do; a DM closed because its channel was sealed now says to keep working in that channel.
- GitHub connection confirmation now checks the requester's live Discord, Slack, or Teams admin status before saving repository access.
- Channel admins are removed. Grants no longer give any rights; server and workspace admins are unchanged. Stored grant rows are kept until a later release drops them.
- Offer “Opus 5.5 fast” as a separate model choice for new Managed Agents sessions, with twice the standard token rates.

- GitHub repository access now uses the GitHub App for every agent. Migration deletes staged grants, keeps live App grants, carries a matching working repo, and retires saved GitHub keys. `daimon github retirement-preview` reports staged rows and saved-key coverage before migration. Private skill imports use an active App grant; missing access posts Connect GitHub. Operators can preview and delete retired Managed Agents vault credentials with `daimon github cleanup-legacy-vault` (`--apply` deletes them).

- **Teams admins are the owners of one team.** Set
  `DAIMON_TEAMS__ADMIN_TEAM_ID` to the team's Entra group ID and add the app
  to that team; its owners, read live and cached for a minute, are Daimon's
  admins, so adding one needs no redeploy, and a removed one loses admin
  within about a minute, routines and tools included. A failed owner read falls back to the last
  good one for up to an hour and demotes no one; once no owner list has been
  read within an hour, nobody is an admin until the next good read.
  Support requests and channel budget notices for the admins reach these
  owners. Guest owners no longer count for channel-admin team grants
  (`role_ids`) either.

- **Agents no longer narrate file delivery.** The file guidance tells the
  agent to say what it made and name each file, and to explain where files
  go only when asked, so answers stop saying files "should be" in the Files
  tab or asking to be told when one is missing. Teams file links below an
  answer are a plain list, without the "Saved to this channel's files"
  heading.

- **The Teams feedback card comes after the files.** After a turn that
  wrote output files, the 👍/👎 and Ask a person card waits for their
  links and previews, so it stays the last message. Delivery starts
  sooner: the listing wait counts from the turn's last tool call, and
  document thumbnails are requested as each file uploads.

- **Teams output previews show inside the answer.** Image previews and
  document first pages go into the answer's last message as markdown images
  after its text and file links, instead of following it as separate
  messages; in a 1:1 chat they sit above the consent cards. Teams refuses an
  edit with text and an image attachment, so they ride in the text, and the
  message is sent as plain markdown, whose images Graph reads back. An
  answer with math, a closing notice card or an edit Teams refuses still
  gets them in a message of their own. Channels without file access and
  group chats no longer show image previews: nothing is saved there.

- **Teams channel files need one permission and no card.** Grant the Graph
  application permission `Files.ReadWrite.All` with admin consent and files
  work in every channel, private and shared ones included, within 10
  minutes; the Enable files card, the `enable_channel_files` tool and the
  sign-in callback are gone. When upgrading, `Sites.Selected` and the app's
  redirect URI can be removed, and a migration drops `teams_channel_sites`.
  The permission is tenant-wide, so Daimon opens a shared file and saves its
  own only inside the channel's Files folder. Uploads take one request.

- **The Teams 1:1 file offer reads "Save <name> to your OneDrive?"**, naming
  where the file goes, instead of a bare file name, for files sent with
  `send_message` too. Accepting still uploads the file to the person's
  OneDrive and declining still drops the offer. The card's buttons are Teams'
  own and unchanged, and channel uploads are unaffected. No setting or
  manifest change.
- Walls say who can fix them and the one next step: `/here` when Daimon can't see or answer in a channel, GitHub refusals, MCP connection refusals and expired approvals. Each slash command has one description on Discord and Slack, and `/help` lists `/here` first. The setup skill and docs no longer promise members MCP connections on shared agents or describe GitHub access levels removed in #1005.
- **Thread titles and the thread-participation classifier now run on Claude Haiku 5.5** instead of Claude Haiku 4.5. Haiku 5.5 costs less per token but counts the same text as about 30% more tokens, so these calls' usage rows shift. Haiku 5.5 thinks by default and thinking counts toward the output cap, so naming now turns thinking off. The classifier runs at effort low with a 1,024-token cap, since its old 100-token cap cut off Haiku 5.5's verdict, which then counted as silence. A refused naming call falls back to the title taken from the message, and a refused classifier call stays silent, as other failures already did; both are still metered. `DAIMON_THREAD_PARTICIPATION__CLASSIFIER_MODEL` still picks the classifier's model, which must support the effort parameter (Claude Haiku 4.5 does not).
- Connect GitHub pages now show clear next steps, keep repo additions and removals pending until Save, and hide repo names the current GitHub account cannot access.
- **When an agent can't read a private repo, it now posts the Connect GitHub button straight away instead of asking first.** Asked to "connect GitHub", it posts the button rather than granting a repo silently, and it no longer says connecting a repo would replace its working repo (connecting never does). The old token form now refuses agents that use the GitHub App, and clone and CLI errors point to Connect GitHub instead of that form. The GitHub App install link also works on deployments that only set `DAIMON_GITHUB_APP__APP_SLUG`; before, the agent said no install link was configured.
- GitHub App sessions now use one `GH_TOKEN` scoped to the agent's live repos in one installation, always with write access. Connect GitHub grants write access without a level selector, and existing read grants are upgraded. A grant version change rotates the live session token before the old token is retired; removed grants and installations still revoke access promptly.
- **A channel admin can now see how their channel's own agent is set up.** The agent tells a requester marked as a channel admin its setup details (repo names, the GitHub account behind its credentials, key names, environment details, instructions), but only about agents that answer in that channel; other agents and the workspace default still get the short "An admin can see setup details" line. `list_agents` and `get_agent` return the attached MCP servers, creation date and sync warnings (and, from `get_agent`, the system prompt) to a channel admin for an agent they could edit from chat, meaning one limited to their channels. Everyone else who is not a workspace admin still gets those fields as null.
- **Files an agent makes can now be up to 200 MB on Slack, and up to your Discord server's own upload limit.** Before, anything over 20 MB was kept and announced instead of attached, even where the platform takes more. On Discord the limit is the server's (10 MB, 50 MB or 100 MB depending on its boost level), and 10 MB where there is no server; it never goes above 200 MB. Teams stays at 20 MB. A file over the limit is still kept in the agent's workspace, and the notice now quotes the limit that applied. Slack uploads get a time limit that grows with the file size instead of 30 seconds. Files the agent sends with `send_message` stay capped at 20 MiB each.
- **On Discord and Slack, the `Ref` line on a stop or failure notice is now shown only to admins.** It is the last six characters of the request id. Before, everyone saw it; now it appears only when the person whose turn it is holds the admin role there, and it is left out whenever that is not known. Support can still find the failure from the logs, which keep the full request id. Teams is unchanged.

- **People with read or write access to a repo can now give an agent access to it, not only repo admins.** The Connect GitHub page lists every repo the signed-in GitHub account can read, triage, write, maintain or administer, and says how many it hides because the account has no permission on them. What a person can grant is capped by their own GitHub permission: read and triage give Read only, write, maintain and admin give Read and write, and never more than the GitHub App installation holds (an installation without write permissions grants Read only). A read-level person can only grant Read only: a Read and write choice is granted read for that repo, never write. A repo an agent needs at write is added at read when the person cannot write, and it stays listed as still needed. Removing a repo or changing the working repo needs at least read on it. The Add repos dialog now has bulk selection: a filter box, **Select all N shown**, **Clear**, an **Everything in <owner>** button per GitHub account, and one Read only or Read and write choice for the whole selection (a repo your permission or the installation caps at read is granted read, and tagged "Read only on GitHub"). The button reads **Grant N repos** with the live count. The limit of 10 repos per add is gone, since a grant only scopes the agent's GitHub token. Each connected repo now records the confirming person's GitHub permission (`tenant_github_repos.authorized_permission`), and a write ceiling an admin set earlier is not credited to a later read-only confirmation.
- **Faster startup and fewer upstream calls per turn.** The image now ships compiled bytecode, so each service starts in about half the time; its dependency layer is about 110 MB larger, and it no longer carries the package test trees. Vault, agent, skill and session-event listings page 100 at a time, and the lookups that repeat on every turn or tool call (an agent's MCP vault, its MA agent id, the channel skills listing) are cached per process. Slack calls reuse one connection pool per process, Discord tool calls skip a redundant roles request, and image and chart downloads run concurrently. Migration `0093_spend_gate_indexes` adds indexes for the per-turn cap and balance reads; it builds them concurrently, so turns keep writing meanwhile.
- QA expected CLI refusals reject transport/crash signatures before matching refusal text, reject empty-match patterns, and refuse wrapped or structured tenant-credit commands before mutation.
- QA footer and ledger assertions compare scoped per-request token usage, independent rates and deployment markup with exact ledger debit identities.

- QA supports frozen Discord answer/thread counts, nested alternatives, component/card observations, running card edits and chunk delivery gaps, with scoped guild and expected CLI-exit parameters.
- Privacy delete previews, channel setup panels, network confirmations, and DM controls use the approved plain words across Discord, Slack, and Teams.
- Connect GitHub shows an agent's complete repo list and working repo. Authorized users can add or remove repos and set or clear the working repo in one save. Live permission checks and stale-page detection protect changes made in chat.

- Staging QA waits for a quiet deployment window before each scenario and records the CI evidence without changing staging deployments. A workflow rerun keeps its run id but is re-read by attempt, so its new deploy must also settle.
- Staging QA records deployment images and restart evidence, keeps deploy-interrupted runs silent and PENDING, and retries once after workers settle within the pass budget.
- GitHub setup sections on Discord, Slack, and Teams show repos and the working repo without grant-editing controls. Ask the agent in chat to change repos; Connect GitHub remains available.

- QA restores normal alertable verdicts on the same worker image while preserving missing event-log checks; same-image restarts fail consistently regardless of probe timing.
- Plain words and spaced text for feedback, help, billing, memory, routines, and wizard panels across Discord, Slack, and Teams.
- Add bounded unauthenticated HTTP checks without Discord setup or model spend.
- QA CLI readback passes validated argv, bounds UTF-8 bytes, and keeps unavailable hooks or malformed resolved expectations PENDING without stopping other checks.

- GitHub cards use plain words: a pending request says "An admin has been asked to allow GitHub access." and "Your request continues when access is ready.", a request for an unconnected repo no longer repeats "(not connected yet)", the "GitHub on Daimon" footer is gone, and a built-in agent's refusal says "Ask me to make a copy and add them there." instead of mentioning forks.
- Stop and admission notices now use two short lines on Discord, Slack and Teams. DMs tell people to send a message; channel notices tell them to mention Daimon. Work and short references use small text. Channel rules have plain names.
- Live QA records terminal stability separately from a watch's end time. A timed-out observation keeps unproven upper bounds, final fences and footer placement PENDING while the watch remains FAIL.
- The privacy policy now describes account deletion across linked servers and workspaces, Anthropic transcript deletion failures, and records and files that remain afterward.
- Setup, error, file and limit notices on Discord, Slack and Teams use plain words, one line for what happened and one for what to do, with a blank line between. A channel with no agent or environment says "Daimon isn't set up in this channel yet." and one whose agent or environment was removed says "This channel's setup is out of date."; both point at `/agent-setup` (`setup` on Teams). Discord and Slack errors name the cause (the AI service busy, unreachable or refusing the request, the usage limit, a missing Slack permission, an unreadable Slack token, Slack or Discord rejecting the call, or "Something went wrong on our side.") and end with a small `Ref` line, the last six characters of the request id; the full id and the exception are logged as `error.rendered`. No exception text reaches the chat; errors whose text is written for people (a deleted setup conversation, a repo nothing can clone, opening Slack setup outside a channel) keep their words, with the same `Ref` line. Slack's file-upload and `/dm` notices are shorter, the `/dm` setup steps are logged and documented in the Slack guide, Discord's usage-limit card no longer mentions the tenant, and a channel budget DM reads "#team-020 has used its $5.00 monthly budget." with how to resume.
- The GitHub screens on Discord and Slack are agent-first. The GitHub home lists each agent with its repo count and an **Add repos** button. An agent's section reads "<Agent>'s repos", says who can use them ("Anyone who talks to <Agent> can ask it to read them."), and has **Add repos**, **Remove from <Agent>** and **Details** (who added each repo, when, and its access). Channel admins who manage an agent get **Add repos** too; anyone else is told to ask whoever manages the agent. The connect and "Manage connected repos" buttons for the whole server are gone from the home.
- The GitHub repo picker for an agent is titled "Add repos to <Agent>", shows repos the agent already has as ticked and "Already added" (a repo it still needs, such as a working repo it has read only, stays ticked and changeable as "Needs write" and is added with write), defaults to Read only, and its button counts only new repos ("Add 2 repos"). The done page says "Added 2 repos to <Agent>." It says the agent no longer uses its old GitHub token only once that switch has finished; while the agent still needs its working or skill repo, it names the repo to add, also when the page is opened again.
- Setting an agent as a default in another place adds "<Agent>'s repos come too." to the confirmation when the agent has repos.
- Live QA now shares one approved cheap-model map across the judge and pre/post-turn guards, with a temporary staging-only Haiku 4.5 exception and HTTP-503-only Gemini fallback helper.
- QA catalog validation now refuses unknown kinds in hourly canaries while preserving isolated PENDING entries for other tiers.

- The scheduler's headless usage sweep is off by default (`DAIMON_SCHEDULER__USAGE_SWEEP_ENABLED`). It listed every session in the shared Managed Agents workspace and drained its request rate limit, stalling admission. While it's off, headless MCP turns aren't metered and usage a live adapter missed isn't recovered; turning it back on backfills sessions still present in the workspace (and restores the full scan until the scoped sweep ships). `--once` honours the same switch.
- Question-and-answer and checking tools no longer ask for approval as if they were writes. A tool whose name asks a question or checks an answer, or names a briefing (DeepWiki's `ask_wiki_question` and the EAP server's `eap_briefing` and `eap_verify_answer`), now runs without a card. A bare `ask`, such as another Daimon's agent-to-agent `ask`, still asks for approval. A server that marks a tool `readOnlyHint: false` is believed over a read-sounding name.
- Teams 👍/👎 are now emoji buttons beside Ask a person instead of Teams' built-in thumbs. Older answers keep the built-in thumbs, which still work.
- Teams answers end like Discord and Slack ones: a card under the answer shows the summary line (`Ada  12s  $0.042 used  $41.20 left`) with 👍/👎 and, when support is set up, Ask a person, all in one place. A tool-only or failed turn's card shows the summary line too.
- Teams support and 👎 feedback posts are easier to read: each line is its own paragraph, since Teams ran them together, and the long message URL is a short "Open the message" link.
- When Daimon can't post as the agent on Discord, the agent's name now sits above the answer as a small subtext line instead of a bold header. Admins whose server hasn't given Daimon Manage Webhooks see a one-line note with a re-authorize link in `/agent-setup`. After a webhook permission error, Daimon retries agent names after 60 seconds instead of 10 minutes, and right away when the bot is granted Manage Webhooks.
- Discord and Slack Connect GitHub buttons now show a link emoji.
- Plainer words, the same on Discord, Slack and Teams, with a blank line between lines: key and connection cards (``🔑 team-020-analyst needs `TOGGL_TOKEN` ``, footer `Only @maria can fill this in. Expires 15:42.`, with Discord showing the clock time instead of "in 10 minutes"); the rejected-token and replace-refused replies, which suggest signing in only when the server supports browser sign-in; the starting-agent and shared-agent refusals; the "Use from your coding tools" reply and its refusal; Ask a person's note hint and support-post marker; the channel skills and channel admins panels; and routine results posted or sent by DM, which name simple schedules in words ("daily routine at 09:00 UTC") and keep anything else as cron text.
- Discord's Ask a person now does what Slack and Teams do for an answer in a channel only its own turns read: the form asks you to leave out anything that must stay there, and the support post says "Reply in the original channel." On Teams, a token the server rejects is now answered in the still-open form with the same words as Discord and Slack, instead of only on the card.
- The privacy panel is now two lines on who stores what, under "🔒 Your data", with the detail behind Policy. Delete's confirmation now says it removes Daimon's records about you and tries to delete your conversations stored at Anthropic, while shared agents and their memory stay. It no longer claims that session transcripts stay in Managed Agents, which was untrue: deletion has always tried to remove them.
- GitHub connection links now appear in a branded card with the agent's face and readable name when agent identity is enabled. Generated names are shown as "this agent". The compact card is the default; a richer card is available with a one-line switch.
- With agent identity on, a routine result that Daimon posts for an agent on Slack or Discord now shows the agent's name and face. On Discord it reads `Result from the Friday routine at 17:00 Europe/London:` instead of naming the agent in the text; Slack keeps the agent's name in the text. The answer to a submitted Discord form also posts as the agent, like a mention reply.

- Agent setup on Slack and Discord no longer takes custom picture uploads. Each agent shows its generated face, and **Use default** turns a picture uploaded earlier back into that face. The `/agent-setup` picture options are gone, and an upload form opened earlier says custom pictures are turned off.
- Teams Ask a person posts to its own `DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID`, as Slack has its own. It no longer shares `DAIMON_SUPPORT__ESCALATION_CHANNEL_ID` with Discord, so one deployment can run Ask a human on both.
- Tool approval cards on Discord, Slack and Teams now name the action and consequence, show plain labelled inputs in Details, and collapse after a decision. Each blocked call gets its own card and confirmation event.
- If a turn stops after an approval click but before its confirmation is sent, the answered card now shows Stopped.

- `/billing` (`billing` on Teams) names its top spenders, never as `User 1234` and without pinging anyone. Discord fetches the member when the cache lacks them; Slack asks `users.info` and shows the name as plain text; Teams reads the rosters of the teams the bot is installed in. Someone who has left, or whose lookup is slow, shows the name daimon last saw for them: it now remembers the names Discord, Slack and Teams send with messages, clicks and lookups (`platform_user_names`, removed by a privacy deletion). Discord also asks for a departed person's account name. Someone never seen at all is a mention the Discord or Slack client names, or `Name unavailable` on Teams. Teams names channel budgets by channel name instead of the `19:…` id.
- `/billing` drops its `·` separators: the admin subtitle is two lines, expiry dates read `$20.00 on Oct 12`, and top-up amounts show `about 100 turns` under the amount.
- `/billing` is a short panel in the same words on Discord, Slack and Teams: the month and what was spent by how many people, the credit left as one large total (`$62.40` `total credit left`, or `No credit left` and `$3.10 spent beyond it`) with how much of it expires, the invoking channel's budget under **This channel** (`$1.20 of $5.00 used this month`), a member's own use (`$11.50 of your $25.00 this month`), and for admins the **Top spenders** and **Channel budgets**, each in its own section. Actions: **Add credit**, **Redeem code**, **Expiry dates** (each timed credit's end date) and, for admins on Discord and now Slack, **Look up a person**. Discord's accent turns red with no credit left or over your cap, and amber while timed credit expires within a week.
- `daimon github connect-link` requires `--requester` with a tenant admin platform user ID; the invitation is minted on that admin’s behalf.
- **Channel and agent rules replace protected, sealed, isolated (confidential) and pinned.** Other entries in this section that use those words describe these rules: a protected channel or category is `writers: none`, a sealed one `readers: inside`, an isolated or confidential one `readers: own` and `writers: own`, and a pin is an agent rule. `set_channel_protection`, `set_channel_isolation`, `daimon channels protect`, `daimon channels isolate` and the rule flags of `daimon tenants access-policy set` are replaced by the rule tools and commands; `archive_isolation_copy` is now `archive_channel_copy`. A stored policy written before reads as the same rules and is saved in the new shape on its next change, so no migration runs. A build from before this change can't read a policy saved by it and refuses rather than falls open. From a channel kept to its own agents, or by an agent with a rule, publishing a report, notebook or attachment now asks the requester to approve it instead of being refused.
- **Turn outcomes tell protection, pin and isolation refusals apart.** A turn
  refused because its channel is protected, its agent is pinned to other
  channels, or the channel is isolated and the agent is not one of its own was
  recorded as `admission_denied`, the same as the invoker allowlist, so
  `daimon usage turns` could not say which gate stopped it. These now record
  `admission_channel_protected`, `admission_agent_pinned_elsewhere` and
  `admission_channel_isolated`, from chat and MCP turns alike. Migration
  `0045_admission_refusal_reasons` widens the `turn_outcomes.reason` check;
  its downgrade folds them back into `admission_denied`. Earlier rows keep
  `admission_denied`.
- **Channel admins change only their channels' own agents.** A channel admin
  could configure, rebind and edit any agent answering in their channels,
  including one a member bound there. They now may only when the agent is
  theirs: made by a channel admin from one of their channels (`create_agent`
  with the turn's `origin_context_id`, or a setup panel's New agent form),
  pinned by a server admin inside their channels, or set as one of their
  channels' default by a server admin; the last does not let them bind it
  elsewhere. Existing agents have no recorded creation channel, and defaults
  a server admin set before this are recognised by the setter's stored role,
  so a server admin re-sets any other default a channel admin should manage.
  That role is the one at upgrade, so a setter promoted since counts as one.
- **Channel admins hand threads only to agents of theirs.** `hand_off_task`
  and the Hand over button let a channel admin bring any agent into an
  unsealed thread of their channel, lending it another channel's own agent's
  keys, connectors and memory. They now may only for an agent they could make
  that channel's default (a built-in agent, the workspace default, or one of
  their own that answers only in their channels); anything else needs a server
  admin. Both a handoff and a channel default set by a channel admin are now
  refused when they would cost another channel's admin their hold on the
  agent. Admin and refused Hand over clicks now record a `panel:handoff`
  audit row, and a Slack channel admin granted through a user group can click.
- **Slack user groups and Teams team owners can be channel admins.** A
  channel admin grant's `role_ids` now also takes Slack user group ids and a
  Teams team's Entra group id (whose owners it admits), from
  `set_channel_admins`, `daimon channels admins set --role` and, on Slack, a
  user group select in the channel admins form; listings show the groups
  before the members. Slack and Teams look up only the groups some grant
  names, each cached for a minute, and outside a chat turn a stored group counts
  only while a fresh lookup still lists the person. A failed lookup grants
  nothing. Any Slack member can edit user groups by default, so limit that to
  admins before naming one. Slack needs the new `usergroups:read` bot scope and
  Teams the `TeamMember.Read.Group` consent, so existing installs reinstall or
  upload the updated app package.
- **Ask a human reaches a channel's own admins.** On Discord and Slack, a
  request from a channel with channel admins is sent to them by DM, then to the
  server admins if none could be reached, and to the escalation channel only
  when no DM landed. It spends the same one credit, and channels without admins
  are unchanged.
- **Per-channel skills.** Server admins and operator tokens can add skills to
  whatever agent answers in one channel, there only: a library skill, or one
  uploaded to that agent. Use `add_channel_skill`, `remove_channel_skill` and
  `list_channel_skills`, `daimon channels skills`, or Channel skills on Who
  answers where in the Discord and Slack setup panels. A channel's own admins
  can't change them. The version added is kept; add the skill again for a
  newer one.
- **Forks keep their own uploaded skills.** `fork_agent`, `daimon agents fork`
  and channel isolation used to leave every skill scoped to the source agent
  off the copy, so isolating a channel lost the skills uploaded to its agent.
  The source's own skills are now downloaded and uploaded again under the
  fork's name, as independent skills with their own ids and upload rows. A
  skill scoped to another agent is still left off, and one that fails to copy
  is named while the fork still succeeds.
- **One wording for a refused turn.** Discord, Slack, Teams and `/dm` now
  word each admission refusal (credit, usage cap, channel budget, invoker list,
  agent pin, channel isolation) the same way, each in its own nouns ("a server
  admin", "a workspace admin"). A DM turn refused for anything but the channel
  budget no longer shows the raw reason code, and `/dm` on Discord names the
  server rather than a workspace. The usage cap is per person and set by the
  operator, so the reply now says "You've reached your monthly usage cap. An
  operator can raise it." instead of naming the server and `/billing`; the
  refusal from the MCP turn tools and the cap's termination notice say so too.
- **Channel budget notices.** When a channel's budget is used up, its channel
  admins (or the server admins, when it has none) get one DM per budget
  window on Discord, Slack and Teams, sent without delaying the refusal.
  Setting or raising the budget re-arms it, and so does a notice no admin
  received; `DAIMON_BUDGET_NOTICES` turns it off per tenant.
- **Audited CLI budget changes.** `daimon channels budget set` and `clear`
  now record the change in `security_audit_events`.
- **Timed credit and channel flags in listings.** `get_tenant_summary` and
  `daimon channels list --json` list the tenant's live timed promo credit
  (`timed_credit`: what is left of each grant and when it ends).
  `daimon channels list --json` also gives each channel's `sealed` and
  `protected` flags, which an operator can already read with
  `daimon tenants access-policy get`; the MCP tool leaves both keys out.
- **The channel budget on the status card.** In a channel with an active
  budget, the Discord and Slack status card's summary shows what the budget
  has left instead of the tenant balance (a DM moved with `/dm` shows its
  source channel's).
- **Channel budgets in `/billing`.** An admin's `/billing` panel on Discord,
  Slack and Teams lists the channel budgets, most used first, with each
  one's spend and share used. Members see only the invoking channel's
  budget, as before.
- Successful turn admissions now log elapsed time for identity and channel-admin reads, policy checks, config and agent resolution, and each billing gate. Discord also logs when the durable first-card intent commits, with its write duration, so slow first cards can be attributed before session preparation.
- Discord opening mentions now get a parent-channel notice when thread naming or creation takes more than three seconds. The notice becomes a thread link or retry guidance; `DAIMON_DISCORD__THREAD_OPEN_NOTICE_AFTER_S=0` posts it immediately. Discord `runtime.health` now counts 429 retries by route and records the longest retry wait in each window.
- Postgres pool size, overflow and checkout timeout are configurable per process through `DAIMON_DATABASE__POOL_SIZE`, `DAIMON_DATABASE__MAX_OVERFLOW` and `DAIMON_DATABASE__POOL_TIMEOUT`.

- `DAIMON_SUPPORT__CREDITS_PER_USER` now defaults to 20 human-support requests per person (was 3); 0 still turns escalation off. This applies to Discord, Teams and Slack. Two requests from one person can no longer both spend the last credit on any of them: the count and the write now run under a per-person lock. Teams' support replies now use the same wording as Discord and Slack.
- **A tidier status card while a turn runs.** Discord shows one embed instead
  of two and Slack one matching card: a bold Thinking or Working headline with
  the elapsed time, up to six recent tool calls in a code block, and the latest
  draft quoted underneath. Built-in tools read as plain verbs ("Reading a
  file", then "Read a file"), MCP and custom tools get readable names, finished
  calls are ticked, failed ones marked, and older calls fold into "+N earlier".
  The list now includes MCP and custom tool calls and still never shows tool
  arguments. The finished-turn summary and the error card are unchanged.
- **Teams shows the same status card, and answers without a usage line.** The
  running card on Teams now matches Discord and Slack: a bold Thinking or
  Working headline with the elapsed time, up to six recent tool calls with
  readable names and done or failed marks (MCP and custom tools included),
  and the latest draft underneath, with Cancel kept. Answers, the close of a
  turn that only ran tools ("✅ Done.") and failure notices no longer end with
  the agent, time, token, cost and balance line. The feedback buttons stay on
  the last part of the answer.
- **Teams agents know how file delivery works there.** The guidance block
  every agent gets now has a Teams paragraph: in a 1:1 chat, saving a file
  under `/mnt/session/outputs` is the delivery path, with Slack's write rules,
  and the person accepts it from a download card; in a channel no file can be
  attached, so the agent says so once in its reply, pastes short text inline
  or points to a 1:1 chat. Channel threads no longer get a separate note per
  file. `send_message`'s refusal of files on Teams now says where files go
  instead of "not available yet". Agents pick it up at their next reconcile
  or edit.

- A handoff or private-input continuation whose process dies mid-dispatch is
  no longer stuck in `claimed`: it is retried if its turn had not started, and
  settled `skipped/interrupted` if it had. When the session is busy, the same
  row goes back to pending instead of being queued again under a new key. It
  still waits for the next turn in the thread, and busy retries stay
  unlimited. A continuation whose process dies five times before its turn
  starts is settled `skipped/attempts_exhausted`, and nothing is posted to the
  thread.

- `get_agent` now returns the agent's `system` prompt to an admin caller on
  an agent chat tools may edit, so a setup flow can save the prompt before
  replacing it and verify the change afterwards. Non-admin callers, and every
  caller on Daimon or another defaults-managed agent, get `system: null`.

- The seeded `pymc-artifact-style` skill now follows the live pymc-labs.com
  palette and type (re-derived from the site CSS on 2026-09-28): it adds the
  site's readable text accents (teal, indigo, dark orange) and navy-header,
  uses Inter 600/500 headings with the site's tracking, bundles Inter Medium,
  and drops the legacy Archivo and Fira Mono fonts, cover art, old logos and
  the non-website chart variants.
- GitHub App installation-token mint rate limits during bound skill resync now
  defer the durable queue job using GitHub's retry deadline; permission 403s
  remain permanent. See `docs/github-push-resync.md` for the covered request
  paths and remaining scope.
- **Sonnet 5.5 is the default model.** It can be selected for an agent and is
  metered at its published rates, and the seeded `daimon` and `dev_agent`
  agents and every new agent now start on it instead of Sonnet 5, at the same
  per-token price. Asking an agent tool for "Sonnet" or "Opus" now means
  Sonnet 5.5 or Opus 5.5. Opus 5.5 and the older models remain selectable per
  agent; fork a seeded agent to keep it on another model. Sonnet 5.5 returns longer notes between tool calls as thinking, so the in-progress
  card can show less draft text than it did on Sonnet 5.
- Opus 5.5 can be selected for an agent and is metered at its published rates.
- The documentation site carries daimon's own look: the readme sticker as
  logo and favicon, and a palette taken from it.
- Defaults reconciliation serializes writes and sweeps per tenant so concurrent
  callers cannot create duplicate seeded resources or archive an ID another
  caller has already resolved.

- Synchronize the Preview lane with main and atomically repair legacy Preview migration stamps while preserving existing data.

### Fixed

- Concurrent routines for the same owner reuse their platform identity when enrollment is still committing, instead of failing with a duplicate-principal error.

- Bind signed session-origin authority only while a turn owns its conversation lease, so queued or stopped attended turns cannot replace an unattended holder’s grant or attendance.

- Close recruitment boundaries for unresolved Discord channel forms, lead intake
  and recovery, saved channel routines, persistent wizard controls and channel
  metadata. Require explicit source authority at the core turn driver and gate
  controls before SDK dispatch. Record complete bulk-delete events atomically;
  audit runtime Discord readers with a discovery-based regression registry.
- Recruitment tracks edits and deletions after raw-data retention, fences source events
  before enrichment, and preserves backend uncertainty independently of later errors.
  Empty recruitment settings require no privacy database or HTTP lookups.

- Discord MCP posts from self-hosted agents use their local name and avatar when agent identity is enabled, without an Anthropic lookup.

- Scheduled routines refuse thread destinations with an unknown parent channel and check retained thread backends before resolving Anthropic agents.

- Preserve each thread's retained backend when checking organic participation, handovers, timers and provider access after a channel backend change. Valid Anthropic timers keep firing after a channel switches to self-hosted; each queued wake resolves its own provider context, and provider guard denials defer work for retry. Discord, Slack and Teams `here` commands use local self-hosted agent records.

- Self-hosted MCP timer lists, current-place cards and tenant summaries resolve agent isolation from live tenant-owned records without Anthropic requests. Hidden-channel names and timer notes remain private.

- Self-hosted channels skip Anthropic thread naming, organic participation, cosmetic responder lookups and session-file sweeps. Their MCP credentials block Anthropic-specific operations while keeping message and file posting available under agent rules. Timers and task continuations resolve local targets, and unavailable follow-up runtimes or handovers refuse without provider calls. Unsupported scheduled routines stop before provider resolution.
- Keep stricter tool approval permissions when replay repeats a call, and recover partially completed selfhosted approval batches after a host restart.
- Restore selfhosted workspace, history and usage inventory from Azure Blob checkpoints after an ephemeral sandbox is destroyed; checkpoint terminal turns and cancellation before acknowledging completion.
- Restore selfhosted workspace, history and usage inventory from Azure Blob checkpoints after an ephemeral sandbox is destroyed; checkpoint terminal turns and cancellation, report degraded durability without blocking sessions, and recover above the client cursor after sandbox loss.

- Join harness tool workers before completing cancellation or accepting another turn, and retain reasoning alongside tool calls during context-overflow recovery.

- Refuse tool-readable files on external harness mounts regardless of filename, and preserve usage while reporting all non-completed model response statuses as errors.

- Restrict harness credentials to API keys or header-protected managed identity, refuse file-backed credentials, and preserve usage while marking incomplete model responses as errored.

- Include async managed-identity transport in the harness image and finish subprocess cleanup when cancellation arrives during launch.

- Preserve SDK header masking and redact harness/model credentials in debug logs; complete turn finalization before acknowledging cancellation.
- Preserve approval permissions across selfhosted reconnects and attach approved tool results to their original tool blocks. Replay and Stop can read past 5,000 journal records within one turn. Unversioned seeded skills are supported; custom uploads and channel-library additions have no production selfhosted catalog writer. The internal retained-bundle reader requires bytes already stored under the agent's tenant; Anthropic library uploads do not populate that catalog.

- Bound selfhosted Entra audiences to operator-approved origins and tenants. Fixed approval handling through the turn runner, preserved explicit agent confirmation and stored MCP rules, and resumed session history and usage replay from durable cursors.

- Bind self-hosted harness bearer tokens to operator-approved origins and tenants, preserve MCP approval identity, and enforce host tool-safety policy. Reuse Entra credentials and HTTP connections, cancel abandoned harness work, and reconcile late usage without a lifetime-session usage bound.
- Self-hosted channels skip Anthropic thread naming, organic participation, cosmetic responder lookups and session-file sweeps. Their MCP credentials block Anthropic-specific operations, and unsupported scheduled routines stop before provider resolution.
- Self-hosted channels skip Anthropic thread naming, organic participation, cosmetic responder lookups and session-file sweeps. Their MCP credentials block Anthropic-specific operations while keeping message and file posting available under agent rules. Timers and task continuations resolve local targets, and unavailable follow-up runtimes or handovers refuse without provider calls. Unsupported scheduled routines stop before provider resolution.

- Verify self-hosted skill pins using their declared content or ZIP representation, preserve held-version mount names, and load pinned channel bundles for other channel members. Explicit custom skills may use built-in names. Wildcard MCP credentials require single-tenant opt-in; private HTTP MCP destinations require operator exceptions.
- Refuse shared writable scratch in multi-session harnesses, kill detached tool descendants at completion, and reap orphan processes with tini.

- Bind self-hosted MCP credentials to operator-declared HTTPS origins and tenants, cap per-turn tokens at one hour, and recognize built-in skills without source metadata.
- Bind the self-hosted harness to one session by default; require verified process and temporary-file isolation for multi-session service.

- Require separate service/tool identities in the self-hosted harness, refuse unsigned gateway authentication, isolate file operations and redact SDK failure logs.
- Verify the self-hosted driver and harness together over HTTP, including approval execution, cancellation, replay and narrowed tool permissions.

- Watch channel/category protection uses the category from the fresh Discord REST parent and current DB rules before admission, execution and output. A stale gateway category cannot authorize a watch in a protected category; unverifiable rules or channel metadata refuse output and pause the watch.

- Discord watches compute access entirely from fresh REST guild ownership, roles, member, parent, thread and membership proofs, checking parent visibility, read history and private-thread membership (or Manage Threads) before running and before every output, including the initial thinking card. Locked threads require Manage Threads. Revoked or unverifiable access pauses the watch with a plain reason and never falls back to the parent channel. Slack and Teams watch enrollment remains unsupported.

- Unattended turns (agent chat `start_turn`/`ask`, routines, watches and wakes)
  run again on Managed Agents sessions that carry the provider's `budget` and
  `execution_identity` fields; every one was refused before the model call. A
  refusal now names the field it could not review (for example
  `unreviewed provider field session.budget`) instead of claiming the agent
  delegates to other agents.

- Preserve recorded approval cards for existing backend switches and other
  non-watch actions when the provider omits session overrides after watch gates
  are introduced. Watch enrollment still requires its own verified approval.

- Protect legacy destination-free routine previews and diagnostics from non-owner
  readers, including administrators, across MCP and chat panels. Clear retained
  legacy private output once during upgrade; operator backfill output stays redacted.

- Lead approval Details loads authorized live evidence on each click and shows
  removed records as no longer available. Cards retain no cached evidence.
  Batch mailbox readback requires the common lead read check, and hidden leads
  no longer consume list pages. Automatic resume batches skip old configuration
  revisions, old destinations and unreadable records before filling their window.

- Access policies that keep the default admin bypass remain readable by older deployments during rolling upgrades.

- Discord private conversations retain operational and billing notices while access denials stay silent. Discovery failures after membership and invoker checks show a generic notice without server or agent names.

- Continuing a routine session through the CLI, MCP or a checkpoint retains its conversation channel rules; its private transcript stamp no longer makes it execute as a DM.

- Stopping or cancelling an unattended decision, or reaching its turn ceiling, leaves pending calls paused and interrupts the session without sending confirmations, including on runtimes without a confirmation gate. Cancellation during decision-task or card cleanup also completes bounded interrupt cleanup before propagating. Normal decisions on those runtimes still refuse unattended confirmations.

- Handoffs created by unattended wakes retain unattended tool safety on Discord, Slack and Teams; queueing a handoff no longer grants human approval surfaces.
- **Picking repos from a 13th GitHub account no longer ends on an error page that drops every tick.** An agent can use repos from up to 12 accounts or orgs; that limit is unchanged. The repo page now says so before you save ("These changes use 13. Remove the repos from 1 account to save."), keeps what you picked and turns Save back on once you remove one. If the server still refuses, its message no longer claims the agent "already uses" 12 accounts when the new picks are what cross the limit.

- **GitHub's "Open GitHub setup" no longer ends on "This link has expired."** After changing which repos the Daimon app can use, GitHub sometimes sends people back without the link's state, and the page refused them. The browser now remembers the state for the same 15 minutes as the rest of the connection, so they land back on the repo picker. In the picker, reopening Add repos after choosing an account shows the account list under the right title again, and an account with no repos to add says so instead of showing an empty list.

- Adding a skill to an agent, or changing its skills, no longer unpins the skills it already has. Every path that keeps an agent's other skills (the add-skill chat tool and panels, `update_agent`, `daimon skills add` and `daimon defaults apply`) dropped each kept skill's version, which the provider reads as latest, so a skill pinned to one version moved to whatever was uploaded last. Kept skills now stay on their pinned version; unpinned ones stay on latest.

- On Teams, the approval card for a change to your Microsoft 365 files or notes (`microsoft_write`, shown when tool safety is on) now says what the call does: "Change an Excel workbook in your Microsoft 365?", "Upload or replace a file…", "Add a OneNote page…", "Change a OneNote page…". It names the change the way the tool itself classifies the path, so a folder called "OneNote" can't make a file upload read as a notebook edit. It shows the method, the decoded path (a long one loses its middle, never its end), and a preview of what is written (a workbook change's values, a OneNote page's content, or an upload's file handle), with the whole body and path under **Details**. It used to say only "Microsoft write?" with the method and path, so the values being written stayed hidden. To make that possible `microsoft_write` now refuses a path over 500 characters or a body over 8,000 characters or 14,000 bytes ("Nothing was written…"; emoji and most non-Latin text take 3 to 4 bytes a character); the agent makes bigger changes in several calls.

- The approval card for sending an email or invite no longer lets a line break in the subject, location, an address or the times start a line of its own. A subject such as "Hi\nTo: someone@else.com" used to show a second, fake "To:" line under the real one. Each field now reads on one line, without hidden direction-override or other control characters, and the card title cuts a very long subject at 100 characters (the Subject line keeps all of it). `microsoft_send` now refuses an address that holds an invisible character or is over 254 characters, so the address on the card is the address that gets the mail. Addresses, subject, location, start, end and time zone together may not exceed 3,000 bytes, so the card shows all of them.

- On Teams, an approval card with a long body (a mail of short lines, a OneNote page of HTML) no longer grows past Teams' message size limit and fails to post: Details uses at most 12 text blocks, with the line breaks kept.

- When an approval card can't be posted, the agent is now told the card could not be shown and nothing ran, not that the person refused.

- On Teams, Microsoft 365 mail, calendar, OneNote, sending and file download links now need the current 1:1 chat named on every call (the `origin_context_id` that `microsoft_search`, `microsoft_get`, `microsoft_write` and `microsoft_send` take). Nothing changes for normal use. Without it the tool says what to pass and does nothing; it no longer guesses that the person's one running turn is the caller, and it no longer answers "ask them in the 1:1 chat" in a 1:1 chat. A scheduled routine, which runs on the person's chat credential but has no chat of its own, can no longer read the owner's mail or calendar or get a file download link while the owner happens to be chatting with the same agent.

- In Teams, **Who answers where** and an agent's **Answers in** line now name each channel ("General → **qa-haiku**") instead of showing its raw conversation id in a code box with the agent name dangling below. A channel with no known name shows "Channel name unavailable".

- On Teams, when Microsoft refuses a `microsoft_write` because the file is locked (usually open in a desktop Office app), the model is now told nothing was changed and to ask whoever has the file open to close it and try again, instead of just "refused (423)".

- In Teams, clicking Stop on a reply's status card now turns that card into "Stopping…" without a Stop button right away. It used to show a "Cancelling…" banner that stayed on the card after the reply had stopped.

- A turn stopped, or cut off by its time limit, just after Daimon created its Managed Agents session no longer leaves that session running upstream with nothing pointing at it. The same goes for a database error at that moment. Daimon now archives the session before giving up; sessions it reuses or adopts from another turn are never archived.

- An agent's stored keys are listed, and written to its mounted `.env`, in the same order on every Postgres: by codepoint, whatever collation the database was created with. On a glibc or ICU `en_US` database (such as Cloud SQL) the order used to follow that collation, so `.env` bytes and their hash depended on the database. After upgrading, a live session whose keys sort differently (mixed case, or `API_KEY` beside `APIKEY`) has its `.env` swapped in place once, on its next turn.

- In Teams, 👍 on an answer now shows "Thanks." in a small dialog. It recorded the vote but showed "Unable to reach app. Please try again.", as did a refused vote.

- In Teams, a refusal or an error when opening another dialog (setup, routines, Ask a person, channel settings, credential requests) now shows its message in a small dialog instead of "Unable to reach app. Please try again."

- In Teams, an @mention, a thread reply or a group chat message posted while Daimon was restarting for an update is no longer lost: the next start reads it back from Graph, if it is under 10 minutes old, and runs it once. A message in a 1:1 chat cannot be read back, so one sent then still has to be sent again.

- A turn footer no longer shows a negative balance such as `$-0.01 left` when a turn settles just past zero; it reads `No credit left`.

- Agents whose GitHub repos span several organizations can now push a clone of a repo from another organization through the mounted GitHub helper (`github-repo.sh owner/repo push <branch>`); they used to push it with the working organization's token and get a 404.

- OpenAI agents with native web search and host-bound MCP now accept the provider's echoed default search fields and empty credential carriers during readback. Nondefault search controls and nonempty native credentials remain refused.

- Fleet workers get distinct names, so later mentions keep reaching the original agent. Defaults updates leave live workers alone.
- OpenAI agent switching names unsupported URL fetching as an explicit consented loss while preserving other source tools and skills.
- Discord turns cap answer output at five messages, combine excess prose into an attachment, and suppress repeated blocks and pure tool-call markup.

- GitHub's `pull_request_read` and `issue_read` tools now count as reads for tool safety, so reading a pull request no longer waits for an approval card. GitHub's write tools (`pull_request_review_write`, `merge_pull_request`, `mark_all_notifications_read` and the rest) still ask.

- **Tool rules on Daimon's own tools are enforced.** An `ask` or `deny` rule
  on `daimon-mcp/<tool>` was saved but never applied, because Daimon's own
  server was exempt from rule lookups. A chat or routine session whose agent
  has such a rule now pauses on `call_tool` and on the tools a rule names;
  the driver reads the tool inside the call, refuses a `deny`, asks on a card
  for an `ask` (refused in a routine), and lets every other call run without a
  card. A rule added or cleared takes effect on the agent's next turn. An
  agent `deny` also wins over `add_skill` and publish confirmations; no rule
  loosens a confirmation or the operator's deny list. Applies when
  `DAIMON_TOOL_SAFETY__ENABLED` and `DAIMON_TOOL_SAFETY__AGENT_RULES` are on.
  An agent-wide `deny` on `daimon-mcp/*` also makes the driver refuse every
  proxied call. Gemini and OpenAI turns for an agent with an `ask` rule or a
  channel `deny` on a Daimon tool are refused rather than run unasked; an
  agent-wide `deny` is enforced by the server and does not block them. A
  tool that waits for Approve (`add_skill`, publish tools) sent through
  `call_tool` is refused at once with a note to call it directly. `daimon-mcp/`
  rule names outside `[A-Za-z0-9_.-]{1,128}` are no longer accepted.

- Switching providers preserves supported MCP tools when a sibling needs confirmation; unavailable tools require explicit consent and remain blocked.

- Fleet workers can wake the root again after an Anthropic turn reaches its budget.
- Blocked fleet runs post a final Discord notice with worker statuses, without sharing unreviewed results.

- **MCP servers connected agent-wide now work in shared threads.** A shared
  thread's session mounted no vault holding the agent's shared MCP tokens,
  so such a server failed authentication there and every reply carried an
  "unavailable this turn" notice. The thread now has a vault of its own
  holding those tokens and nothing else; a token added, rotated or removed
  reaches it on the thread's next turn. The shared workspace still holds
  the agent's own credentials only, never anyone's personally connected MCP
  servers. A shared thread whose session was created before this change
  picks the tokens up once that session is replaced.

- **Delivered files say whose they are.** Files an agent saves arrive a few seconds after its reply, and in a Slack or Discord thread several people use, the next person's turn has often started by then, so one person's files looked like part of someone else's turn. Every delivery is now labelled with the person whose turn made the files, in every thread, not only shared ones. On Slack the first file (or oversize notice) of each delivery says "For @name", so that person is notified once. On Discord, a file that cannot go on the answer, and every oversize notice, replies to the person's own message and says "For @name" without pinging. Files are labelled, not reordered: they can still appear after a later turn's answer (#788).

- A GitHub App token mint rejected for one repository group now leaves other groups available, queues an installation refresh, and tells the next turn which repositories are unavailable, including after token rotation or reconciliation. Each repository's first refusal starts a 60-minute exclusion; rotation skips fully excluded groups, and a later turn retries with a replacement session after expiry. Notices survive reconciliation and are sent once per continuous refusal. Authentication and transient failures still stop session setup and close pending token rows.

- GitHub App grants stop covering repositories removed from an installation, suspended installations, and installations GitHub confirms deleted. A refreshed GitHub repository snapshot is intersected with the tenant's confirmed Connect set; webhook changes, Connect saves, and existing installations queue a refresh. Connect cannot clear a suspension recorded by reconciliation.

- Installations migrated while an older scheduler was still running no longer stay without a GitHub snapshot: the scheduler queues a refresh for any App installation that has never been refreshed.

- **Teams agents can reopen the files they delivered.** History replay and
  the MCP Teams read tools resolve the SharePoint links in daimon's own
  saved-files list to download URLs, inside the channel's folder only, as
  for shared files, and replay its previews as images. The list itself is
  left out of replayed text, and the agent guidance says daimon adds it:
  an agent had copied it from history into its reply and hedged about
  whether the file was saved.

- **A Teams restart no longer drops a turn's files.** Shutdown gave running
  work 15 seconds, while an output sweep polls the listing for up to 14
  seconds before it uploads, and it ignored a sweep that a turn ending
  during the drain started. It now waits 60 seconds for all of it, within
  the 90-second stop grace the self-hosting guide asks for.

- Require fleet reviewers to use a different backend from the primary, including retained reviews checked before summary delivery.

- Allow fresh Gemini preparation for a new mention thread while preserving an existing session when replacement is unsupported.

- Accept omitted empty Gemini catalog tool and source lists on readback while continuing to refuse capability drift.

- Enforce explicitly requested Gemini fleet worker input and output token envelopes through one tool-free model interaction, counting complete prompts before dispatch and retaining existing accounting holds.

- Preserve heterogeneous fleet budget ceilings when freezing a root; inherited workers use their provider cap while explicit excess requests still refuse.

- Keep native-only MCP servers and their credentials out of fleet worker grants,
  preserve exact builtin permissions, and declare incomplete native toolsets on backend switches.

- Backend switching refuses unknown fields and values on channel skills as source-definition obligations, even when all supported capability losses are confirmed.
- Backend admission refusals now log the failed gate and closed profile/cause labels (unknown labels are redacted), distinguishing workspace policy, invalid configuration, missing capabilities, non-runnable profiles and source preparation failures, so operators can distinguish them without exposing provider messages or agent setup.

- **A Teams turn no longer loses its thread to one slow Microsoft Graph read.** Reading the thread history or a message's images and files gave up on the first timeout, and the agent then said it could not read earlier messages. A Graph read that times out or loses its connection is now tried once more before the turn goes on without it. Writes, such as file uploads, are never repeated, since the first may already have landed.
- **A workspace too large to move whole now carries what fits, instead of nothing.** When a session is replaced (a model, instructions or environment change, or a handoff), the old session archives its working files for the new one, up to 20 MiB. An archive over that cap used to be deleted, and the new session started with the conversation but none of the files, so the agent could no longer see its own earlier outputs. The archive is now rebuilt once without its largest files, so the handoff note, scripts, tables and charts still travel, and the new session is told which files were left out, the first 50 by path and the rest as a count, so it can regenerate the ones it needs. Only an archive still over the cap after that rebuild falls back to the conversation alone, as before.
- **A Teams message caught by a restart is now answered after it, and no Teams message runs twice.** When a deploy or restart stopped the Teams adapter, a message queued behind a running turn was answered with "Sorry, something went wrong handling that. Please try again.", and one sent during the 15-second shutdown was dropped without a word. Both are now kept: the chat is told "I'm restarting for an update. I'll pick this up as soon as I'm back.", and the next process runs them in order through the usual checks. One more than 10 minutes old by then gets "I restarted for an update and couldn't get to this in time. Please send it again." instead; while the database is unreachable the chat hears nothing rather than a note that could prove wrong. Every message is now admitted once by its Teams id across processes and restarts, so a redelivery never starts a second turn, and each restart note is sent at most once. A message's text is kept only until its turn starts; a turn that had already started is still only marked interrupted.
- **Agents on the sandbox can now push their commits, and the PR names who asked.** `git push` and `git fetch` against `origin` hung until the tool timed out, because `GH_TOKEN` in the sandbox is a placeholder that only outbound requests replace. The agent guidance now has agents push with the explicit `https://x-access-token:${GH_TOKEN}@github.com/<owner>/<repo>.git` URL, without `-u`, and tells them `gh` commands work as they are. Slack now puts the requester's display name on the `<user_query>` as `author_name` (Discord already did), so "Requested by <name>" in a PR body is the real person, or "Requested in chat" when there is no name, instead of one the agent made up. Agents are also told never to say an edit worked unless the command's output shows it did.
- **A non-admin asking for the agent list no longer sees how agents are set up.** `list_agents` and `get_agent` now return each agent's name, description, model, skills and id to everyone, but the attached MCP servers (names and URLs), creation date and sync warnings only to admins; for other callers those fields come back as null.
- **An agent switched from a saved GitHub key to the GitHub App keeps its working repo.** App mode ignores the agent's old repo binding and mounts only a grant marked as the working repo, but the switch (a chat Connect, the agent's GitHub panel or `daimon github grants activate`) left the matching grant unmarked, so the agent silently stopped mounting its repo. The switch now marks the grant for the binding's repo as the working repo unless a working repo was chosen in the same step. A data migration (`0096_keep_legacy_working_repo`) does the same for agents already switched, when exactly one of their live grants matches the old binding; their next turn starts a new session that mounts the repo again.
- **Anyone in a Discord thread can now use its Connect GitHub button**, not only the person who asked. Before, anyone else got "Only @name can use this". Now the link opens privately for whoever clicks; what they can give the agent is capped by their own GitHub permission on each repo, and the person who asked must still manage the agent. The link stays single-use and is not tied to the clicker's own Daimon account.
- The scheduler no longer keeps a database transaction open for its whole lifetime after taking its singleton lock. That open transaction made concurrent index builds, such as migration `0093_spend_gate_indexes`, wait forever, so deploys stalled at the migrate step.
- On Discord, a server that is unavailable when the bot connects (during a Discord outage) no longer stops the startup sweep. Before, the sweep raised on it and skipped the command sync and the setup of every server after it until the next reconnect; now that server is skipped and synced the next time the bot starts or fully reconnects.
- Confirming a chat skill add (or a GitHub repo grant) with a polite yes now works. The reply still has to be the whole message, but it may now be `yes please`, `yes pls`, `yep`, `yeah`, `ok`, `okay`, `sure`, `go ahead` or `do it` as well as `yes`, `y`, `confirm` or `approve`, with commas and extra spaces ignored. Before, "yes please" cancelled the preview. A reply that adds anything ("yes but not now", "sure, after lunch") or asks ("ok?") still cancels it.
- A file the agent writes in its working directory now survives a workspace move (adding a skill, changing the model and the like). The sandbox shell starts in /, so a file written there with a relative path landed outside everything the move archives, and the next workspace could not find it. The loose files directly in / now travel with the rest, and the new workspace is told to put them back there.
- **Replying to a routine's result on Discord now gets an answer.** A reply that does not mention Daimon starts a turn only when it answers a post Daimon recorded as the agent's, and a routine's delivered result was never recorded. With agent identity on the result is posted through a webhook, which a reply cannot ping, so replying to it did nothing at all. Every message of the result is now recorded as the routine agent's post with the creator as the person who asked, so a reply to any of them starts a turn, with identity on or off. Only the creator or a server admin can have the agent edit or delete it. The answer of a turn started from a setup-wizard form is recorded the same way as a mention turn's answer, so replying to it works too.
- On Teams, replying to one of the bot's channel messages with **Reply** (a quote) now gets an answer again from clients that send the quote as an HTML blockquote rather than a `quotedReply` entity. Before, such a quote was not counted as addressing the bot, so the message was treated as unmentioned thread chatter and, outside a followed thread, got no answer. A quote of someone else's message, or of the bot inside someone else's quote, still needs an @mention.
- A workspace whose tenant id begins with the same 8 characters as an existing tenant's can no longer be registered. Skill titles name a tenant by those 8 characters, so the two could have seen and changed each other's skills; the database now refuses the second one, even when both register at once, and registration fails with an error naming the workspace.
- A Discord mention that has to wait for a free turn slot (the server's turn cap, or the process-wide one, is full) now gets the ⌛ reaction while it waits, the same marker a mention queued behind its thread's running turn gets, and loses it when the turn starts (or is stopped or times out in the queue). Before, it showed only 👀 for the whole wait, which looked the same as a turn already running. Slack and Teams are unchanged.
- A turn's "Working on it" card keeps counting up while a long tool call runs (for example a 45-second command), updating about every 10 seconds on Discord and every 5 seconds on Slack and Teams. Before, nothing changed on the card until the tool finished, so a long step looked hung. The Teams card now shows the elapsed time too, under the draft.
- A Discord private form (the **Enter it privately** button for a key, token or repo) that nobody clicks now turns into "⌛ This form expired." with a line saying how to ask again, and loses its button, within about a minute of expiring. Before, it kept looking live until someone clicked it and only then said it had expired.
- A Discord private form (the **Enter it privately** button for a key, token or repo) that nobody clicks now turns into "⌛ This form expired." with a line saying how to ask again, and loses its button, usually within a minute of expiring. Before, it kept looking live until someone clicked it and only then said it had expired.
- Turns started with the MCP agent-chat tools (`start_turn`, `continue_turn`, `ask`) are now billed, at the same price, markup and budget channel as a chat turn and once only. They are settled the first time `get_my_session`, `list_events` or `ask` sees the turn finished. Before, they were billed only by the usage sweep, which is off by default, so they were not charged at all.
- The usage sweep (`DAIMON_SCHEDULER__USAGE_SWEEP_ENABLED`) now settles only the agent-chat sessions whose finished turns nobody read, instead of listing every session in the Managed Agents workspace, and is on by default. Calls made before this release are not charged automatically: `daimon usage settle-backlog` debits them for the internal tenants given and waives them for everyone else.
- Archiving an agent (`daimon agents archive`, the `archive_agent` tool, the Slack panel's Delete) now drops its agent rule along with the channel defaults naming it. Before, the rule stayed, and a later channel copy given the same name (copy names are cut to 40 characters, so two channels can share one) was refused with "<name>'s rule names other channels, so it can't be used for Own agents only". A channel copy also skips any name that still has a rule, and `daimon agents rule set … <name> --anywhere` (or `set_agent_rule` with no channels) now drops a rule whose agent no longer exists, so rules left by agents archived earlier can be cleared (#22).
- Archiving an agent (`daimon agents archive`, the `archive_agent` tool, the Slack panel's Delete) now drops its agent rule along with the channel defaults naming it. Before, the rule stayed, and a later channel copy given the same name (copy names are cut to 40 characters, so two channels can share one) was refused with "<name>'s rule names other channels, so it can't be used for Own agents only". A channel copy also skips any name that still has a rule, and `daimon agents rule set … <name> --anywhere` (or `set_agent_rule` with no channels) now drops a rule whose agent no longer exists, so rules left by agents archived earlier can be cleared. It never drops a rule a live agent still answers to, under its MA name, its config name or, for a report reader, its source agent's name.
- `daimon skills delete` and the `delete_skill` chat tool refuse to delete a skill an agent or channel still uses, saying where it is attached and how to detach it. Before, the delete succeeded and every later turn for that agent or channel failed. `daimon skills delete --force` detaches it from every agent and channel in every tenant first (its confirmation says how many, and how many are outside the selected tenant), then deletes it. A skill being attached while it is deleted is no longer left attached to a deleted skill: one waits for the other, and an attach that loses says the skill was just deleted. Syncing a skill repo no longer deletes a skill removed from the repo while another agent or a channel still uses it, and the startup cleanup of duplicate default skills keeps a duplicate that is still in use.
- The defaults sweep that runs on every start no longer deletes a library skill that only a channel adds; before, every turn in that channel then failed.
- A turn whose agent or channel still names a deleted skill now says which skill is missing and to ask an admin to remove it from the agent's or channel's skills, instead of "Daimon's AI service couldn't accept the request" or "Ask again to retry", including on the mentions that follow while Daimon waits before retrying.
- A Discord turn with completion pings on no longer shows its summary line (time, cost, money left) twice. The status card used to carry the summary while the pinged answer was posted below it with the same summary, until the card was deleted; when that delete failed, both stayed. The card now reads "Done." without the summary, and only the answer carries it. If the answer cannot be posted before its summary is up, the card gets the summary back.
- Opening an expired, revoked or unknown notebook or report link in a browser now shows a plain "Link unavailable" page asking the sender to share a new link, instead of a raw JSON error. The status codes (404 for notebooks, 403 for reports) and the link checks are unchanged, and API clients still get JSON.
- Mentioning Daimon in a Discord channel whose **Who can post** rule is **Nobody** is no longer met with silence that looks like an outage. Your message gets a 🔒 reaction and you get a direct message saying Daimon can't reply in that channel and who can change the rule: a server admin in `/agent-setup` → **Who answers where** → **Permissions** for a channel rule, or whoever runs Daimon for a category rule. It is only sent when a rule really matches, not when Daimon refused because it couldn't look up the channel's category. Daimon still posts nothing in the channel and runs no turn. You're told at most once per channel every 10 minutes (`DAIMON_DISCORD__PROTECTED_CHANNEL_NOTICE_COOLDOWN_MIN`).
- A new Discord thread is no longer titled after the agent's internal name (for example "Chat with qa-haiku-canary-20261010") when the automatic title is unavailable. It is titled with your opening message instead, cut to 50 characters, and a generated title that names nothing, such as "Greeting" or "Help request", is replaced the same way. A bare mention with no text is titled "Chat with" the bot's name.
- Model calls an agent made while its chat worker was down (a deploy or crash mid-turn) are now billed when the next turn in that thread ends, at the same price and once only. Previously they went unbilled unless the usage sweep was switched on.
- When your team's Daimon credit runs out, the reply now says how to fix it: "An admin can top up with `/billing`." on Discord and Slack, and `billing` in a 1:1 chat on Teams. Before, it only said "Ask an admin to top up." without naming where.
- A routine that posts to a channel or thread now says so there when it fails, instead of posting nothing. The notice names the routine, says what went wrong in plain words (for example "The request to the AI model failed." or "Your team's Daimon credit has run out.") and that it will try again at its next scheduled time. It is posted once when a routine starts failing, not on every failed run, and again only after a run has succeeded.
- A file the agent makes that is over the upload limit (10 MB on most Discord servers, 20 MB on Slack and Teams) is no longer deleted. It stays in the agent's workspace and, on Discord and Slack, the thread gets one notice saying it was too big to attach and that you can ask for a smaller version or parts under the limit; later turns don't repeat the notice. Teams keeps such a file too, including in a channel without file access.
- A Discord deploy now waits for every turn in progress, including wizard submits and DMs that are still queued or preparing, and starts no queued turn once it begins cancelling. A thread turn cut off by the deploy keeps its marker, so the next process delivers its finished answer or interrupts its session; a cut-off DM interrupts its own session. Before, a wizard turn was invisible to the drain and its session could keep running with nothing left to recover it.
- Deleting a Discord thread while it is answering no longer leaves an error message in the channel. The turn stops, nothing more is written to the thread, and messages queued behind it are dropped instead of started. A deleted thread's chats are retired so a restart does not revive them. A thread that cannot be written to because it is archived and cannot be reopened stops the turn the same way, but keeps its chats.
- A reply to an earlier Discord message is now answered about that message. The agent is told which message you replied to, including one from before its last turn, or part of a long answer, instead of treating your reply as being about the latest message.
- Discord turns no longer show the agent their own status card as an empty message from itself right before your request, so it stops saying the message you replied to "has no text".
- The **New agent** button in `/agent-setup` opens its form again. Discord had been rejecting the form, so clicking the button showed "This interaction failed" and no agent could be created from the panel.
- Deploys no longer leave Discord buttons and slash commands unanswered for about 14 seconds: the new Discord container starts beside the old one and takes over the gateway through a database lease as soon as the old one has drained and closed.
- The report viewer loads its stylesheet and script on installs that resolve FastAPI before 0.137, instead of staying on "Loading…".
- Replacement checkpoints now report priced model calls in turn outcomes while retaining their predecessor-channel debits.
- Restart recovery detaches a chat session when interrupting its orphaned turn fails, so the next message starts a fresh session instead of reaching a busy one.
- Long Discord answers keep the consumed-input watermark while omitting their delivered answer chunks from the next turn's history.
- Queued Discord mentions in one thread now reach the agent together, while a separate confirmation message starts its own turn.
- Reused chat sessions are replaced when their budget channel changes, so the usage sweep charges missed turns to the channel that admitted them.
- New Discord threads retain links to recent channel uploads after busy chat moves them outside the 25-message context window.
- Discord continuations check thread history back to the queued handoff and retry when that history is unavailable, so a newer human reply cannot trigger stale work.
- Follow-up turns obey the tenant's current concurrency cap after an operator changes it, without starting queued turns above a lowered cap.
- Registered Discord tenants with an unjoined guild receive defaults updates on restart, so their agents no longer keep stale guidance.
- One `yes` now approves only the latest skill-add preview in a thread, even when the previews target different agents.
- Canceling Connect GitHub says "No changes saved", covering additions, removals, and working-repo edits.
- After posting the Connect GitHub card, the agent no longer repeats the card's steps in its reply.
- Connect GitHub links offered for a named repo open with that repo selected when it is available to add.
- QA chunk delivery gaps use the answer edit time for cards observed running during the same turn; missing edit evidence remains pending.
- QA reads nested Discord V2 card text for current-card assertions and records component-only card edits; unreadable card copy stays pending.

- QA observes queue progress text during live turns and can post thread follow-ups before the first answer completes. Overlapping follow-up evidence is attributed per turn, and unattributable evidence stays PENDING.

- QA running-card edit checks retain direct message snapshots on every poll and report missing or insufficient capture as PENDING.
- QA running-card edit checks retain direct message snapshots on every poll, count successive changes, and report missing, sparse or gapped capture as PENDING. A failed card readback is recorded as unavailable instead of stopping the scenario.
- Agents created before the Connect GitHub change get the new guidance on the next restart.
- Agents no longer suggest pasting a GitHub token. When a private repo can't be read, they offer the Connect GitHub link. Agents that already use a saved token keep working.
- QA reports model verification as n/a PENDING on confirmed pre-admission skipped turns, preserving product silence failures and recording zero spend.

- Approval cards still up during a Discord restart are retired before the client closes, so their buttons no longer stay live.

- **QA accounting and alerts:** Sum final run receipts rather than differences in shared reservations; support file-only alerts without external delivery.

- **QA evidence:** Strip recorded agent headers before anchored answer full-matches, isolate invalid non-canary catalog contracts, and retain channel/thread observations.
- **QA context:** Keep channel-history seeds unbilled, with actual turn numbering.

- Turn cards on Discord, Slack, and Teams now add each Haiku 5.5 request's cost at its own prompt-length rate.
- Judge and harness PENDINGs count toward the three-run alert threshold while individual unavailable runs stay silent; mixed burst timeouts retain their watch FAIL evidence.

- Live QA alerts advance dedupe after durable inbox delivery and record queued or failed tsend status without halting a catalog pass.

- `/privacy` deletion results now say whether account deletion succeeded and whether chat transcript deletion is still incomplete.
- Live QA burst posts follow their declared cadence with concurrent per-trigger watchers; first-response latency uses Discord creation/edit timestamps, and anchored text checks keep content separate from embed footers.

- Live QA records private redacted tracebacks for harness errors, reports them as PENDING without root alerts, and retries a transient read-only model-probe failure once before refusing a trigger.
- Live QA suppresses alerts only for judge execution failures, preserves other PENDING streaks, and records the sanitized exception type/status in run notes.
- Live QA's Haiku 5.5 judge omits its deprecated temperature parameter. Judge execution errors preserve product checks as PENDING without root alerts; failed verdicts still fail and alert.
- Discord turn cards serialize progress, recovery, and terminal writes per message, so an older in-process edit cannot restore Working and Stop after an answer, stop, or error. Recovered turns take ownership of the same card; failed turns preserve sealed answer text. The write order is model-checked and logged for replay.

- Live QA records private redacted tracebacks for harness errors, reports them as PENDING with consecutive-unavailability alerts, and retries a transient read-only model-probe failure once before refusing a trigger.
- Live QA preserves PENDING streaks and records the sanitized judge exception type/status in run notes.
- Live QA's Haiku 5.5 judge omits its deprecated temperature parameter. Judge execution errors preserve product checks as PENDING with consecutive-unavailability alerts; failed verdicts still fail and alert.
- Older live QA configs that omit `dated_snapshots` now inherit the approved backend default when loaded. Explicit conflicting settings still refuse.

- A turn on a dated model snapshot id (e.g. `claude-haiku-4-5-20251001`) is priced at its alias row instead of going unbilled.
- Live QA admits the primary alongside its temporary staging override and accepts exact Haiku 5.5 dated snapshots in deployment evidence and judge replies.

- `/agent-setup` now tells admins when a channel's own permissions deny Daimon Manage Webhooks even though the server grants it, so new agent posts there may show as Daimon. Each line names the channel and the role, `@everyone` or member entry that denies it, for up to 5 channels.
- The live QA runner now targets the replacement staging `qa-live-runs` category after the old category was removed.
- Live QA preserves measured model and token evidence when a turn has no priced cost, and retains transient trigger reactions observed during progress. Unknown catalog kinds mark their scenario PENDING without blocking supported scenarios.

- Rejected or failed MCP and GitHub token forms on Discord, Slack and Teams now show an error with a usable retry button. After 90 seconds the card says it is still saving and will update when finished; long skill imports continue until success or error.
- A failed attempt to republish a report no longer disconnects the existing report.
- Slack, Discord and Teams members now see only locally routed responders and agents explicitly allowed in their channel, across setup panels, agent tools, skill reads and routing explanations. Unrouted agents and agents belonging to other channels stay hidden; admin visibility and channel isolation are unchanged.
- Discord form, credential and app install buttons now check private thread membership before posting for someone.
- Report PDF upload links now expire with their reader turn, including uploads still streaming when that turn ends.
- Stop subscribes to the session's events before sending the interrupt, so a fast stop no longer waits two minutes and reports "Stop not confirmed".
- Worker deploys allow 95 seconds for container shutdown, exceeding the worker compose asset's 90-second grace for turn draining and cleanup.

- Slack keeps turns and Stop buttons separate when message timestamps match in different channels or workspaces.
- When an agent with a rule, or one in a channel kept to its own agents, is refused a publish or upload after the approval step, the MCP server now logs why (`publish_gate.needs_approval`, with the missing proof: no turn origin, no live session, a session that does not hold the tool on a card, another agent's session or an unreadable session). The refusal used to log nothing, so a missed Approve could not be traced.
- Discord replaces a missing status card before delivering an answer and ignores stale message edits after delivery, instead of posting a false Unknown Message error. Stopping a deleted card still interrupts the turn.
- Refused Slack Stop clicks in a thread now show their explanation in that thread.
- Discord acknowledges mentions with 👀 before resolving agents and environments, and clears it when the turn settles. Queued follow-up ⌛ markers and Slack 👀 markers also clear on completion, failure or cancellation, regardless of completion pings.
- Routine digests now deliver the full final reply across Discord, Slack and Teams messages, including creator fallbacks. The saved last-output preview shows the actual tail and marks truncation.
- Replies and error messages to threaded Slack DMs now stay in the same thread.
- Discord failures use plain copy instead of provider JSON, exception bodies, request IDs or tool details. Overloaded Claude requests ask for a retry in a minute. A failed reply allows for files still arriving, and the output sweep no longer claims a file was too large to attach after it was already posted in that turn.

- On Discord, the first reply in Daimon's thread that doesn't @mention Daimon gets a 🔔 reaction, once per thread a day, as a hint that only a mention continues the conversation. The reply itself is still not answered. Turn it off with `DAIMON_DISCORD__UNMENTIONED_REPLY_HINT=false`.
- Slack approval replies now appear in the same thread as the approval card.
- Daimon no longer tells you an earlier message "never reached me" when it was answered in another thread or didn't mention Daimon. It now says it only sees messages that mention it and that other threads are separate conversations.
- Daimon no longer tells you an earlier message "never reached me" when it was answered in another thread or wasn't addressed to Daimon. It now says the message may be in another thread or wasn't addressed to it, and that other threads are separate conversations. Conversations already under way get this too.
- Slack now shows a file delivery failure in every affected thread, once per thread.
- Expected Discord setup, registration and credential refusals keep their authored next steps through an explicit safe-copy error type; name-collision guidance is raised by Discord’s actual New Agent creator as well as the shared creator. Unknown exception bodies stay in logs.
- Discord failures use plain copy instead of provider JSON, exception bodies, request IDs or tool details. Overloaded Claude requests ask for a retry in a minute. A failed reply allows for files still arriving, and the output sweep no longer claims a file was too large to attach after it was already posted in that turn.
- When a Discord turn fails after its thread opened, the error now replaces the turn's status card in that thread. It used to be posted in the parent channel while the card kept spinning with its Stop button. An answer already on screen is never overwritten; the error goes under it.
- Discord can catch up missed mentions and replies after restart when replay is enabled, without duplicate turns; interrupted cards show recovered answers or restart notices, and transient worker ownership loss is retried.
- A rejected notebook re-publish keeps the previous source, so its link still opens the original notebook after an idle stop or host restart.
- When a restart cuts an answer short, the card now reads "Daimon restarted before this request finished." and "@mention Daimon with your request to try again." in the same words on Discord, Slack and Teams. Teams used to say "Send a message to try again.", which does nothing in a channel.
- Discord form buttons acknowledge quickly, and a slow form lookup asks you to try the button again instead of failing silently.
- Discord agent tools now check a caller's thread access and posting rights before sending, and split long messages when agent identity is unavailable.
- A Discord thread that can't be opened gets one plain reply under the mention: "Couldn't open a thread. @mention Daimon again." It used to be the generic error with a request id. A failed typing indicator no longer stops the thread from opening.
- Long Discord support and feedback notes now reach operators in ordered, message-sized parts; failed feedback delivery is logged.
- Discord thread names are short and plain, like "Shorten the competitive brief" instead of "Simplify Text By Removing Jargon While Keeping Key Points": sentence case, at most 50 characters. Attached file names count, so a mention with only a file gets a name too.
- Discord's `/billing`, `/agent-setup`, `/routines`, `/privacy` and `/memory` answer with an error when a database read fails, instead of staying on "thinking" forever.
- A failed skill upload now asks for a fresh preview and `yes` reply before another attempt.
- **Teams answers arrive again when support is set up.** The answer's last message carried the Ask a person button in a card beside its text, and Teams refuses an edit carrying both, so every answer was replaced by "Something went wrong. Mention me to try again." The button now follows the answer in a message of its own.
- A conversation is no longer stuck after Daimon restarts while an approval card is up. The next message used to fail every time. Daimon now clears the unanswered request and carries on, and if that ever fails the thread says to start a new thread.
- Teams keeps a generated file available for another try if its DM file card fails to send after upload.
- A new agent's face is rendered when the agent is created, so its first card or answer has it instead of the Daimon picture. This covers the setup panels' New agent form, `create_agent`, copies (`fork_agent`, `daimon agents fork`, channel rules that copy an agent) and `daimon agents create`; CLI commands wait up to 10 seconds for it.
- Discord acknowledges a GitHub requester's Link click before minting the link and resolving the card. Connect cards clip long agent names, escape Discord markdown, and load images from the configured app host.
- Teams keeps a channel file available for another try when its upload hits a temporary Graph failure.
- The `used` amount on a finished Discord or Slack answer is what the tenant is debited, markup included, so it agrees with `left`. With a 1.1 markup it showed the raw model cost, 10% low.
- A deploy no longer stops an answer that is about to finish with "Stopped: Daimon restarted." When a deploy replaces the worker containers, in-flight turns now get up to 60 seconds to finish, as the adapters intended; Docker killed them after its default 10.
- Archived workspaces no longer start scheduled routines, including when archived after a routine was claimed.
- A finished Discord or Slack answer ends with one short line instead of a five-line Details block: `Ada  12s  $0.042 used  $41.20 left`. Fields are set apart by spacing, not dots. Token counts are gone, and a channel budget's remainder reads `$8.10 left` like the prepaid balance.
- On Discord, the files an agent makes are attached to its answer, the same message that carries the summary line and the 👍 👎 🙋 reactions, instead of arriving as separate posts after it. The summary line no longer moves onto a file post, which left an empty-looking card at the end of the thread. Agents on Discord, Slack and Teams 1:1 chats are now told to put only files for the person in their outputs folder, keep working files such as `.typ` sources elsewhere, name files instead of saying "above" or "below", and not post a file into their own thread themselves.
- Routines scheduled during a repeated daylight saving hour now wait for the next real time slot.
- The summary line and Discord's 👍 👎 🙋 sit under the last message of an answer, not the first: the last chunk of a long answer on Discord and Slack.
- Discord no longer attaches a generated file twice when the agent already sent it in the thread itself.
- Privacy deletion now includes sessions belonging to archived agents.
- On Discord, a reply to any message of an answer reaches the agent, not only a reply to the first one. With agent identity off, a reply without the ping to the card or a later chunk used to be ignored.
- Hackathon staging layouts accept the existing QA admin bot and recognize Discord Administrator permissions, so private team roles can be provisioned without the roleless fallback.
- Canceling a GitHub connection now revokes its user token and prevents that connection from being confirmed later.
- The file-handling skill now tells agents to export only the current turn's finished deliverables, keeping working directories and already delivered files out of outputs to avoid duplicate attachments.
- Privacy deletion now retries Managed Agents transcript deletion after upstream failures or a restart.
- A copied agent keeps the skills its source added itself again. Copying (a channel's own agent made with `--copy-from`, or `fork_agent`) downloaded each skill with a header a workspace API key is refused for, so the copy silently left those skills off. The workspace recovery export downloads skills the same way and is fixed too.
- A turn with several approval cards no longer fails with "The agent stopped because it couldn't confirm your approval" when the stream reconnects while the answers are being applied. An approval card that takes longer than two seconds to update now still updates, instead of keeping its buttons.
- Agent setup changes now require server or workspace admin rights unless the agent is restricted to channels the caller administers. The same ownership check protects MCP connection forms and skill changes.
- A new agent's first Slack or Discord answer now shows its generated face. The first turn waits up to three seconds for the face to render instead of posting without a picture.
- GitHub connection links now appear as buttons where the person asked, without GitHub setup DMs. Discord conversation buttons mint the invitation on the requester's click and reveal its URL ephemerally; Slack uses ephemeral messages in the originating channel or thread. After a self-serve connection, interrupted work resumes in its thread. A bare connection confirms ephemerally where possible, with a count-only Discord thread fallback. Failed confirmations back off and expire after eight attempts or 24 hours.
- GitHub new-repo and removal notices are available when an admin opens the setup panel; they are no longer pushed automatically.
- Discord agent posts and MCP tools now wait at most two seconds for a new channel webhook, then post through the bot with the agent name while creation continues in the background. Creation is deduplicated per channel, 429 retries respect a cooldown, and new channels create one webhook.
- **The scheduler's usage sweep no longer re-replays every model call it has
  already metered.** It asks the API for `span.model_request_end` events only
  and skips events already in `usage_events` before writing, so the startup and
  hourly full passes cost one query per session and a read of its model calls,
  not every event it holds and one transaction per call. On a workspace with
  tens of thousands of metered calls those passes ran for many minutes of CPU.
  The sweep also runs on its own loop now, so a long pass no longer holds up
  routine claims. Billing is unchanged.
- Identity exclusions now leave unscoped DMs on the deployment switch, and Discord card recovery retires aged pending cards after repeated lookup failures without dropping cards found during a pass.
- Discord card recovery now keeps the hourly pass away from live turns and startup reconciliation, retires aged intents after repeated failed passes, and uses "Stopped." for cards found while the bot is running.
- The Discord webhook capacity model now accounts for lifecycle edit debounce; its overload cases distinguish cold bursts from the removed-debounce counterfactual.
- Discord restart recovery does not replace an uneditable orphan card. Aged intents stop blocking channel tidy only after a definite recovery failure; a periodic pass revisits them. The bot deletes a known stale card only while it still carries that turn's pending button. Missing messages no longer count as failed deletes, while unknown webhooks do.
- Discord keeps a pending turn-card intent when recovery cannot edit its webhook card or an unprompted turn cannot delete its card, instead of retiring an unresolved card.
- Discord picture upload retry expires cleanly, and a completed upload still succeeds if its setup panel was dismissed.
- The Docker Compose `init` service runs migrations again. It called `uv run alembic`, which failed with a permission error writing `/app/uv.lock` as the image's non-root user.

- Repeated GitHub Connect repos submissions show the successful repo count.
  The submit button disables while connecting, and new selections default to
  read and write access.
- **Sonnet 5.5 cache reads are billed at $0.10 per million tokens**, Anthropic's price since 2026-10-07. Daimon still charged the old $0.20, so the default model's cache reads cost tenants twice the list price in estimates and ledger debits.
- Approving a tool call no longer fails the turn with "re-requested approval for tool call(s) already confirmed". The approved call now runs inside the turn, so a notebook or attachment publish is no longer refused after Approve.
- Allow-listed Discord QA bots can start a turn by replying without a mention to a recorded agent post, as they already can by mentioning Daimon.
- Slack keeps the agent name in answer footers when it accepts custom header fields but posts with the bot's header.
- GitHub App tokens now refresh during running turns before they expire. Repository resources and vault credentials update in place; a superseded token keeps working until its natural expiry, and is revoked early only when the installation, link, grant or repository authorization is removed or staged, or a later refresh narrows its access. A failed refresh keeps the session open and retries after one minute, doubling up to 30 minutes. A session that has been running continuously for more than 12 hours stops refreshing; seeing it idle resets that clock.
- When a GitHub App token refresh fails partway through a running turn, the new tokens already in the session's repositories or vault are no longer revoked, so a tool that picked one up keeps working until the token expires. A new token that grants more than the session still holds is revoked at once, and tokens that never reached the session are revoked as before.
- **Notebook names work for every account.** Each notebook slug starts with a 12-character tag derived from the account, and about 1 account in 64 has a tag beginning with `-`. The notebook host refuses slugs that start with `-`, so every named notebook from those accounts failed at upload with a 400. One in 64 unnamed notebooks failed the same way. Those tags and random slugs now get an `x` in front; every other account keeps its tag, so existing notebooks stay where they are.
- **Discord's help button says "🙋 Ask the team" too.** The button in the direct message after a 🙋 reaction, and its form, use the same name as Slack; the form's text box says "Someone from the team will reply."
- **Slack's Ask a human button is now "🙋 Ask the team"**, with an emoji like the 👍/👎 beside it. Its form, notices and refusal say "the team" too ("What do you need help with? Someone from the team will reply."), and the support wording shared with Discord no longer says "a human".
- **Slack's "What went wrong?" and Ask a human forms open again.** Both declared a 4,000-character text box; Slack caps a text input at 3,000 and refuses the whole form when it is larger, so every 👎 form and every Ask a human note form failed with `invalid_arguments` and nothing opened.
- **Slack turns that only ran tools can be rated and can ask a human.** Their finished status card now carries 👍/👎 (and Ask a human when it is enabled), as Discord's tool-only turns already did.
- **Slack's Ask a human button no longer looks dead on a slow check.** The click opens a "Checking…" form at once and replaces it with the note form or the reason there is none. Before, the checks (including a live user-group lookup) ran first and could outlast Slack's 3-second window, so nothing opened.
- **Slack feedback acknowledgements land in the answer's thread** instead of the channel.
- **Archiving the thread an agent is answering in no longer breaks its reply.** `archive_thread` on the turn's own thread archived it at once, so Discord refused the turn's final card edit: the card stayed on "Thinking", an error was posted, and that post reopened the thread. The archive now happens once the turn has posted everything, generated files included, and the tool says it is scheduled. A refused call schedules nothing, and a thread a newer turn has started in by then stays open. The `channel-tidy` skill tells agents that tidying a thread does not include archiving it, and to say a thread will be archived only after the tool scheduled it.
- **Slack `/help` lists `/memory` and `/dm`.** Both commands worked but were missing from `/help`, so its list didn't match the commands Slack offers.
- **Slack `read_thread` stays within 200 messages and its cursor reads older replies.** Slack adds a thread's root to every page, so a read at the 200-message limit returned 201. The continuation hint also said "newer" while Slack returned the thread's older replies. The root now counts toward the limit, so a page holds at most 200 messages; a limit below 2 still reads the root and one reply. The hint and tool description say the cursor reads older replies.
- **A top-level Slack mention sees what was just said in the channel.** A fresh mention replayed only its own new thread, so a question about a message posted moments earlier went unanswered. Its first turn now gets up to 24 messages before it, as on Discord, read under `read_channel`'s rules. Mentions inside a thread replay that thread as before.
- Concurrent mentions no longer rewrite an unchanged account role or platform roles on every admission. Policy and config share a database checkout, as do balance, user cap, channel budget and channel skills reads, while keeping their gate order. Managed Agents agent and environment retrieves share concurrent requests and cache live results for 30 seconds, reducing admission latency and API request pressure during bursts.
- **Channel admins can ask their own agent to change its setup.** Discord and Slack turns now tell the agent when the requester administers the current channel, so it can call the setup tools for that channel's agent. The tools still check each target and refuse changes outside the caller's authority. Discord replies retain the active channel budget's remaining amount on the finished message.
- **`add_skill` and the publish tools wait for Approve.** A chat session lists daimon's tools through `search_tools` and `call_tool`, and Managed Agents applies a tool's `always_ask` to the name the model calls. Called through `call_tool`, `add_skill` saved a skill, and `publish_report`, `create_notebook_upload_url` and `create_attachment_upload_url` ran, with no approval card. These four tools are now listed under their own names in every chat session, so their card shows, and `call_tool` refuses them. Search still finds them.
- **Slack agents with a custom name answer the app mention.** An agent whose name differed from the Slack app's could refuse ordinary requests, reading the `<@U…>` mention of the app as another bot, and its own "Thinking" card in the replayed thread as that bot already answering. The turn's controls now name the workspace's bot user and its mention as the responder on every Slack turn, including queued, continued and recovered ones, and the thread replay leaves out the turn's own status card. Earlier answers from the app and other bots' messages are still replayed.
- **Code in Slack answers shows `<`, `>` and `&` as written.** Inline code and fenced blocks in a final answer showed `&lt;` and `&gt;` where the answer had `<` and `>`. Code is now sent unescaped; prose around it is escaped as before, so Slack-style links in prose stay clickable and broadcast mentions stay inert.
- **Answered Slack requests lose their ⌛.** Mentions queued behind a busy thread kept the hourglass after they were answered, so finished work looked pending. The hourglass now comes off every queued message once its request is answered, fails, is cancelled or is dropped unanswered. A failed removal is logged and never affects the turn.
- **`add_skill` says why it could only preview.** When no confirmation card can show, the preview and the refused confirm say whether approval cards are off for the deployment (`tool_safety.enabled` is false) or why this conversation can't show one, and how to fix it where the person can. Each also points to Add skill in `/agent-setup`.
- **A reply timestamp no longer reads a sealed thread.** Slack `read_thread` given a reply's ts read the whole parent thread, text and files, checking the seal only on the ts it was given. It now checks the parent's seal too.
- **A cancelled turn says so.** A cancelled Discord or Slack turn someone asked for shows "Turn cancelled." even after tool calls, keeps any partial answer, and sends no completion ping or feedback buttons. An unprompted Discord turn cancelled before it has an answer stays silent.
- **Slack links in answers are clickable.** `<https://example.com|label>` in a final answer renders as a Markdown link; code examples stay literal and broadcast mentions stay escaped.
- **Slack threads replay more than 15 messages.** Thread context requests `slack.history_page_limit` messages (default 100, the depth Discord replays). Slack still clamps workspaces it rate-limits to 15, and a partial replay is marked truncated. `read_channel` and `read_thread` return at most 200 messages a call, as Discord's `read_channel` does.
- Privacy deletion removes encrypted GitHub user tokens after the last linked account is erased, and shows the removed link in Discord, Slack and Teams panels.
- **An isolated channel's content no longer leaves through new agents, timers
  or reads.** An isolated channel's own agent, wherever it runs and through
  any of its coding-tool tokens, could call `create_agent`, and the new agent
  answered outside the channel with whatever prompt was written into it. Such
  calls, and `create_agent` from the channel's setup thread, are now refused
  (`channel_isolated`), admins included, as is a chat turn's `create_agent`
  naming no `origin_context_id` while a channel in its workspace is isolated.
  `list_timers` also showed the notes of timers set in an isolated channel
  from anywhere; they now list only inside that channel, as its routines do. A
  call held to an isolated channel, by its own agent or by a turn inside it,
  now also reads only that channel and the conversations that ran there:
  `read_channel`, `read_thread`, `get_message`, `list_threads`, the session
  tools and searches refuse or withhold everything else, and `list_channels`
  lists only that channel, so a prompt planted in the channel can no longer
  have its agent read another channel and repost it there. Admins' hub reads
  of conversations are unchanged. A chat turn inside an isolated channel that
  leaves out its `origin_context_id` is held there too, and
  `get_channel_budget` and `get_thread_participation` look only inside it.
- **Publishing and daimon's server name stay inside isolated channels.** A
  pinned agent, an isolated channel's own agent, or a turn in an isolated
  channel publishes a report, notebook or attachment link only once the
  requester presses Approve on a card (agent keys and runs nobody watches
  can't), and can't change daimon's server-wide name or avatar, admins
  included; these tools now take the turn's `origin_context_id`. No one publishes a reader of an
  isolated channel's own agent. A chat turn whose agent can't be
  found is refused while a channel is isolated instead of being treated as
  outside it.
- **Environment names across isolation.** An environment only isolated
  channels pick no longer shows in `list_environments`, `get_environment`,
  `get_tenant_summary` or a channel admin's picker outside them, and the
  environment-changing tools treat it as missing; operator tokens and server
  admins on the panels still see every name.
- **Open networks in sealed channels need confirming.** A server admin's
  environment pick or clear that leaves a sealed channel on unrestricted
  networking now waits for `confirm_open_network` on
  `set_channel_environment` and `clear_channel_environment`; the panels write
  nothing and point to chat. The same holds for a workspace default that
  sealed channels without their own pick follow, for `update_environment`
  opening the network of an environment a sealed channel runs in, and for
  `archive_environment` dropping one onto an open fallback; both tools take
  `confirm_open_network`. A sealed Discord thread counts under the channel
  its sessions ran in, also for a pick made on that channel directly.
- **Isolated runs from the hub or a DM count toward the channel budget.** A
  run of an isolated channel's own agent from the hub or a DM, which only
  admins and that channel's admins may make, is now gated by and charged to
  that channel's budget, so closing the channel with a $0 budget stops those
  runs too.
- **Teams routines saved before their channel was recorded.** Such a routine
  is now placed in the channel its destination id names. Teams ids contain
  ":", which split the id at the wrong place, so the isolation and
  environment checks treated the routine as outside its channel.
- Cold Managed Agents tag resolution now caches every tenant tag returned by a listing, and concurrent misses for the same tenant and resource kind share that listing. Resolver entries last 30 minutes; a missing or archived resource found during turn admission invalidates its entry, and an archived agent still triggers the existing scope cleanup.
- Fresh and replacement session preparations now queue before taking an advisory-lock connection, so a burst cannot exhaust the Discord worker's Postgres pool while Managed Agents creates sessions. `runtime.health` reports active and waiting preparations as `prep_gate`.
- Compatible session preparation releases its Postgres connection during vault I/O, and the detached turn outcome writer allows ten seconds for a busy pool before logging a failed write.

- **Tool safety keeps asking after a thread's first turn.** On the next turn,
  a session created with tool safety on was compared with the agent's own
  `always_allow` tools, so it read as changed and the in-place update wrote
  `always_allow` back onto it: third-party writes stopped showing a
  confirmation card after the first turn. The bind and the update now gate the
  tools the way session create does, so an unchanged session is left alone and
  a real tools change keeps `always_ask`. A session that already lost its
  cards gets them back on its next turn.
- **Teams posts nothing but its answer.** A file it could not read, an oversize output and a declined file offer each sent a status message of its own under the answer. The agent is now told what it could not open and why, and the rest is only logged, as on Slack and Discord. Cards and file offers still post.
- **Teams channel files without Graph Explorer.** When a channel file is refused, a daimon admin gets an Enable files card: one sign-in by a SharePoint or global admin grants the bot that team's site. It needs `DAIMON_TEAMS__PUBLIC_URL` and a redirect URI on the app (see `docs/teams.md`).
- **The MCP endpoint is stateless.** It kept MCP sessions in one process's
  memory, so after a redeploy, or on a deployment running the MCP server on
  more than one instance, a client's tool calls failed with "server terminated
  the MCP session" (HTTP 400) or "Session not found" (HTTP 404) for the rest of
  its session. Every request now stands alone, needs no `initialize` first and
  answers with JSON, as the hub endpoints already did; an unknown session id
  is ignored. Identity and tool visibility were already worked out per request
  from the token, so no tool changes.
- A Teams channel message the bot ignores because it was not mentioned is now
  logged as `teams.message.ignored`, and a refusal reply that fails to send as
  `teams.refusal.send_failed`, each with the conversation type and reason and
  never the message text. Both cases used to leave no trace.
- The Slack bot no longer answers thread replies that don't mention it. Slack
  can deliver an `app_mention` event for a reply in a thread the bot is in even
  when the reply never mentions it, and each one ran a billed turn. A turn now
  runs only when the message contains `@daimon`, as on Discord, so follow-ups
  in a thread need the mention every time. Dropped events are logged as
  `slack.event_dropped.no_explicit_mention`.
- Slack answers that put a bare link in bold or italics (`**https://…**`) no
  longer render as a dead link ending in `*`. The link is posted as an explicit
  Markdown link, with any trailing full stop or question mark left outside it.
  Code, existing links and table cells are left as written.
- The checkout landing pages no longer tell every payer to return to Discord.
- Continuity notices (workspace loss, fresh start, a failed preparation, a new
  responder, a timer that did not run) say "here" and "ask again" instead of
  "this thread" and "mention me", so they read right in a chat without threads.
- **Sonnet 5 and Opus 4.7 are metered at list price.** Sonnet 5 was charged at
  $3/$15 per million tokens, the rise Anthropic announced and then withdrew;
  its standard price stayed $2/$10. Opus 4.7 was charged Opus 4.1's $15/$75
  instead of $5/$25. Every agent-model row in the pricing table is now the
  provider's list price. Deployments that want a margin set `DAIMON_BILLING__MARKUP`, which
  applies the same multiplier to every model and leaves the cost reports
  showing provider cost.

- Stop retrying Anthropic's monthly spend-cap response and show a clear model usage limit notice in Discord and Slack.

- Avoid repeated Managed Agents event reads for unchanged sessions during the usage sweep, with hourly full passes.

- The MCP server's hub login store opens one database connection at startup and
  grows to four, instead of holding ten per instance. During a deploy the old and
  new revisions no longer exhaust a small Cloud SQL tier's connection slots.

- Fresh Discord installs and boot reconciles share a bounded seed queue. Skills API
  calls are paced across the adapter process and retry temporary rate limits;
  defaults reconciliation lists workspace skills once per tenant instead of once
  per skill.

- Follow Anthropic Skills API cursors across multiple pages. A full final page
  without a cursor still fails closed before skill writes or deletes.

- Direct-message policies normalize tenant UUID keys and reject invalid keys at
  settings load, so restrictive policies cannot silently miss their tenant.

- Ordinary chat sessions carry their executing agent identity to the Google token broker,
  including existing vaults on the next session creation, while preserving chat tool
  visibility and caller isolation.
- Slack direct-message errors explain the required `im:write` scope and workspace
  admin reinstall for missing scopes or invalid authorization, preserving the
  count of messages already delivered.

- A continuation turn (the follow-up after a private form or a task handoff)
  on Discord or Slack now runs with the requester's live role, re-read from
  the guild or workspace at dispatch, instead of always as a plain user. An
  admin's setup run no longer loses its admin tools on the turn that applies
  their answer; a non-admin's form, or a failed role lookup, still runs as a
  user.

- Bound GitHub skill resyncs now preserve the binding's exact Managed Agents
  identity through ledger updates and skill attachment. Duplicate active agent
  names already present at bridge resolution refuse the resync before
  credential selection or repository fetch. Duplicates observed later refuse
  before MA or ledger writes, including orphan deletion; errors remain visible
  as permanent binding failures.
- Rate-limited GitHub tarball downloads now remain retryable, and durable push
  resync waits for GitHub's `retry-after` or reset deadline before claiming the
  job again. Other 403 permission failures remain permanent.
- Discord setup Details now drops superseded reads and binds setup and
  coding-tool actions to the agent shown on the card. A bounded TLA+ model
  retains the pre-fix wrong-target traces.
- Discord guild removal and rejoin transitions are serialized per guild, and
  delayed lifecycle work checks the current gateway cache before changing the
  tenant. Startup now revives archived tenants for guilds still joined without
  posting another welcome or issuing signup credit again.
- Slack shutdown now waits for acknowledged mention handlers that are still
  preparing a turn, closing the graceful-drain loss window before thread
  registration. A hard process crash after acknowledgement can still lose work.
- A Cancel click on a newly posted Slack status card is routed while Slack's
  `chat.postMessage` response is still pending; after the response, routing
  follows the message timestamp so recovery can rebind the active turn.
- A malformed GitHub installation creation payload with a present non-array
  `repositories` value no longer clears the existing repository cache.
- A malformed tenant tag in one Managed Agents session no longer aborts the
  usage sweep; malformed optional account metadata drops member attribution
  while preserving tenant usage and its debit.
- The usage sweep no longer attributes a session to a platform user from a
  different tenant when its account metadata points across tenant boundaries.

- The scheduler's usage sweep no longer debits a tenant for `BillingExempt`
  usage: MCP turns started by a caller with no platform user (an operator,
  CLI or internal token) and headless runs with no recorder. Such sessions are
  now stamped `daimon_billing_exempt` when created, the sweep skips them, and
  the operator absorbs their cost. The sweep logs each skipped session's
  would-be cost as `usage_sweep.exempt_skipped` and totals it per pass in
  `usage_sweep.completed`. See `docs/billing.md`.
- A session recovery that is rolled back (by the turn time limit or a
  cancel) after creating its replacement session now archives that session
  instead of leaving it running upstream with nothing pointing at it. The
  archive wait is bounded, its eventual result is logged, and repeated
  cancellation does not replace the error that caused the rollback.
- A failed report publish keeps the reader's current PDF paired with its
  accepted bundle. Each upload archive is stored separately, so a failed push
  cannot replace the archive used to recover that bundle. A later publish
  prunes unreferenced archives after at least 24 hours and the configured turn
  deadline.
- Concurrent GitHub App repository add/remove updates no longer overwrite
  changes from another delivery.
- GitHub App suspension, unsuspension, and permission-change events no longer
  clear the cached repository list. Only installation creation replaces it.
- A pending report publish keeps its archive while another publish prunes old
  uploads, so a delayed seam response cannot commit a missing archive reference.
- A session loss recovered right at a turn's time limit no longer leaves a
  replacement session behind that the next mention silently continues on
  without being told the previous work was lost.

- Slack now keeps a substantive answer when the agent uses a tool after writing
  it, including turns that end without a further reply.
- When two turns in one thread (for example a Discord form submit and a
  mention) both find the thread's session gone, they now continue on one
  replacement session. Previously each created its own, leaving the thread
  with two active sessions and one turn's work in a session later messages
  never reached.
- A Discord mention sent into a thread while its turn is finishing is no
  longer left unanswered until the next mention, and a failed ⌛ reaction
  (for example, a missing Add Reactions permission) no longer drops the
  queued mention.
- Finishing an MCP OAuth sign-in no longer fails with "Sign-in did not
  complete" when one of your turns re-copies the agent's shared token for the
  same server at that moment; the sign-in replaces it and is kept. That turn
  also no longer fails when the sign-in replaces the token it was updating.
- That sign-in is now kept however many of your turns with the same agent are
  running while it finishes. With three or more of them it could still fail
  with "Sign-in did not complete".
- Reinstalling daimon in a Slack workspace that had uninstalled it leaves the
  workspace live again. The uninstall's archive stamp used to survive the
  reinstall, so the hub and the boot defaults sweep kept treating the workspace
  as gone; and an uninstall event delivered late, after the reinstall, no
  longer deletes the fresh bot token.
- A private form submitted in Slack or Discord just as the thread's turn was
  finishing now resumes the task that was waiting on it, instead of waiting
  for the next message in the thread.
- A Slack or Discord mention sent into a thread while a task resumed by a
  private form is running there now gets its own reply once that task
  finishes, even when resuming the task fails (Slack posts an apology in that
  case). It used to keep its ⌛ reaction and wait for the next mention in the
  thread.
- After a restart cuts off a turn in Slack or Discord, the next message in that
  thread gets its own reply instead of the cut-off turn's answer: the boot
  sweep that marks the turn as interrupted now also stops it on Managed
  Agents, so it no longer keeps running and billing after its card says it
  was interrupted.
- A Discord mention answered in the first seconds after a restart, before
  the bot has finished connecting to every server, is no longer mistaken for
  a turn the restart cut off: its card is no longer marked as interrupted
  while it is still running.
- Reconnect replay keeps the current turn's answer and rendered content when
  the event history is incomplete or ends with a session termination; repeated
  SSE events no longer repeat adapter callbacks.
- Stripe Checkout credits once per payment intent. Concurrent refunds and
  disputes cannot claw back more than the original credit, and a refund that
  arrives before Checkout completion is applied when the credit appears.
- Discord and Slack orphan recovery no longer clears a newer active turn;
  Slack retries a failed startup sweep before admitting turns.
- A stale wizard submit cannot replace newer answers, and expiry cannot
  abandon an already submitted session.
- A per-agent MCP token (coding tools) now reaches only the sessions its own
  account started with that agent: `list_my_sessions`, `get_my_session`,
  `list_events`, `continue_turn`, `ask`, `cancel_turn`, `archive_my_session`,
  `get_turn_cost` and `deliver_turn_charts` no longer see other workspace
  members' sessions of the same agent.
- A model call made while a turn's event stream was disconnected or stalled
  is metered by that turn when it replays the session history, under the
  turn's own member and ledger reason, instead of waiting for the usage sweep
  (or going unmetered where no scheduler runs).

### Added

- Opt-in recruiter-reviewed Discord staffing threads with a tenant-scoped encrypted ledger,
  revision-bound confirmation, durable recruitment API outbox, candidate privacy guards,
  configurable retention and a synthetic QA backend.

- Built-in sandbox tools (bash, file reads and edits and the rest) can be turned back on for one channel or thread routine at a time, for that routine's own scheduled runs. The supported way today is the operator's `daimon routines sandbox-tools --tenant TENANT ROUTINE_ID... [--off] [--dry-run]`, which also opts in a confirmed list of routines at once and leaves any already set alone. An opt-in covers exactly the routine's current agent version and environment: a run gets built-ins only when its agent version and environment are the ones recorded, so after anyone edits the agent or its environment the next run goes without them and the opt-in is cleared until the operator approves the routine again. The `set_routine_sandbox_tools` MCP tool accepts only a live Discord server admin's own token, which no Daimon surface issues yet; it is kept for a future admin surface and refuses agents, operator tokens, legacy tokens and Slack or Teams callers. DM routines cannot be opted in, and editing a routine's agent, trigger message, destination or budget channel turns the opt-in off. Every change records who made it, when, the routine id and the agent version and environment it covers. `get_routine`, `list_routines` and the Discord `/routines` panel show whether a routine has sandbox tools. Watches, timers and wakes, agent chat, fleet workers, `daimon run` and workspace checkpoints keep them off with no opt-in.

- Native tenant-scoped lead intake with encrypted capture, Gmail qualification,
  restricted Discord dossiers and draft replies, conservative HubSpot reuse,
  durable write recovery, authorized human resolution and retention tombstones.
  CRM recovery refuses conflicts with approved selections and requires another review.
  Dossier effects/readback honor thread permissions; classification can read new
  discoveries without service credentials. Nested attachments are excluded,
  discoveries expire, source tombstones survive connection renames, and actor
  erasure prevents racing resolution audits from recreating personal data.
  Intake authority is bound to one attended session grant; routine provenance
  remains isolated through refresh and resolution is protected unattended.
  A lead's readers are fixed to the sales channel it was captured for: every
  reader, including `/leads` and approval-card Details, needs that channel's and
  its dossier thread's history, and an agent kept to its own channel reads no
  lead elsewhere. Resolve cards keep the approved IDs visible; adoption refuses
  a channel change; discoveries survive a mailbox connection rename.

- Turns nobody watches (routines, timers and worker wakes, fleet workers, agent chat, `daimon run` and workspace checkpoints) refuse a session whose agent coordinates other agents, before anything is sent: its roster's agents could run tools and MCP writes the unattended gate cannot reach, and Managed Agents cannot remove the roster from a live session. Such a routine fails with `AdmissionDenied: backend_unsupported`, agent chat answers with a tool error, and a checkpoint carries only the conversation. A session carrying a field this release has not reviewed, at any level from the session and its resources down to a tool's permission policy, is refused the same way (including a field hidden inside a setting that should be a plain value), as is an unattended turn on a Managed Agents runtime that cannot read its session. Every MCP call in these sessions now pauses for the unattended decider, Daimon's own tools and calls with tool safety off included; the decider allows what it allowed before. Built-in sandbox tools (bash, file edits and the rest) and custom tools are disabled for every unattended entry point, including watches, with no opt-in. Routines and other unattended runs cannot generate files with sandbox tools, and workspace checkpoints cannot create sandbox archives.

- A conversation can run on a model of its own: `/model` in a Discord thread shows it or switches it to Haiku, Sonnet or Opus (or back to the agent's default), and asking in words ("use opus", "think harder") switches it on any platform. The thread keeps the choice for every later reply, across restarts; other conversations and the stored agent are unchanged. Anyone who may start a turn there may change it; routines and other unattended runs can't. A choice that can no longer run falls back to the agent's own model, and the reply says so. Worker fleets ignore the choice: a fleet root and its workers always run the model the agent's owner approved, and `/model` says so on such a thread.
- Prevent watch feedback loops with bounded human-only poll windows and canonical
  source IDs. Verify owner enrollment cards inside create/resume/key rotation;
  unsupported backends refuse. Document exact isolated preview enable commands.

- Require attended owner approval to enroll, resume or rotate watches; prevent
  polling the reporting thread, honor the agent's live read toolset, and isolate
  polling on an authenticated private executor. Scheduling races no longer appear
  as owner access revocation.

- Native OpenAI sessions install isolated MCP session grants before creation, bind them to each executing origin on reuse, and revoke origin authority when the turn ends. Child preparation cannot inherit a parent origin grant.

- Agent chat and hub mutations require a session opened on that headless surface. Same-account platform sessions and legacy sessions without proven headless provenance cannot be continued, cancelled or archived through it. Unauthorized callers are refused before waiting on a session mutation fence.
- Preserve the deployed Microsoft accounts migration ID and automatically reconcile earlier preview migration stamps during upgrades. Keep diagnostics read-only on empty and legacy databases, preserve normal explicit Alembic bookkeeping, and explain deployment serialization and legacy preview upgrades.

- Add default-off OS-signed page-event watches with atomic evidence and durable wakes,
  restricted database identities, replay/retention limits and an optional preview
  sidecar. Account erasure removes owned watches, evidence and queued wakes. Woken
  turns require an execution-bound unattended session grant, explicitly allowlisted
  snapshot fields and named read tools; skills, multiagent coordinators and unknown
  capabilities refuse before provider mutation. Original tools restore after each
  turn, including cancellation. Fresh access and spend checks gate every send and
  output write; a failed check latches delivery off through cleanup. Startup audits
  resolve the real ingress schema and refuse unsafe effective database privileges.
  Expired batches never absorb fresh events.
- Add deterministic polling of reviewed OS read tools, bounded projections, saved
  baselines, separate poll leases and error backoff. Owner MCP tools manage watches
  and deliver page signing keys privately once. Offline imports remain paused until
  their exact owner and destination are confirmed; unmapped page-data polls stay
  paused and queue a private owner notice.

- Scheduled routine results attach generated files on Discord (including owner DMs) and Slack. Upload limits and unsupported Teams file delivery are explained in the result; the outbox does not replay interrupted deliveries across polls. A platform SDK may retry a single send after a server error or connection reset. Fallback-DM files recheck DM controls, recipient eligibility and isolation before upload; delivery failure logs omit sensitive exception text.

- Discord routines can deliver privately to their owner through MCP, the routines panel, or `daimon routines create --destination dm`. Delivery rechecks live access, respects tenant DM controls, and exposes failures without public fallback or outbox replay across polls. Platform SDKs may retry a single send after a server error or connection reset. DM routines run as scheduled owner messages through the normal live-DM admission, binding and runner, with the owner’s normal memory and credentials in a fresh private scope. Only explicit DM routines mount personal memory; all headless routines, including legacy destination-free rows and routines switched to No automatic delivery, use shared memory only. They are unattended and Managed Agents only; calls needing a person are refused. Nobody watches intermediate actions: permitted tools retain ordinary DM side-effect risk. Crash and stop guarantees are exactly those of live DMs. A slot is skipped if the owner has a live DM turn running. Agent or destination edits invalidate private output and pending delivery atomically; concurrent edits, pauses and resumes reject stale revisions and require a reload. Pause/resume revisions strictly advance and cannot overwrite a newer cron/timezone schedule. Creation-time source channels, threads and provenance status are immutable across retargeting, clearing and DM conversion. Legacy unknown-source routines retain ordinary edits and firing, but must be recreated from their source to get DM delivery. Routines created from a private conversation verify its saved guild source; cold conversations remain source-free, and unknown provenance refuses DM delivery. CLI source IDs are verified against the selected tenant's guild, including in dry-run.
- Private Discord replies recheck the answering agent's current pin and isolation rules before delivery. Unavailable bound agents and unreadable private policies are silent, and superseded server-switch controls are retired.

- Discord can answer a first DM using an explicitly configured tenant agent, with a private server chooser when several servers qualify. Cold starts copy no channel history. Ineligible senders and revoked private access receive no reply; server switches reset the private scope.


- Add per-tenant `invoker_admin_bypass` and CLI flags to require server admins to match invoker user/role lists.

- Gate Discord turns and routine owners by configured role IDs, with current membership checks for MCP, scheduled fires, and private delivery. Configure roles with `daimon tenants access-policy set --invoker-role`.

- A standalone Azure OpenAI harness runs local agent tools, MCP connections and skills, with durable event replay, approval decisions and cancellation. It supports direct HTTP and Foundry invocation envelopes; deployment details are in `apps/harness/README.md`.

- Tenant-owned self-hosted agent and environment records have Postgres-backed resource ports, scoped lookups and revision checks. Credential fields reject embedded secrets while instructions and descriptions remain free text. Principal-owned retry receipts retain only resource references and revisions for seven days; account erasure serializes with writes and removes personal receipts.
- `daimon selfhosted init` creates or reuses tenant-owned agent/environment records without provider credentials. Invalid database or harness configuration returns a generic error without echoing credentials. Selfhosted channel admission resolves these records locally without calling Anthropic when explicitly enabled and admitted by the profile capability gate; it stays closed by default.

- **Teams people can connect their own Microsoft 365.** With
  `DAIMON_MICROSOFT_ACCOUNTS__*` set, `connect_microsoft` sends a private
  sign-in card, and `microsoft_search`, `microsoft_get`, `microsoft_write`
  and `microsoft_send` then work on their files, Excel workbooks, mail,
  calendar and OneNote as them. Mail, calendar and notes stay in their 1:1
  chat, every email or invite waits for their Approve (so does every write
  with tool safety on), and `privacy` → Disconnect Microsoft removes the
  saved account. Teams sessions of the default agent add a `microsoft-365`
  skill, leaving room for 5 channel skills instead of 6. Unset, and on Slack
  and Discord, sessions are unchanged.
  See [Teams](docs/teams.md#microsoft-account-optional).
- **Agents edit Office files and update Teams channel files in place.** On
  Teams, the default agent's sessions add Anthropic's built-in `docx`, `pptx`
  and `xlsx` skills; other backends run without them, and Slack and Discord
  are unchanged. They count toward the 20-skill session limit, so a Teams
  channel answered by the default agent now adds at most 6 skills of its own,
  down from 9; adding a seventh is refused, and extras added earlier are
  left out of its turns, logged once rather than on every turn. Live Teams
  sessions of the default agent are replaced once after upgrading, since
  their skills change.
  On Teams, the new `replace_channel_file` tool saves an edited file over one
  in the channel's Files folder as a new version, so "update the deck" no
  longer leaves `deck 1.pptx` behind. It writes only at the version the agent
  read and only in the channel the turn runs in, and says whether a failed
  write may have saved. Shared and replayed channel files carry the link and
  version it takes. No new permission is needed.

- **MCP sign-in works with authorization servers that offer no dynamic
  client registration**, such as Microsoft Entra ID. List clients registered
  by hand in `DAIMON_MCP_OAUTH__CLIENTS`; each names its authorization server
  host and the MCP servers it may be used for, and a sign-in uses it only
  when both match. An entry can override the authorization and token
  endpoints, set the scope to request and leave the RFC 8707 `resource`
  parameter out, and it asks for `offline_access` when the authorization
  server offers it so the connection keeps refreshing. Every other server
  still registers a client dynamically, as before. See docs/self-hosting.md.

- **Self-host on OpenAI, with no Anthropic account at all.** `DAIMON_ANTHROPIC__API_KEY`
  is now optional; `docker compose up` and `daimon defaults apply` succeed
  without it. `daimon openai init` creates (or reuses) a project-scoped
  OpenAI agent and hosted environment, seeds it with the same default skill
  set an Anthropic deployment gets, and writes one row — optionally a
  tenant-wide `"*"` default — into the provider runtime file. `daimon
  agents backend set <agent> openai` routes that agent's channels to it, and
  `daimon openai smoke` is a post-deploy check: one chat turn, one tool
  call, one skill, pass/fail. See docs/self-hosting.md's "Self-host on
  OpenAI" for what's different on OpenAI (no multi-agent delegation, no
  mid-session resource swap, no workspace export/import, no cross-turn
  memory, no Daimon model knobs).

- **Teams now answers in group chats.** Each group chat is one session: the
  bot answers an @mention, a reply to it or a command, replays the chat's
  history and images through Graph, and answers commands privately. It answers
  only while everyone in the chat is from the organisation, checked again
  before an answer, approval card or question card posts; otherwise the sender
  is told privately why not, and a turn under way stops without showing its
  work. Files are not shared there, beyond image previews, and routines and
  agent tools do not reach group chats yet. Rebuild the app package (version
  0.6.0) for the `groupChat` scope and the `ChatMessage.Read.Chat` permission.
- **Teams answers render math.** Answers, their follow-up chunks, MCP
  `send_message` posts and new threads, and `tidy` text edits go out as Teams
  Extended Markdown, so `$…$` and `$$…$$` show as formulas. A `$` that opens
  or closes no math span by pandoc's dollar rules, such as a price in "$5 and
  $10", is escaped to stay literal; code spans, fences (closed only by a bare
  fence line) and `<https://…>` autolinks are left untouched. A message that
  @mentions someone, such as a completion ping, stays plain markdown, so its
  ping is unchanged and its math shows as typed. Long answers still split at
  4,000 characters.
- **Teams agents can show a picture of a document they made.** An output named
  `<file>.preview.png` (for example `report.pdf.preview.png`) shows inline,
  under the file's name, and is never offered or uploaded; delivery deletes it
  afterwards. It is matched to its file by the saved name, so spaces and
  non-ASCII letters work. In a channel it takes the place of Graph's thumbnail
  of that file; in a 1:1 chat it shows just before the file's consent card,
  and the turn context tells the agent it may make one when it can render its
  first page cheaply, from a PDF with PyMuPDF; no office renderer is
  installed by default.
- **Teams activity feed notices.** A routine result posted in a channel now
  notifies its creator in their Teams activity feed, linking to the post, with
  one feed entry per routine that each run replaces. An approval or question
  card in a channel thread notifies the person who must answer it. A private
  or shared channel's team comes from the message or the channel's stored
  Files site or subscription; one not found is not looked up again for ten
  minutes. 1:1 chats already notify and get none. Notices run in the
  background through Graph with a five-second limit; a failure is logged once
  and never delays or fails the post. The manifest adds the
  `TeamsActivity.Send.Group` consent, so an existing install needs the rebuilt
  package (version 0.6.0) and a team owner to accept it.
- **Teams now reacts on the asker's message, as Discord and Slack do.** Teams bots
  could not react, so a message queued behind a running turn showed nothing
  until its turn began, and a completion ping was an @mention alone. A message
  waiting behind the turn running in its chat or thread now gets ⌛, removed
  once its turn starts, if its sender may start a turn there. With completion pings on, the asker's message gets 👀
  once the turn is accepted and ✅ in its place once it is answered. Reactions
  go through Bot Framework in the background, spaced to its limit of two a
  second per bot, so a reaction never holds up or fails a turn. An add that
  would wait over five seconds, or fails, is dropped; a removal always runs.
  No new setting or manifest change.
- **Teams commands in a channel now answer privately in the thread.** `help`,
  `here`, `memory`, `routines`, `billing` and `privacy` typed in a channel
  answer there as a message only the sender sees, and their panels stay
  private; Teams expires one after 24 hours, which closes its panel. `setup`
  and `support` still answer in the 1:1 chat. Refusals and pointers meant for
  one person go to them alone too, and fall back to the old public post or
  1:1 answer where Teams will not deliver one. A private message sent to
  Daimon in a thread runs a command, or gets a line saying private questions
  are not supported yet.
- **Teams gains `/` commands and prompt starters.** Typing `/` in a channel
  lists the in-thread commands, and a command may start with `/`. The
  personal chat and channel @mentions offer ready-made requests: explore a
  dataset, build a notebook, set up the workspace; summarise the thread,
  analyse its data, write it up. Manifest 1.30, template version 0.6.0:
  rebuild and upload the package to get them.
- Explicit Gemini agent deployments can retain full default skill bundles through scoped external deployment receipts and caller-bound remote MCP, with fresh source-byte verification and native readback before execution.
- `daimon channels answerable discord GUILD_ID` lists the Discord channels where Daimon may answer at all: every channel the turn gate admits, that is one not under a `writers: none` channel or category rule, with each bound thread checked the same way. With `--expect FILE`, a list of the channels another bot sharing the server does not answer, it checks that Daimon's set equals that list, prints the ids missing on each side and any answering bound thread under an unlisted channel, and exits 1 on any difference. Read-only, and it fails closed: a channel it cannot place is answerable or an error, never silent.
- **Fleet roots now run against one retained original reservation.** A fleet root is admitted once against a scoped hold that it keeps for its whole run: pending financial state is settled before any funding check, each original input the root sends is counted against that hold, and a native root carries its explicit budget into the session. A root is an ordinary agent session, so it keeps the agent's personal memory store, which is neither counted nor funded. Off by default: fleet roots run only with `DAIMON_FLEET__ENABLED=true`. No migration.
- Explicitly authorized OpenAI fleet workers can run without a workspace, tools, MCP, memory or skills. Their initial task is counted and sent with session creation; retries reuse the acknowledged input and uncertain delivery requires reconciliation.

- Approval cards for classified third-party MCP writes now offer **Always allow** when agent tool rules are enabled. Workspace admins save a chat-only rule for the agent; channel admins save one for the parent channel. The click approves the pending call, and a concurrent rule edit leaves it approved for this time only. Discord, Slack and Teams show who answered and whether the rule was saved.

- Workspace admins can set, list and clear per-agent rules for individual third-party MCP tools from `daimon agents tool-rules` or MCP. Channel admins can manage rules for any agent in their channels. A channel rule also applies to threads under its parent channel; where it overlaps an agent-wide rule, the stricter outcome wins. Rules can allow, ask or deny in chat, with an explicit option to allow unattended routines. They are bound to the agent id and server URL, audited with revisions, and inactive by default behind `DAIMON_TOOL_SAFETY__AGENT_RULES`.

- **Followed threads, Replies to the bot and typed "@Daimon" now work in Teams private and shared channels.** Teams sends a bot only the posts in those channels that @mention it, so a followed thread got no unprompted replies, a Reply quoting the bot was ignored and a typed name was never seen. An operator can now set `DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED=true` (off by default; needs `DAIMON_TEAMS__PUBLIC_URL` and the Graph application permission `ChannelMessage.Read.All` with admin consent). Daimon then subscribes to Microsoft Graph change notifications for each private or shared channel it is in, reads each new message once and handles it exactly as a Bot Framework message, usually within seconds. Subscriptions are renewed before they expire and deleted when the bot is removed, or found gone at a renewal; missed notifications are caught up with one read, as each boot reads every channel once, at most 10 minutes back, and an older notified message is skipped; a Reply quoting the bot or a typed name read during a shutdown runs on the next instance, while other replies read then are dropped, as on Bot Framework. Edits never start a turn, and a message seen through both routes, or read again after a restart, runs once: both write the same database receipt. The reverse proxy must also pass `/api/graph/notifications` and `/api/graph/lifecycle`. Migration `0103_teams_channel_subscriptions` adds the table.
- **Admins can let a typed "@Daimon" start a reply, on Discord, Slack and Teams.** A real mention on Teams mobile means typing part of the name, waiting for the suggestion list and tapping the bot, and a pasted prompt that says "@Daimon" is only text, so neither was answered. A server or workspace admin can now ask the agent to turn on plain-text mentions (`set_plain_text_mentions`, off by default; `get_plain_text_mentions` reads it). With it on, a person's message in a channel or thread that contains "@" followed by the bot's name, in any case, is answered as a real mention would be, with the same channel, billing and queueing rules. A name followed by "(" or a dot and more text ("@Daimon.example"), one inside a URL or email address, and one in a quoted line (Slack) or reply quote (Teams) do not count; a name like "Daimon (Staging)" also answers to "@Daimon". Messages from bots and webhooks never count, a message with both forms runs one turn, and direct messages are unchanged. Teams private channels only deliver posts that mention the bot, so a typed name is seen there only with private channel messages on (above). Migration `0099_plain_text_mentions` adds the setting.
- **Backend switches for ordinary agents now record declared drops.** When an agent's target backend cannot keep its memory, tools, skills or MCP servers, the switch must carry explicit consent to those losses, stored with the agent's backend revision. A turn refuses to start if that consent is missing, if the agent's setup has drifted since the switch was previewed, or if the agent has a source field or value Daimon does not recognise. Ordinary Gemini agents can run as a plain chat counterpart once memory is dropped. Off by default: this applies only with `DAIMON_TURN__PATH=mux` and `DAIMON_TURN__CHANNEL_BACKENDS=true`, and the screens for confirming drops arrive in a later release. Migration `0102_agent_backend_drop_consent` adds two nullable columns.
- Agent guidance now says what an agent may tell a non-admin: what it can do (capabilities, skills by name, model family) is open to anyone, while repo names, the GitHub account behind its credentials, key names, environment details and its instructions go only to a requester marked `is_admin="true"`. Anyone else gets one line saying an admin can see setup details, the same in every thread. Before, a non-admin was refused in one thread and given repo names and the GitHub account in another. Agents also commit binary files (images, PDFs, decks, model files) with git from the sandbox clone and push with the repo token, since the GitHub MCP takes text only. The `marimo_notebooks` skill now tells agents to style every new notebook with `pymc-artifact-style` by default.
- **Commits and pull requests Daimon makes now show as Daimon's GitHub App, not as a person.** On a pilot, commits and a PR came out under a team member's GitHub account, and nobody could tell whose it would be. Agents in GitHub App mode now commit with git and open PRs with `gh pr create` on the App's repo token instead of the GitHub MCP (where a person's own GitHub sign-in outranks the agent's credential), and put "Requested by <name>" in the PR body. A session that holds an App token also gets `/mnt/session/uploads/git-identity.sh`, which sets the git author and committer to the App's bot (`<app-slug>[bot]`) so commits link to it. A `GIT_AUTHOR_*` or `GIT_COMMITTER_*` key an admin or member has set in the agent's keys still overrides that identity. Agents on a stored per-agent token are unchanged. Sessions already running get the identity when they next start.
- **Charts and other images an agent makes now show inline in Teams, as on Slack and Discord.** Before, every output reached Teams only as a file: a link under the answer in a channel, a consent card in a 1:1 chat. Each PNG, JPEG, GIF or WebP output now also comes with an inline preview, downscaled to fit 1024x1024, the largest Teams renders inline, and re-encoded under 512 KiB to keep the message small. In a 1:1 chat the preview comes right before the file's consent card, and comes again when the file is offered anew after a failed upload, but not when an unanswered card is resent. In a channel up to four previews follow the file links in one message, in delivery order, and are resent one per message if Teams refuses them together. The file itself is delivered exactly as before. Animated GIFs, images that cannot fit or are over 40 million pixels (a JPEG counted at the reduced size it decodes at), and unprompted answers get no preview, and a preview Teams refuses is only logged. A channel without file access gets the previews too, though the images, like its other outputs, are not saved.
- **Word, PowerPoint, Excel and PDF outputs now preview their first page in
  Teams channels.** Before, a document saved to a channel's files showed only
  as a link. Once uploaded, its first page, as Graph renders it, now follows
  the image previews in a message of its own, up to four in delivery order;
  images never wait for it or lose a place to it. Graph often renders it
  seconds after the upload, so a missing thumbnail is asked for up to three
  times, ten seconds at most in all; one that does not come is logged and left
  out. The thumbnail is fetched without a token, only
  from SharePoint or Microsoft's media service. 1:1 chats are unchanged.
- **GitHub setup now works from Teams.** Before, an agent asked to connect GitHub on Teams said "GitHub setup isn't in Teams yet" and sent people to Discord or Slack. Now an admin, or a channel admin who manages the agent, gets the Connect GitHub card in their 1:1 chat with Daimon, since Teams has no message only one person sees and the link is single-use. In the channel the agent only says the link went to that chat; if the chat cannot be opened, the link is withdrawn and the agent says so. Work that waited on the repo resumes once it is connected. Choosing an agent's working repo and removing a repo also work from Teams, the GitHub App install link is posted as a link, and the Connect GitHub pages no longer tell Teams users to run a Slack command. Access requests to admins are not on Teams yet.
- Provider-neutral mux library, optional provider drivers and offline conformance tests. The host runtime remains unchanged; the library does not activate backend selection. Shared test helpers retain the current skill and conditional memory-delete routes.

- **Members can redeem a promo code into their own channel's budget (#791).** `daimon promo create --amount 10 --channel-cap 30` makes a channel code. Any member can redeem it from `/billing` on Discord or Slack, or `billing` typed in a Teams channel. It adds $10 to the budget of the channel they are in (a thread counts as its parent), once per person per channel, until that channel has taken $30 from the code. `--max-redemptions` still limits the total. The credit is a `promo_credit` ledger entry tagged with the channel, and it raises the channel's budget limit by the same amount. In a channel with no budget, the credit goes to the server's balance. Admins still redeem ordinary codes for the whole server. A member who tries one is told only an admin can, and a channel code entered in a DM, a Slack group DM or a Teams 1:1 chat is refused. **Redeem code** now appears for members while a channel code can be redeemed, and for admins while any code can. Each attempt writes a security audit row naming the channel, including an empty code or a failed redemption on Teams. A Discord reply that fails after a redemption is no longer recorded as a failed redemption. `daimon promo list` shows `channel_cap_usd` and `daimon promo redemptions` shows `channel_id`. The Discord ready message now reads "Have a promo code? Redeem it in `/billing`."
- **The promo refusal throttle now counts per person.** Five refused attempts in 15 minutes pause only that person's redemptions. Before, they paused everyone's on the server, admins included.
- Migration `0088_channel_promo_codes` only adds: the `promo_channel_redemptions` table and two nullable columns. Workers of the previous release keep redeeming server-wide codes during the rollout and after an application-only rollback. Create channel codes only once every worker runs this release, because older workers would credit one server-wide.
- **Each person gets their own memory with an agent.** An agent now keeps your preferences, and anything it learns about you, in a personal memory that only your own conversations with it can read, in channels and DMs alike. Its shared memory, which everyone who talks to the agent reads, is kept for team knowledge such as conventions and project facts, and the agent is told never to copy anything from your personal memory into it. Another person's conversation never has your personal memory mounted, so it cannot be read there whatever the agent is asked. Your personal memory starts with your next new conversation; conversations already open carry on as they are, and nothing already in the shared memory is moved. Routines use the shared memory only. Deleting your account deletes your personal memory at Anthropic.
- **Each person gets their own memory with an agent.** An agent now keeps your preferences, and anything it learns about you, in a personal memory that only your own conversations with it can read, in channels and DMs alike. Its shared memory, which everyone who talks to the agent reads, is kept for team knowledge such as conventions and project facts, and the agent is told never to copy anything from your personal memory into it. Another person's conversation never has your personal memory mounted, so it cannot be read there whatever the agent is asked. Your personal memory starts with your next new conversation; conversations already open carry on as they are, and nothing already in the shared memory is moved. A routine that posts nowhere but to you (one made in a DM, with no channel to post to) can also read your personal memory, without changing it; any routine that posts in a channel uses the shared memory only. Asking an agent through the hub uses your personal memory too; agent keys and operator tools don't. `/memory mine` (`memory mine` on Teams) shows your own personal memory and `/memory forget-mine` clears it after you confirm with the code it shows you; nobody can see or clear anyone else's. Deleting your account deletes your personal memory at Anthropic.

- **`daimon memory personal-report`** shows operators, per agent, how many entries in its shared memory look personal, with a few redacted examples. It only reads: nothing is moved or deleted.
- `daimon usage settle-backlog --internal UUID` settles the model calls agent-chat sessions made before this release with no ledger row: debited for the internal tenants given, waived (marked settled, nothing charged) for every other tenant. It is a dry run unless `--apply` is given, and prints a receipt (one JSON object with `--json`): the cutoff, the mode and, per tenant, the sessions, calls and the amount debited or waived.

- Agents can remove a repo from their own GitHub access in a setup conversation after the same requester confirms in a later turn. Removing a working repo clears that selection without changing other agents.

- Account deletion on Discord, Slack, and Teams privacy panels can be paused during an event with `DAIMON_PRIVACY__DELETE_ENABLED=false`; Policy and Export remain available.
- **QA CLI readback:** Check staging QA agent configuration through a bounded operator hook, with required and forbidden text assertions and no shell execution.
- **QA sessions:** Serialize timer and manual live runs with a shared lock; accept configured hook context and templated catalog fixture arguments.

- **QA observations:** Support channel text checks and bounded unauthenticated HTTP probes; record agent header evidence per turn.

- **More Discord assertions run in live QA.** Message counts, per-message balanced fences, cost-footer placement, thread-name readback, and filename-filtered attachment counts are supported.

- Agent setup can choose one working GitHub repo by name, or `none`, in conversation. Other granted repos remain available through tokens for cloning by name. A confirmed request can give an agent read access to an already connected repo, with write access only when requested; otherwise the agent offers Connect GitHub.

- **Haiku 5.5 can be picked as an agent model and is metered.** It is billed at Anthropic's list price by prompt length: $0.10 input and $0.50 output per million tokens up to a 100,000-token prompt, and five times that above it. Before this, turns on `claude-haiku-5-5` were recorded as free.

- A channel admin can connect GitHub repos, including personal ones, to an agent they manage (one limited to their channels and not a Daimon default agent), from the agent's Connect GitHub or by asking the agent. Repos connected for an agent belong to it: no other agent can be given them, by anyone. Connecting again adds repos and keeps the ones it has. A server admin's Connect for one agent works the same way. Whoever manages the agent can remove its repos, which removes only that agent's access. If the agent had a saved GitHub key, the key is retired once its working repo and skill repos are among its repos.

- An operator live QA runner validates external scenario catalogs, exercises disposable Discord QA channels, verifies Haiku model pins before triggers, evaluates collected evidence even on silent/stuck watches, records guarded daily costs, and alerts on failures or repeated unavailable checks. Shared hourly canary/daily catalog cadence, offline tests, and an uninstalled timer template are included; production canary remains disabled pending approval.

- `DAIMON_DISCORD__UNMENTIONED_REPLY_HINT` (off by default): on Discord, the first reply in Daimon's thread that doesn't @mention Daimon gets a 🔔 reaction, once per thread a day, as a hint that only a mention continues the conversation. The reply itself is still not answered. Bots never get it, and neither do channels where Daimon may not post.
- The approval card's expiry is now a setting, `DAIMON_TOOL_SAFETY__CONFIRMATION_TIMEOUT_S`. It defaults to 600 seconds and can be set between 30 and 3600, so staging can use a short expiry while production keeps ten minutes.
- Where approval cards are turned off, a person adds a skill from chat by replying `yes` to its preview. "Add this skill to our agent" previews it. Daimon reads the person's next message in that thread itself: a plain yes (or y, confirm, approve) approves the upload for that turn, and any other reply cancels it. The preview is valid for 15 minutes, for that person, thread, agent and content only, and is used once. A newer preview replaces an older one, even when two arrive at once: only one preview per person, thread and agent is ever open. Who may change the agent is unchanged. Discord turns pass the message; elsewhere the chat path stays preview-only.
- Turns over the concurrency caps now wait instead of being refused. On Discord, Slack and Teams a mention over the per-workspace cap (or Discord's process-wide cap) shows the usual "Working on it…" card with Stop and starts when a slot frees, served round-robin across workspaces and in order within one. Stop cancels a waiting turn. A turn still waiting after five minutes ends with the usual error. The capacity notice now appears only when the queue itself is full (50 per workspace, 500 in total, per adapter process; `DAIMON_TURN_QUEUE__*`). Queue depth and wait times are in the `runtime.health` log.
- MCP browser pages now share a responsive Daimon shell with Daimon's face in
  the page header, local Inter font, brand colours, and inline action icons.
  The picker uses separate desktop, tablet, and phone layouts. GitHub and Slack
  page copy has been rewritten. The GitHub picker shows access
  before the repo list on narrow screens, keeps selections across search, and
  locks controls during Connect. Billing, Slack installation, personal GitHub
  linking, and MCP connection pages use the same shell. Tailwind CSS is
  generated from the server templates with a pinned standalone CLI and checked
  in CI.
- Agent identity can be disabled for selected Discord guilds or Slack workspaces
  while remaining enabled elsewhere in the deployment.
- Discord and Slack GitHub setup panels now manage connected repos, agent grants,
  personal links, waiting requests, and disconnects. The
  server-rendered connection pages include a searchable picker, a persistent
  Connect repos action, and pages for approval and recovery states.
- Agents can offer an admin a private GitHub connect link bound to the current agent; members can record a setup request. `/github connect` is available in Discord and Slack with a private button. Self-serve links refuse legacy-mode agents with a saved GitHub key, working repo, skill repo, or channel pin. An operator can issue an agent-bound CLI link for a saved-key agent, then finish the staged update with `daimon github finish-update`. New selections default to **Read and write**: push branches, open issues and pull requests. **Read only** reads code, issues and pull requests.
- With agent identity enabled, new default pictures use a tenant-assigned
  Daimon face built from the production mascot and canonical expressions.
  Layer IDs and draw weights live in a manifest; stored variants keep their
  appearance when the draw table changes.
  First-use face generation runs after a turn starts, and stored 20 px
  thumbnails keep assignment fast in larger workspaces.
  Public avatar URLs can serve pre-rendered 128 px and 512 px images. An
  initials URL remains valid after its generated face is stored, until an
  admin changes the picture. Uploads and the identity-off behavior keep their
  existing paths.
- Agent identity now has a deployment switch, `DAIMON_AGENT_IDENTITY__ENABLED`,
  off by default. When off, Slack and Discord post as the app, Discord replies
  need a mention, Teams omits the agent name prefix, and setup panels hide
  avatar controls.
- **👎 feedback can go to the support channel, per tenant.** With
  `DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT` on for a tenant (off by default), each
  submitted "What went wrong?" form on Slack, and each 👎 text on Discord, is
  also posted once to the channel Ask a human uses, with the person, the agent,
  the reasons, the text and a link to the answer. The form tells the person
  it is shared.
- **Teams feedback catches up with Slack.** 👎 on a Teams answer now opens a
  "What went wrong?" form with the same reasons and optional text, and 👍 is
  acknowledged. Only people who could start a turn at the answer can vote.
  `DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT` now covers Teams: a submitted form is
  posted once to the support channel, spending no credit. With support set
  up, answers carry an Ask a person button that opens the support form for
  that answer. Turns that only ran tools get both on their finished card;
  cancelled turns get neither.
- **Teams support requests reach a channel's own admins first.** As on Slack
  and Discord, a request asked from a Teams channel with channel admins now
  goes to them in 1:1 chats, then to the organisation's admins, and to the
  support channel only when no chat landed. Only people who could start a
  turn in that channel can ask from it. A channel read only from inside
  is marked in the request and in a routed 👎 form, and the form warns that
  the note leaves the channel.
- **Add skill in the Teams setup panel.** An agent's Details in `setup` now
  has an Add skill button, as on Discord and Slack. Its dialog takes a pasted
  SKILL.md, previews its name, description, files and any scripts, and adds
  it as the agent's own skill when sent again unchanged. The same people may
  add as on the other platforms, re-checked on every step. Teams dialogs take
  no files, so a `.zip` is attached in a message and added from chat.
- **Channel skills in the Teams Channel settings dialog.** Server admins can
  now see a channel's extra skills in its Channel settings dialog, add one by
  name, `agent/name` or skill id, and tick skills to remove them, as Who
  answers where offers on Discord and Slack. The same core checks decide
  which skills a channel may add, refusals say why, and every save is
  audited. A channel's own admins still can't change them.
- **Teams channel reads link files.** `read_channel`, `read_thread`,
  `get_message` and `search_messages` now return each file on a message with
  a download link where the channel's SharePoint site is granted, as Slack
  reads do, so an agent can open a file it finds there. Only files in the
  channel's own Files folder are linked: the site grant ignores SharePoint's
  per-file permissions, so a file from another library or folder, and any
  file elsewhere, is still listed by name. Links last about an hour and only
  come with messages the read already returns, so a sealed thread's files
  stay out. Each file is now an object with `name` and `url`, not a name.
- Agent setup uses short Picture labels on Slack and Discord. Discord Change opens a file upload form; the attachment option on `/agent-setup` remains available.
- Status cards and follow-up prompts use short labels on Discord, Slack and Teams. Working cards show Stop, tool steps sit under Details, and finished Discord and Slack cards end with one summary line of time, cost and money left for everyone. Error, stop, restart and post-failure notices show a clear next step. Feedback and help forms use the same short wording across platforms. Threads whose channel responder changed offer Switch to the new agent and a new-thread hint; staying with the old agent in that thread is not a supported action.
- **Teams files in private and shared channels, turned on by asking.** The
  Enable files sign-in now starts from a channel and grants daimon that
  channel's own SharePoint site: the team's for a standard channel, a site of
  its own for a private or shared one. The admin's sign-in finds the channel's
  Files folder, which daimon's `Sites.Selected` permission cannot, and daimon
  stores it (`teams_channel_sites`). The admin must be a member of the channel
  and a SharePoint or global admin; the sign-in now also asks for the
  delegated `Files.Read.All`. Asked to turn files on, or for a file where they
  are off, the agent calls the new `enable_channel_files` tool and the bot
  posts the card after its answer, instead of offering an artifact or notebook.
- Discord server admins can change an agent's avatar with an attachment on `/agent-setup` or reset it from the agent detail panel. The panel shows the current public avatar and warns that caches may keep old images.
- Slack workspace admins can change or reset an agent's avatar in `/agent-setup`; uploads are cropped and saved as metadata-free PNGs. Teams answers from non-built-in agents start with the agent's bold name on the first chunk.
- Slack turn messages now carry the answering agent's name and avatar. Built-in Daimon retains the app identity. Existing installs without `chat:write.customize` fall back to the bot header until reinstalled. Turn posts are recorded for agent ownership checks.
- Discord agent replies can show each agent's name and avatar through a pool of channel webhooks. Replies to recorded bot or application-owned webhook posts can start a turn without mentioning the bot. Servers without Manage Webhooks permission retain bot posts with a name on the first answer chunk.
- **Agents choose how long a notebook link lasts.** `create_notebook_upload_url` takes `ttl_days`, 1 to 365, default 1, for a read-only scratch notebook; the upload response and `list_notebooks` report `expires_at`. A blog is still kept until it is deleted, and the editor still lasts the host's `subprocess_ttl_seconds`. Read-only notebooks and blogs now survive notebook-host restarts and deploys. The host starts one when someone opens its link and stops it after two hours without a visit (`DAIMON_NOTEBOOK__WARM_WINDOW_SECONDS`), so a link that has been quiet takes a few seconds to load. The host caps lifetimes with `DAIMON_NOTEBOOK__MAX_NOTEBOOK_TTL_SECONDS` (365 days). Re-uploading a blog without `permanent` keeps it a blog.
- **Slack 👎 asks what went wrong, every time.** The form opens as soon as 👎 is clicked, with optional reasons (wrong or inaccurate, didn't do what I asked, incomplete or cut off, too slow, something else) and optional text. A repeat 👎 opens it again so details can be added later. If Slack doesn't open the form, the person gets a private button that does. Reasons are stored on the feedback row (`message_feedback.feedback_reasons`), next to the text. Only people who could start a turn there can vote.
- GitHub App grants can be staged and activated per agent. App sessions use per-turn repository tokens and a session-owned vault; legacy agents retain their existing GitHub path. The GitHub connection page offers Select all for repos the confirmer administers, and the default environment includes `gh`.

- `/here` on Discord and Slack shows a compact, private status card: which agent answers here, its reading scope and whether publishing needs approval. When nothing can answer (no channel access, replies disabled, no agent selected) the card shows only that reason. Credential names and routing details stay off the card; the `where_am_i` MCP tool returns the same short summary for conversational questions, with the full structured facts alongside. Discord read and scoped search tools explain when the bot lacks channel view or message history access.
- **`here` on Teams.** Typed in a channel post, `here` answers in the 1:1
  chat with the same compact card as Discord and Slack for that post's
  thread: who answers, what it can read and whether publishing needs
  approval. Typed in the chat it describes the chat, or the setup
  conversation it is in. Only the place it was typed in counts as one the
  caller can see. The `where_am_i` MCP tool now answers in Teams channel
  turns too, and its facts name a Teams setter by stored name.
- **Teams `memory` shows the agent where it was typed.** Typed in a channel
  post, `memory` now lists what that post's agent remembers, following a
  thread handed to another agent, instead of the 1:1 chat's agent. The answer
  still arrives in the 1:1 chat, so when the channel's readers are limited
  (`inside` or `own`) the card says the memory is kept inside the channel and
  shows nothing, rather than carry it out. Typed in the chat it is
  unchanged.
- **Teams `billing` shows the channel's budget.** Typed in a channel with a
  budget, `billing` adds the **This channel** section the Discord and Slack
  panels show, worded from its state, and keeps it across Refresh, Add credit
  and Redeem code clicks on the card. The channel is held server-side for the
  person who typed the command, so a crafted click cannot read another
  channel's budget; after a restart a refresh drops the section until
  `billing` is typed again.
- **Look up a person on the Teams billing card.** Admins get a **Look up a
  person** action with the Teams people picker over the organisation's
  directory. Picking someone shows their spend and turn count this month
  under the actions, with the name daimon stored for them, or "Name
  unavailable" for someone it has never seen. Admin is checked again on the
  click, as on Discord and Slack.

- Slack message reads and searches include file metadata and expiring download links. `send_message` uploads staged file handles, or reposts a file link from the same workspace when the requester can read the file where it was shared and the channel policy lets the call read it there; a file in a sealed thread is reposted only into that thread, and a file shared only in a 1:1 DM only into a DM. `add_skill` applies the same check to a Slack file link. A caption is required, the combined limit is ten files, and posting needs the `files:write` scope. The upload messages are recorded with the caption, so the agent can delete them and `delete_thread` still accepts a thread whose files it posted. Slack thread reads return continuation cursors for newer replies.
- A thread can move to another agent without opening a new thread. Ask the agent in the thread to hand it over (`hand_off_task`), or, when a channel's agent changed under an existing thread, press Hand over on the notice that thread shows (Discord, Slack and Teams). The new agent gets the conversation and working files from the next message, and the old session is archived with its history still readable. Inherited seals keep memory read-only. A member may hand a thread to an agent of that channel: the one it answers with, one pinned to it, or one of an isolated channel's own agents. Other agents need a server admin, or a channel admin of the channel when the thread is not sealed and the agent is one they could make its default.
- Agents can tidy their own posts on Discord, Slack and Teams. `edit_message` and `delete_message` change one message the agent posted with `send_message` or `create_thread`; `archive_thread` (Discord) and `delete_thread` (Discord, Slack) close a thread it opened. An agent can change only what it posted itself, which daimon now records at send time; people's messages and other agents' and other bots' posts are refused. Each call is checked against the channel policy (protection, pins, isolation, seals), and checked again on a fresh policy right before the Discord or Slack call. Calls are refused in the support-escalation channels. Discord thread cleanup keeps the thread and other people's messages. Bulk deletes check and audit each message. Limits are 10 message actions per turn and 40 per hour per agent, and 20 refused calls in an hour pause tidying for that agent. Every edit or delete writes a security audit row with the channel and message ids, an HMAC of the replaced text keyed by a server secret, and the turn, never the text. Post records expire with `daimon audit prune`, and erasing an account clears the HMACs on its audit rows. A new seeded skill, `channel-tidy`, tells agents when to use them.
- **"Tidy this thread" works on Discord.** An agent can now edit and delete its own chat replies and status cards (empty cards included), and archive or clear the thread it opened from someone's mention. Daimon records what each turn posts, with the agent and the person who asked. A reply or card can be tidied once its turn has finished, and only when the person asking started that turn, opened the thread with that agent, or is a server admin; archiving or clearing an auto-opened thread takes the person who opened it or a server admin. People's messages and other agents' posts stay refused. Erasing an account clears the person's id from these records. Posts from before this release, turn error notices and setup-wizard turns are not recorded and can still be removed by hand. Slack turn replies are not covered yet.
- **Channel and agent rules.** Two rules now say what reaches whom ([permissions](docs/permissions.md)). A channel rule sets its `readers` (`any`, `inside`: only turns in it, `own`: only its own agents) and `writers` (`any`, `own`, `none`); an agent rule sets the only channels an agent runs in (`runs_in`). Admins set them by asking daimon (`set_channel_rule`, `set_agent_rule`), on each channel's **Permissions** screen (Discord and Slack setup panels, the Teams Channel settings dialog), or with `daimon channels rule set` and `daimon agents rule set`. `daimon tenants access-policy rules` lists them. Rules are per channel; only `daimon channels rule set --thread` keeps one Slack or Discord thread to turns inside it, and threads sealed before keep their seal.
- Tenant admins can issue a GitHub App connection invitation; a browser confirmation checks repository admin access before authorizing repos for the workspace. The flow is available only with the separate GitHub App settings and encryption keys.
- Separate `DAIMON_GITHUB_APP__*` settings for agent-scoped GitHub App credentials.

- **The Teams app can be added to private and shared channels.** The
  manifest template moves to version 1.25 with `supportsChannelFeatures:
  tier1` (package version 0.4.1). Adding the app to a team does not add it to
  these channels: each one's owner adds it from the channel. Rebuild the
  package and upload it again to use them.
- **Operator tokens from the setup panels.** Server admins on Discord, Slack
  and Teams mint, list and revoke operator tokens from Who answers where:
  `tenant:read`, `channels:write`, `agents:archive` and `promo:redeem` only
  (`promo:create` stays with `daimon mcp mint-operator-token`), for 30 days,
  shown once. The panels list and revoke only tokens within those scopes, so a
  CLI-minted `promo:create` token is managed only from the CLI. The listing
  never shows a token, and every mint and revoke is audited.
- **Teams channel admins mint coding-tool tokens.** The setup panel's "Use
  from your coding tools" dialog now asks a channel admin which of their
  channels the token runs in, and binds it there under the same rule as
  Discord and Slack: the agent must be pinned to channels they administer,
  and an unbound token stays with server admins, who keep that choice.
- **Teams channel settings.** Who answers where in the Teams setup panel
  opens a Channel settings dialog for a channel picked there: its
  environment, for server admins and that channel's admins, and its
  isolation and channel admins, for server admins only, as on Discord and
  Slack. Every save is re-checked and audited.
- **Skills from Teams files.** `add_skill(attachment_url=…)` takes a `.md`
  or `.zip` shared in a Teams 1:1 chat by its download link: https on a
  SharePoint, OneDrive or Graph host only, sent without a token, with no
  redirect off those hosts and Discord's and Slack's caps and preview.
- **`daimon skills add`.** `daimon skills add --agent NAME PATH|URL` adds one
  skill to one agent from the CLI: a local folder, SKILL.md or `.zip`, or a
  folder of a public GitHub repo (`--branch`, `--path`). It shows the skill's
  files and runnable files, asks before uploading unless `--yes`, and follows
  the same rules as the chat tool and the setup panels: a built-in agent is
  refused, and a skill another agent also has is never given a new version.

- **Ask a human on Slack.** Slack answers get an Ask a human button beside 👍/👎 when `DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID` is set. It opens a form and posts the note, the asker and a link to the answer (no answer text, link previews off) to that channel. It uses the same per-person, per-workspace allowance and messages as Discord and Teams, from the same ledger. Slack requests post only to the Slack channel, never to Discord's or Teams' channel; leaving the Slack one unset keeps the button hidden. `DAIMON_SUPPORT__SLACK_ESCALATION_TEAM_ID` names the workspace that owns the channel when daimon is installed in several. Only people who may start a turn in that thread can ask, checked on click and again under the tenant's policy lock in the transaction that spends the credit, so a policy edit committed first refuses with nothing spent and a later one waits. The escalation channel's protection is checked from a fresh read right before the post. Asking twice on one answer records and posts once. Requests from a sealed channel warn that the note leaves it.
- **Scoped operator tokens for integrations.** `daimon mcp mint-operator-token`
  mints a token acting over `/mcp` for one server admin with only the scopes it
  names (`tenant:read`, `channels:write`, `promo:redeem`, deployment-wide
  `promo:create` with an optional `--max-issued-usd`), for 30 days by default
  and at most 90. Each request rechecks the row and the account's stored admin
  role, which changes on the person's next platform turn, so `revoke-token` is
  the immediate stop and `set-token-scopes` narrows. Calls are rate limited
  (`DAIMON_MCP__OPERATOR_CALLS_PER_MINUTE`); calls, refusals and token changes
  are audited. Channel budget, agent and admin tools take the scopes, and
  `tenant:read` lists environments to pick for a channel. New tools:
  `get_tenant_summary` (with each channel's admins; `daimon channels list`
  prints the same) and, for `promo:create`,
  `create_promo_code`, `list_promo_codes`, `revoke_promo_code`. `mint-token`
  tokens now expire and can be revoked. Run migration `0036_operator_tokens`.

- **Teams reaches parity with Discord.** "Summarise this channel" works: the
  channel tools (`list_channels`, `read_channel`, `read_thread`,
  `get_message`, `list_threads`, `parse_link`, `search_messages`) read Teams
  through Graph for anyone on the channel's roster, and a turn names only the
  tools its platform has. Turns replay posts with their replies, earlier
  images and files, senders, times and channel names, and say when history
  could not be read. `send_direct_message`, channel budgets and channel
  admins, files through `send_message`, `post_wizard` forms (Adaptive Cards)
  and routine destinations work on Teams. A command typed in a channel is
  answered in the 1:1 chat, `support` asks a person for help, completion
  pings post the answer fresh with an @mention, admins redeem promo codes from
  the `billing` card, the bot says hello when installed and forgets a team it
  is removed from, and a restart finds channel cards whose post lost its id.

- **Teams takes every private form Discord and Slack do.** A key value can
  span lines, `request_agent_key` without a key posts a `.env` form on Teams
  (the file is pasted, since a dialog has no file input), and repo and
  skill-repo cards open a GitHub token form that binds the working repo or
  imports and attaches the skills, with Slack's admin gate and whole-file
  rules. A token that cannot read the repo is refused in the dialog before
  the request is spent.
- **Teams channel turns replay their thread, as on Discord and Slack.** The
  first turn in a thread reads the root post and its newest replies through
  Microsoft Graph, a later turn only what came after the last message it read,
  and a mention that starts a thread the channel's recent posts, all marked
  untrusted. A bare @mention asks about the thread. Images pasted into a
  channel message now reach the agent; files shared in a channel are named and
  the person is told they can't be opened, as is anything Graph can't read.
  This needs the resource-specific consent `ChannelMessage.Read.Group`, which
  a team owner grants at install: upload the updated app package again. A
  refused or slow read never fails a turn, which then runs without history.

- **Teams channels can take files, once an admin grants the team's site.**
  With the Graph application permission `Sites.Selected` and a write grant on
  a team's SharePoint site, files shared in its channels reach the agent as
  download links, and files the agent writes are uploaded to the channel's
  Files tab and linked below its answer, or in one message; one that fails to
  upload is named there. The turn tells the agent whether this works in that
  channel. Without a grant, in private and shared channels, or when Graph
  refuses, channels behave as before. The manifest is unchanged; `docs/teams.md` has
  the grant steps.

- **Teams threads can be followed, as on Discord.** Ask the agent to follow a
  thread (`set_thread_participation`; a channel or the whole organisation
  needs a listed admin) and it reads replies nobody addressed to it, then
  joins in when the same classifier, quiet timer and hourly cap Discord uses
  say it can help. The turn passes the usual admission and billing gates,
  runs as the newest author, and posts only its answer: no status card,
  notice or error. Root posts, bots and protected channels are never judged,
  and without Graph history a followed thread stays mention-only. Quoting one
  of the bot's messages in a channel now counts as mentioning it, and the
  quoted text reaches the agent in place.

- `scripts/hackathon_rehearsal_readout.py` prints stage readouts from staging logs, Monitoring metrics and content-free turn outcomes.
- Long-running adapters emit `runtime.health` logs every 30 seconds with Anthropic response attempts, database pool use, event loop lag and turns in flight; `DAIMON_OBSERVABILITY__HEALTH_INTERVAL_S=0` disables them.
- Microsoft Teams adapter: answers in 1:1 chats and when @mentioned in
  channels, with an in-place status card, author-only Cancel, per-thread
  queueing, feedback buttons and restart recovery. The configured Entra
  organisation is provisioned at boot. The 1:1 chat offers `help`, `new`,
  `setup` (with setup conversations), `routines`, `memory`, `privacy` and
  `billing`; admins are the owners of the team in
  `DAIMON_TEAMS__ADMIN_TEAM_ID`. Pasted images and files shared in 1:1 chats
  reach the agent, output files are delivered with file consent cards, and
  `send_message`, `create_thread`, task handoffs and timers work from Teams
  turns; the wake poller runs handoffs and timers across restarts.
  Agent keys, MCP tokens and MCP sign-ins are collected privately through
  Teams dialogs, and with tool safety on, attached-tool writes wait on an
  Approve/Deny card only the requester can answer. A turn that ends early
  explains why with the termination notice. Protected channels get no
  replies, notices or tool posts. Tenant turn caps, agent pins, key-name
  rules, prepaid balance footers and spend-limit alerts match Slack.
  Builds on #220 by @jchu96. See `docs/teams.md`.
- Staging-only load rehearsal script for synthetic installs and metered headless turns, with a dry run, spend guard, and tenant cleanup.

- Optional Discord webhook alerts for new installs, Stripe top-ups, and Anthropic spend or overload events.

- Discord delivers files generated during tool-using turns into the chat thread,
  with upload-limit notices and protected-channel checks.
- **Channel budgets.** Admins can cap what one Discord, Slack or Teams channel
  may spend, monthly, in total or over a fixed date range, with
  `set_channel_budget`, `clear_channel_budget` and `list_channel_budgets`, or
  `daimon channels budget set|clear|list`. Once a channel's debits (markup
  included, threads counting toward their channel) reach its limit, new turns
  there are refused, as are unprompted replies, wakes, `/dm` and DMs moved
  from there, YouTube transcripts asked for there and routine fires that post
  there or were made there. Members can read a budget with `get_channel_budget`, and
  `/billing` shows the channel's spend against it. Nothing changes until a
  budget is set, with or without Stripe. Usage and debits now record their
  channel from this release on, and sessions carry it as
  `daimon_budget_channel`.
- **Channel admins.** Server and workspace admins can name the roles and
  members who run a channel, with `set_channel_admins`, `list_channel_admins`
  and `clear_channel_admins`, from Who answers where in the setup panel, or
  with `daimon channels admins`. A channel admin may change agents that answer
  only in channels they run (instructions, skills, keys, MCP servers, repos)
  and set or clear those channels' default agent, though never to another
  channel's own agent; built-in agents and the server default stay with server
  admins. Slack grants can also name user groups, and Teams grants a team,
  whose owners they admit. A routine or wake set up by someone with more
  rights keeps the agent out of a channel admin's hands, and so do a
  personal default, someone else's live session or routine in another channel
  or in no known one, and, for key, MCP server and skill repo changes,
  answering nowhere. Connecting a skill repo now counts an agent as shared
  wherever a key change does (someone's personal default, a bound thread, or
  another member's routine or live session) on Discord and Slack too, as it
  already did over MCP. An admin of every channel an agent is pinned to may
  change it from outside them, and from the hub may list and read the sealed
  conversations of the channels they run, as a server admin may of any, when
  every seal on one lies in those channels; DMs stay private. No chat
  tool, panel or CLI `config` write can make a pinned agent the default of a
  channel outside its pin. Nothing changes until a channel admin is named.
- **Channel-bound coding-tool tokens.** "Use from your coding tools" pressed
  in a sealed channel, or in a channel the agent is pinned to (a thread counts
  as its parent), now mints a token bound to that channel, and the reply says
  so. Its calls run as a turn there: the agent's pin admits it, it reads that
  channel's sealed conversations and continues those opened under the seal,
  and it uses the channel's environment and budget. Its sessions are stamped
  with the channel (sealed in a sealed one, with read-only memory unless the
  agent is that isolated channel's own), so their spend counts there. The
  token never carries its minter's channel admin grants. A channel admin of
  every channel an agent is pinned to may now mint one from inside those
  channels, always bound. Tokens minted elsewhere, and existing ones, are
  unchanged. Run migration `0037_mcp_token_channels`.
- **Channel isolation.** A server admin can isolate a channel from Who
  answers where in the setup panel, with `set_channel_isolation` (also under
  an operator token's `channels:write`), with `daimon channels isolate` (which
  can copy an agent too) or with `--isolated-channel`. That
  seals it and pins its default agent to it alone in one write; a default
  that is built in or answers elsewhere is refused with the reason, unless
  the admin asks for a copy, made without credentials. Inside, only the
  channel's own agents run, post, read and take routines or bindings, with
  writable memory, and only they read its sessions; they are hidden
  elsewhere, post nowhere else, send no DMs, and `/dm` there is refused. A
  conversation in the channel keeps its posts, cards and routines inside it,
  while its setup thread can still configure the channel's agent. The CLI
  refuses a pin or seal change that would break an isolated channel. Teams
  channels isolate too, a thread counting as its channel. The panel shows the
  channel as private, dedicated agent and hidden. Ending keeps the seal and
  pins unless lifted too, and warns that the agents keep what they
  remembered there. No migration; clear isolation before rolling back, as
  older releases reject the field.
- **Channel protection.** `set_channel_protection` (also under an operator
  token's `channels:write`) and `daimon channels protect` protect or seal one
  channel, or lift either, without restating the policy's lists. Only server
  admins and operator tokens may, never a channel admin; an isolated channel
  stays sealed until its isolation ends.
- **Archiving an isolation copy.** `archive_isolation_copy` (also under a new
  operator scope, `agents:archive`) archives the agent `set_channel_isolation`
  copied for a channel once that channel closes, with its pin and default
  there. Only server admins and operator tokens may; it never archives
  another agent, a default, or a copy still pinned or a default elsewhere.
- Operators can isolate a channel from the CLI with `daimon channels isolation set PLATFORM WORKSPACE_ID CHANNEL_ID [--fork-from AGENT]`, which copies the agent when asked, and end it with `daimon channels isolation lift`, which also drops the seal and pins. `scripts/hackathon_layout.py` uses them to set up one private, isolated, budgeted channel per hackathon team with pre-created threads, and tears the layout down.
- **Channel environments.** Admins can pick the environment a Discord,
  Slack or Teams channel's turns run in, or the workspace default, with
  `set_channel_environment` and `clear_channel_environment` (a channel's, not
  the workspace's, also under an operator token's `channels:write`); an admin
  of a channel can pick that channel's. In a sealed channel, or one holding a
  sealed Slack thread or the sealed Discord thread the pick names, an
  environment with unrestricted networking (any network beyond package
  managers and MCP servers, so any allowed host) needs a server admin, and so
  does clearing a pick when the default it falls back to has one. Sealing or
  isolating a channel whose own pick has such a network warns that a server
  admin should confirm or change it. Who answers where in the setup panel
  lists each channel's environment on the reader's side of an isolation and
  offers a select for it, and `explain_agent_resolution` names the
  environment and the tier that chose it.
  A conversation switches from its next message and keeps its files and its
  seal, and a routine runs in its channel's environment. Archiving or deleting
  an environment clears the picks that named it. The missing-environment
  notice now points at the panel instead of the operator. With no channel or
  workspace environment set, nothing changes; see the upgrade notes for
  routines where one already is. A thread id names its channel on every
  platform, and the caller must be able to see the channel.
- **Skill uploads.** Add one skill to one agent from a pasted SKILL.md, a
  `.md` or `.zip` attached on Discord or Slack, or a GitHub folder, with
  `add_skill` or Add skill on the setup panel's agent details (Slack takes a
  paste; attach files in chat). Each add previews the name, description, files
  and any runnable scripts first. The skill becomes that agent's own copy and
  never touches shared or built-in skills, or one another agent shares;
  archives are unpacked safely and size-capped. A chat add lands only when the
  person presses Approve on a confirmation card, so it needs tool safety;
  without it the panel's Add skill is the way in. Built-in agents are refused;
  otherwise server admins, channel admins on agents local to their channels,
  and anyone on agents nobody else uses, read as widely as a key change.
  `remove_skill` follows it too. A pinned agent takes an add, in chat or on the
  panel, only from its own channels, and an isolated channel's agents add and list skills only
  inside it. Nothing changes until someone adds a skill.
- Optional Discord process-wide turn limit for guild chats and DMs. Excess
  requested turns get a retry notice; surfaced Anthropic 429/529 responses
  emit structured logs.
- Operators can credit tenants with `daimon tenants credit --note` and set default
  or per-person monthly caps with `daimon tenants cap`. Caps apply without Stripe;
  prepaid Discord and Slack turn footers show the remaining balance.
- Operators can set a tenant's concurrent chat-turn cap with
  `daimon tenants turn-cap`, or clear it to use the deployment default.
- **Promo codes.** Operators create credit or timed codes with `daimon promo`,
  and admins redeem them from `/billing` on Discord and Slack or with the MCP
  tool `redeem_promo_code`. Timed credit is spent first inside its window and
  the unspent rest expires when it closes; spend dated inside the window is
  credited back if it is recorded within 15 minutes of the close. Nothing is
  visible until a code can be redeemed: only then do the Redeem code button
  and the join-message lines appear. Turn debits now store the model call's
  time as `occurred_at`.
- Record content-free turn outcomes across chat, headless, routines and MCP hub/agent-chat, including attributed admission refusals, with bounded best-effort persistence. MCP `ask` records its terminal reason; fire-and-forget `start_turn`/`continue_turn` record dispatch only (`unknown`), without a later terminal update. Pre-attribution and adapter readiness gates are outside coverage.
- Routines can name an optional destination channel or thread
  (`create_routine`/`update_routine` `destination_kind` + `destination_id`,
  `clear_destination`), validated on save against the caller's own server or
  workspace. The run is told where its result goes, and if the agent does not
  post there itself, the Discord or Slack adapter posts the result, at most
  once, after checking the tenant's protected channels and invoker allowlist.
  A protected or unreachable destination sends the result to the routine's
  creator by direct message instead. Routines without a destination behave as
  before.

- Per-turn token, cache and estimated provider-cost telemetry shares the terminal outcome row; operators can query tenant usage by channel and origin with `daimon usage turns`. MCP SDK polling outcomes retain unknown usage rather than zero. Billing and admission behavior are unchanged.

- Opt-in Discord and Slack DM conversations: admins enable with `/dm enable`;
  `/dm` moves recent channel context into a private, resettable session. Every
  private turn checks live membership and the tenant invoker policy. Privacy
  preview and deletion include bounded local DM context. Slack checks IM scopes
  before setup and uses signed execution grants in isolated session vaults.
  Private session transcripts and controls require that exact grant, preventing
  same-account routines, channel turns and MCP callers from borrowing access.
  Slack private turns use fresh sessions with bounded history replay.

- Every turn now ends with a typed `TerminationReason` from the turn core,
  set on `TurnState.termination` and `RunOutcome.termination` for every driver
  exit and derivable from admission and session-binding refusals with
  `termination_reason(err)`. Notices and outcome records can build on one
  vocabulary.
- When a turn ends early, the red Discord card and the Slack error card now
  explain it: what happened, which tools were still running, what was kept,
  what to do next, and a request id to find the error in the logs. The raw,
  truncated error text no longer appears in the card.

- Memory mounts are read-only for sealed-channel turns, routines, and DMs when
  the tenant policy requests it. Session reuse enforces policy changes before
  another turn runs, including wizard submissions in sealed threads.
- Security audit writes run in bounded background tasks. Privacy deletion erases user identifiers, tenant deletion removes audit rows, and `daimon audit prune` applies configurable retention (90 days by default). Tool errors are distinguished from authorization denials.
- Database deletion triggers erase audit identifiers even when older privacy workers delete accounts or tenants during a rolling upgrade.
- The security audit covers the main JWT MCP application. Separate hub OAuth applications (`/discord/mcp`, `/slack/mcp`) and adapter setup panels are excluded in this version.
- Authenticated main-JWT MCP calls and listings now append tenant-scoped security audit metadata, including shared operation-policy decisions. Operators can query and export it with `daimon audit list`; database guards prevent ordinary updates, deletes and truncation.

- Core selects chat, routine, relay and handoff prompt fragments per invocation.
  Agent specs can replace or extend each fragment; default chat is unchanged.
- Per-tenant operator-funded mode replaces depleted-balance refusals with structured alerts while preserving usage metering and configured caps. Configure it with `daimon tenants funding-mode`.
- Optional Postgres backup service, checksum-verified empty-database restore and
  isolated restore drill; workspace-wide Managed Agents object export and a
  state-by-state disaster recovery contract in the self-hosting guide.
- Routine dispatch runs independently of scheduler ticks, with bounded in-flight tasks, per-routine `skip`/`run-once` catch-up policies, and visible skipped-slot ranges.
- Tenants can opt into accepted/done reactions and a fresh final reply that pings
  only the requester on Discord and Slack with `completion_pings`; defaults stay unchanged.
- `daimon tenants access-policy get|set PLATFORM EXTERNAL_ID` shows and edits a
  tenant's access policy (invoker allowlist, protected channels and
  categories, sealed channels, DM memory); `--clear` puts the tenant back on
  the open default. IDs are validated per platform before writing, and concurrent
  edits preserve fields changed by other CLI commands.
- Sealed channels: the channel read tools refuse a channel the tenant access
  policy seals, and threads under it, unless the call passes the
  `origin_context_id` of a turn inside that channel and the token executes
  as that turn's agent. A single Discord thread, or a Slack thread keyed
  `channel_id:thread_ts`, can be sealed on its own. Search withholds sealed
  hits and, once anything is sealed, counts only what it shows. The read
  tools gain an optional `origin_context_id` parameter.
- Protected channels: the agent, including its own replies, never writes into
  channels, threads and Discord categories the tenant access policy marks
  protected, for admins as well. A mention there is dropped silently (no
  thread, reply or upload), and the write tools (send message, create or
  rename a thread, credential, wizard and app-install cards) refuse them.
  When the policy can't be read (including a database outage) the agent
  stays silent in that channel rather than posting an error there.
- Tenant access policy: a tenant can limit who may start a turn to a list of
  platform user ids (admins are always allowed). Discord and Slack refuse
  anyone else at admission with a notice, and so do the MCP and hub turn
  tools; routines whose creator is no longer allowed skip with
  `invoker_not_allowed`. Tenants without a policy are unchanged. The policy also carries protected and sealed channel lists for
  the channel tools.
- Opt-in write safety for attached third-party MCP tools
  (`DAIMON_TOOL_SAFETY__ENABLED`, off by default). Each tool is classified read
  or write (operator override, then MCP annotations, then its name; unknown
  means write). In chat, a write waits for the requester to press Approve on a
  Discord or Slack confirmation card showing the exact input; in routines,
  writes are refused unless listed in `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`;
  `DAIMON_TOOL_SAFETY__DENIED` blocks a server or tool everywhere. The
  confirmation card is a reusable `ConfirmationHook`; surfaces without one
  refuse the write.
- `update_agent` now refuses to re-point the reserved `daimon-mcp` server or to
  register the deployment's own MCP endpoint under another name, matching
  `attach_mcp_server`.

- `send_direct_message` delivers private agent messages to verified Discord/Slack
  tenant members, with per-tenant disabled/allowlist policies and delivery receipts.
- A durable wake queue runs a turn in an existing thread later, at most once,
  through the same admission, bind and run path as a mention. The Discord and
  Slack adapters poll for due wakes. A wake whose process dies before its turn
  starts is retried when the lease expires. One that dies after the turn
  starts is settled `interrupted` and never re-run. Migration
  `0028_feat003_wake_queue` adds the lease columns to `task_continuations`.
- Discord final replies, including wizard replies, render Markdown tables as readable PNG attachments
  by default, with the raw table kept as the image's alt text; a tenant set to `false` in
  `DAIMON_TABLE_RENDERING` keeps plain text. Slack renders native wrapped table blocks only for
  tenants that opt in. Other adapters keep plain text; unsupported font glyphs retain the original Markdown.
  Rejected PNG uploads and Slack table blocks retry as plain Markdown without dropping the answer.

- One-shot timers: `create_timer`, `list_timers` and `cancel_timer` let an agent
  come back to a conversation once, at a set time, with a note it left itself
  ("remind me in two hours"). A timer runs in the thread it was set in, as the
  person who asked for it, and goes through the wake queue. A cancelled timer
  never fires, and a timer whose thread now answers to a different agent is
  skipped with a notice instead of running under that agent. Handoffs and
  applied private input are now skipped the same way. Migration
  `0029_feat084_timers` adds the `timer` reason; deploy it and timer-aware
  Discord, Slack and MCP builds before anyone can create timers (see
  `docs/architecture.md`). Downgrading it deletes all timer rows.
- Added durable initial-card intent rows and bounded Discord and Slack history
  lookup. Both adapters now commit an intent before posting, record the
  returned message ID, and reconcile unresolved cards after a restart. A
  bounded TLA+ model retains the stale-edit, new-intent snapshot, and lossy
  history counterexamples.
- The scheduler prunes retired initial-card intents after seven days, at most
  500 rows per tick; active intents remain available for restart recovery.
- GitHub push-triggered skill resyncs now persist before webhook acknowledgement,
  recover through scheduler leases, retry transient binding failures with
  backoff, and retain permanent binding errors for operator action. See
  `docs/github-push-resync.md` for delivery and concurrency limits.
- Bounded TLA+ models and a source-linked coverage report for turn rendering,
  scheduling, session preparation, continuations, adapter recovery, billing,
  wizard submission, and recovery transaction atomicity.
- A bounded report-publish model checks that the visible PDF, bundle, digest,
  and retry archive stay coherent across seam failure and the local commit.
- A bounded GitHub push resync model checks durable acknowledgements,
  generation-fenced completion, crash duplicates, and fair crash-free progress.
- TLA+ models for usage metering (live recorder, usage sweep, balance gate)
  and Slack event dedupe and redelivery, each calibrated against earlier bug
  fixes. `docs/billing.md` now states the overdraft bound for concurrent and
  MCP-started turns, the sweep's attribution, and why deployments must not
  share a Managed Agents workspace.
- A bounded model and signed PostgreSQL tests capture out-of-order GitHub App
  installation repository events and the unresolved need for reconciliation.
- GitHub App installation webhooks now queue a durable repository refresh.
  The scheduler fetches all pages from GitHub and commits only the current
  generation; existing installation caches are queued on migration.
- A bounded TLA+ model for notebook upload capability replay, restart persistence,
  and the loss window after a token is burned but before its body is read.

### Security

- An unattended session with an MCP server whose name contains `/` is refused before anything is sent. A watch's tool allowlist is kept as server and tool pairs, so a server named `linear/bridge` can no longer enable an absent `bridge/get_issue` tool on the `linear` server.

- MCP server names can no longer contain `/`: attaching one (`attach_mcp_server`, `create_agent`, `update_agent`, a credential request, or an agent spec for `daimon agents create`/`update`) is refused, because tool keys use `/` between the server and the tool. A call on a server that already has such a name is never allowed by an operator key or a tool rule: it asks in chat and is refused in unattended runs, unless the operator's deny list refuses it outright.

- An unattended session's telemetry (timing, token usage and cache creation, outcome evaluations) must match its reviewed schema; a new or reshaped field there is refused like one anywhere else in the session.

- An unattended session carrying a private attribute at any level is refused; only the SDK's own request ID is allowed.

- Origin-based MCP actions require the signed grant of the calling session.
  Routines and timers cannot borrow a concurrent human origin, and empty origin
  IDs refuse. Human handoffs retain their approval surfaces.

- Fleet workers and unattended wakes re-gate Managed Agents sessions before every
  confirmation batch. Runtimes without session reads refuse unattended
  confirmations with a plain reason, including when tool safety is off.
- Agent-chat turns (`start_turn`, `continue_turn` and `ask`, with an agent key or on the hub) now follow tool safety. Before every message the session's tool settings are set again from the current policy, and each tool call is decided by the unattended rules when it is made: reads run, and a third-party write is refused unless the operator allowed it in unattended runs. These turns used to run every tool without asking. With tool safety off, calls the session still asks about are allowed.
- Agent-chat turns apply agent tool rules for their channel (a channel-bound key's channel, or the conversation's), and refuse Daimon's card-only actions: a backend switch and Microsoft mail and invites with tool safety on or off, and Microsoft file changes with it on, as in chat.
- Reading a session never answers its tool calls. An agent-chat turn left waiting on a tool call by a restart reads as `paused` from `get_session` and `get_my_session`, with a hint; only the session's owner resumes it with `continue_turn`, whose `message` is now optional for that case, and the current policy decides the waiting calls. `continue_turn` never answers a confirmation card from a chat turn.
- An MCP tool hint that marks a tool both read-only and destructive now counts as a write.
- `daimon run` treats its turn as unattended: before the message it sets the session's tool settings from the current policy, even for a session created while tool safety was off, and decides each paused call by the unattended rules (`DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`, not chat-only grants).
- Every unattended turn (agent chat, `daimon run`, routines and other headless runs, workspace checkpoints) sets the session's tool settings from the current policy right before its message and before each batch of tool confirmations, and sends each once, without automatic retries; a failed update stops that send. Fleet workers and unattended wakes decide their paused calls by the same unattended rules, with tool safety on or off.
- `daimon run` on an existing session applies that session's own tenant and channel tool rules (a Slack thread's parent channel, never a DM's source channel); only a session stamped for no tenant uses the local CLI tenant. A checkpoint turn applies its conversation's channel rules, not those of the channel it is billed to.
- A workspace transfer's checkpoint turn no longer auto-approves tool calls: its old session is gated right before the checkpoint message, and paused calls are decided by the unattended rules.
- Routines and other headless runs answer tool confirmations with the same unattended decider as agent-chat turns, never auto-approving: with tool safety off, ordinary third-party calls are still allowed, but Daimon's card-only actions (backend switch, Microsoft mail and invites; file changes with tool safety on) now ask in the routine's session and are refused.
- Fleet: a saved agent with a description that was switched to GPT runs as a fleet worker again (J4). Its OpenAI worker was refused before it started, because OpenAI agents have no description field. The worker now goes without the description, which is a label for people and not something the model uses.
- Fleet card: a worker the budget watch stopped reads why, whenever the watch stopped it, before or after it started: "its spending couldn't be measured, so it was stopped; try again" (it used to read "Daimon refused to start it"), "it used up its budget; ask for a bigger budget …" for the worker's own or the run's budget, "Daimon restarted before it finished" for a restart. A limit with no specific reason still reads "a spending limit was reached".
- Claude workers: the native tool schema now models `url_sources`, which Managed Agents echoes on a `web_fetch`/`web_search` config. A named agent's clone worker (J4 `fleet-claude`) passes its readback and runs; it used to fail as "a setup error on our side". The value is kept and sent back exactly as echoed, never set by Daimon, and only URL tools may carry it.
- Fleet: a Claude worker whose allocation readback echoes a tool field Daimon's strict native schema doesn't model is refused like any other readback mismatch (its allocated agent is archived), instead of failing as "a setup error on our side" with a raw validation error. The host logs `fleet.worker_readback_unrepresented` with the unknown field paths (names only).
- Fleet: a Stop that races a run's first worker spawn still leaves the run owing its end card. Ending a run now locks the run row first, so its check for a spawned worker sees a spawn that was committing at that moment.
- Hand-off and switch copy is accurate across providers: a hand-off says the conversation and files carry over only while the agent stays on its provider, and on Discord the switch card says "this conversation moves… earlier messages aren't carried over" for an agent this thread was handed to (never in a setup conversation, which doesn't move).
- Approval cards can be configured to wait up to 24 hours (`confirmation_timeout_s`, was at most one hour), but a card never shows or keeps a time past its turn's end: it expires just before the turn would. A card still open after 10 minutes pings its requester once in the thread ("This is still waiting for your click."). A card nobody clicked before it ended says "This request ended before anyone clicked. Ask again to redo it." A click after the turn has ended still can't run the request.

- Asking in chat to switch the server's default agent (the built-in Daimon, the deployment's default, or the server's own default) to another provider no longer changes it for everyone by accident. Unless the person says it's for everyone, the answer is "Switching Daimon changes it for everyone on this server. To try GPT just here, I can make a copy of me on GPT.", and nothing changes. Said for everyone, the owner or an admin switches it as before. Other agents switch as before. When two switches of one agent cross, the later one now says "Someone just switched Daimon; it's now on Gemini. Ask again if you still want GPT.", in chat, when the Confirm card would be posted, and on the card's button. A confirm call that drops "for everyone" posts no card.
- Fleet: a worker whose own setup (its policy and source reads, budget admission and backend check) gets stuck now fails within 45 seconds and reads "it took too long to start" on the fleet card, instead of sitting at "starting" until the run's deadline. Its budget-lock waits give up after 10 seconds. Each setup stage logs `fleet.worker_stage` with its outcome and time.
- Fleet: a worker whose own setup (its policy and source reads, budget admission and backend check) gets stuck, or waits on a busy budget lock, now fails and reads "it took too long to start" on the fleet card, instead of sitting at "starting" until the run's deadline. The setup has a time bound and its budget-lock waits are short; each setup stage logs `fleet.worker_stage` with its outcome and time. A root turn no longer holds the tenant's fleet lock while it rechecks its fleet grant, a run's budget watch takes that lock every 2 seconds instead of four times a second, a failed worker's record no longer waits out a busy lock: after a few short tries it is left to the deadline sweep, which ends the worker as past its deadline and lets the root turn continue, and the scheduler sweeps each tenant in its own short transaction.
- On the mux path, headless turns such as routines and smoke checks retry definitely refused provider writes within the caller's attempt allowance and turn deadline. A write whose response was lost after possible acceptance is no longer blindly resent by the SDK. Retry state stays in memory for that turn; this does not add routine restart recovery or stored transcript copies. Legacy is unchanged.
- Fleet: a worker running as an agent whose own model has native controls (for example effort) runs again; it was refused with a validation error before it started. A worker that fails because of a bug on our side now reads "a setup error on our side (ref <worker id>)" on the fleet card, never "it couldn't be set up on that model here". When a person names a model the fleet doesn't allow, the worker tools tell the agent to say so and ask which allowed model to use, instead of starting a paid worker on a substitute.
- Fleet card: every failed worker's line now names the cause and what a person can do about it, for example "this server is out of credit; an admin can top it up" or "this server doesn't allow that model provider; an admin can allow it, or ask for another model". A worker refused for being out of credit, over a spending or prepaid provider limit, on a provider that is still being set up, on a provider the server or channel doesn't allow, or by a timeout now records that cause instead of the generic `preparation_refused`/`dispatch_refused`. Those two generic codes no longer read "it couldn't start on that model here" or "it couldn't be set up on that model here": they say Daimon refused it and give a log reference for an admin. A Gemini worker that reaches its deadline reads "the model provider didn't answer in time (it may be busy); try again in a minute". A Gemini quota refusal says the server's Google plan doesn't include the model, or that Google is rate-limiting it for now. A worker refused at the account's hold cap reads "too many replies were already running on that model for this account; try again when one finishes". A worker someone stopped reads "it was stopped" (it used to read as a refusal), and a worker cut off by its own deadline, including after the turn ceiling, reads as running out of time.
- Fleet: a task worker on another Claude model (for example an Opus worker of a Sonnet agent) runs; it was refused when the provider filled in its default speed and effort. A refused worker no longer leaves its allocated agent behind. Every "can't run on that backend" refusal now records the place in our code that refused it, in the logs, and the fleet card says "this server isn't set up to run that model's workers yet" or "that model provider isn't set up on this server yet" when that is the cause. "Fork yourself across Opus, Astra and Argon" now means a fleet of workers (no admin needed), not a saved copy of the agent.

- Fleet: a worker running as an agent whose own model has native controls (for example effort) runs again; it was refused with a validation error before it started. A worker that fails because of a bug on our side now reads "a setup error on our side (ref <worker id>)" on the fleet card, never "it couldn't be set up on that model here"; a provider outage or provider-side error reads "the model provider returned an error or didn't respond". When a person names a model the fleet doesn't allow, the worker tools tell the agent to say so and ask which allowed model to use, instead of starting a paid worker on a substitute.

- A turn the model refuses now says so ("Daimon's AI declined this request. Rephrase it and @mention Daimon again.") instead of a bare "Stopped. Send a message to start again."; a refused routine run says the AI model declined it instead of "Something went wrong", and the MCP `ask` tool reports a refused turn at once instead of waiting out its timeout. The API ends a refused turn as an ordinary `end_turn` idle and reports the refusal only in `stop_details`, so these turns read as an empty answer. They are now recorded with the turn outcome reason `refused` (migration 0121) and log `turn.failed` with `turn_error_kind=refused` and the refusal category; the mux Anthropic driver reports them as an errored `turn_ended` with `native_reason: "refusal"`.
- On the opt-in mux turn path, an Anthropic request that is turned away with a 429 and a `Retry-After` header, or that never reached the provider (connection refused), is now retried a few times instead of failing the turn: never before `Retry-After`, within the turn's time limit, and stopping at once when the turn is cancelled or loses its thread. A request that may have reached the provider (timeout, dropped connection, 5xx, 529) is still never resent, and 408 and other unlisted 4xx responses now count as outcome unknown as well. Legacy is unchanged.
- GPT with zero setup: an agent's first GPT turn on a tenant whose runtime file has no OpenAI row creates the tenant's hosted OpenAI environment and every later turn reuses it; the code-default GPT agent template (alongside PR3's defaults) runs on it, capped at 50c per session. Concurrent first turns create exactly one (the mux operation store picks one sender). A create that fails or times out is never resent blindly: after 5 minutes it is looked up by name and only proven absence (a compare-and-set on the state that was read) lets it be retried. Until it exists the person reads "Couldn't set up GPT for this server yet. Try again shortly." A tenant's own row always wins (`daimon openai init` names its environment differently and its row is used instead), previews never create anything, the name carries the deployment so staging and production never adopt each other's, and logs never carry the key. Like the other code defaults it uses the deployment key's default project (no `OpenAI-Project` header).
- On the opt-in mux turn path, a message to a conversation that has moved now gets the same "This conversation moved" answer as on legacy, rather than a scope error. A caller outside the conversation's tenant or thread still gets the scope error.
- `daimon run` works on the opt-in mux turn path. On mux it refuses a session that another tenant owns ("session … belongs to tenant …; run it from that tenant") instead of creating a CLI identity inside that tenant. Sessions from `daimon sessions create` still run. Legacy is unchanged.
- On the opt-in mux turn path, routines, `daimon smoke` and workspace-transfer checkpoint turns now run: smoke passes its provisioned account, and a headless or checkpoint turn started inside another turn no longer inherits that turn's state and gets refused. A headless turn reads the turn path once and passes it down, and callers can pass it in. Nothing changes on the default legacy path.
- Fleet: a worker's model can be named the way people say it. `fleet_spawn`'s `model` and every `planned_models` entry accept plain names ("Opus 5.5", "Astra", "Argon") and resolve them to the granted ids. A name the fleet doesn't allow, or one that could mean several models, is refused in plain words with the allowed list, instead of a `not_subset` code. A `planned_models` list that names models by plain name no longer refuses every worker in the plan (an unknown name still refuses it, in plain words). `fleet_agents` now also lists the models a task worker may run on, by name, so a root no longer mistakes its worker agents for its allowed models. Every refused spawn is logged as `fleet.spawn_refused` with a category.
- A new read-only `list_models` tool answers "what models can I use?" and "what does luna cost?": each agent model on a provider this server has turned on, with its plain name, id, provider and list price. Models are named the way people say them ("Claude Opus 5.5", "GPT-6 Astra", "Gemini 4 Argon").

- An admin can stop anyone's runaway reply or fleet. On Discord, Slack and Teams, Stop on a reply's card works for the person who asked and for whoever is an admin when they press it (Discord: owner, Administrator or Manage Server; Slack: workspace admin or owner; Teams: an owner of the admin team). Anyone else is told "Only the person who asked, or someone with Manage Server, can stop this reply." (Slack says "a workspace admin", Teams "a Daimon admin"); when Slack or Teams can't confirm the clicker's role, they're asked to try again instead. On the fleet card, a refused Stop now says "Only the person who started this fleet, or someone with Manage Server, can stop it." (it used to point at a refresh button that doesn't exist); a finished fleet or worker, a Resume after the fleet ran out of time or steps, and a press still going through each say so, an out-of-date card asks to wait for it to update, and an outage asks to try again. Each accepted Stop is logged with who pressed it (`turn.stop_pressed`, `fleet.control_pressed`).
- An agent's owner can change it from the Discord, Slack and Teams setup panels, connect MCP servers through token forms and OAuth, and rotate its keys, as from chat. Whoever makes an agent from the panel's New agent button or copies one owns it; a channel's copy keeps the original's owner. Owners see their agent's setup, and "which agents do I own?" lists them (`list_agents` with `mine=true`).
- Fleet: a worker running as an agent created with `daimon agents create` (with model controls such as speed) no longer fails at dispatch. An agent on OpenAI or Gemini can be a worker: what that provider's worker can't carry (its native tools, skills, MCP, memory, sandbox, model controls) becomes a declared loss, together with the losses its owner accepted for that backend. The root may declare more with `agent={"name": …, "declared_drops": […]}`, and the spawn reply lists every loss. The "Fleet is still working" answer no longer lists worker states that went stale; the fleet card shows progress and ends in the run's final state.

- Admins can clear a channel's model-provider pin from chat: "clear this channel's pin" shows what the channel pins and clears it after the admin confirms on the card, so each agent answers there on its own provider again. Refusals and provider switches that mention a channel pin now give those words.
- Fleet: an agent whose own setup can't become fleet worker authority (for example a connected tool with its own login) no longer fails every turn once fleet is on. The turn answers with fleet off, the host logs `fleet.root_freeze_refused` with the cause, and the fleet tools say why in plain words. Turning fleet on for such an agent is refused up front with the same reason.
- Fleet: a worker can run as an agent that has never had a conversation. Its shared memory store is created the way its first session would create it, and the worker reads it read-only. A Claude worker that declares its memory dropped is refused at spawn, before any money is held, instead of failing at dispatch. When the Gemini hold cap is used up, the refusal now says so (`provider_hold_cap_reached`) instead of blaming a channel budget. The fleet card names an agent only for a worker that actually ran as it.
- Gemini (Flash and Argon) attempt holds are now released instead of piling up until every attempt was refused at the cap. An accepted attempt's hold settles to its measured token cost when the usage is recorded; a hold that never settles (lost response, 503 fallback, crash before usage) expires after 24 hours with a `provider_hold_expired` log; a 4xx still releases at once. `scripts/gemini_hold_report.py` shows each ledger's outstanding holds read-only.
- Gemini (Flash and Argon) attempt holds are now released instead of piling up until every attempt was refused at the cap. An accepted attempt's hold settles to its measured token cost when its interaction's final usage is recorded (a running snapshot never settles; Gemini usage observations now carry `final`); a hold that never settles (lost response, 503 fallback, crash before usage) expires after 24 hours with a `provider_hold_expired` log; a 4xx still releases at once. `scripts/gemini_hold_report.py` shows each ledger's outstanding holds read-only.

- Configure an agent's fleet from chat: choose worker agents by name, worker and spending limits, and an expiry, then confirm the exact settings. Owners or admins can change the fleet; only admins can change the server's provider allowlist. Natural model names such as “opus 5.5” and “luna” work when configuring an agent's model. Edits preserve every omitted limit and the saved expiry; confirmation cards name removed caps. Ambiguous model-name typos ask for a choice.
- Agents can use `gpt-6-astra` (OpenAI) and `gemini-4-argon` (Gemini) as their model, and a fleet can run such an agent as a worker. Astra is in the catalog at its published rates ($10 input, $50 output, $1 cached input, $12.50 cache writes per million tokens; a prompt above 272k tokens is not priced, so it is not admitted). The OpenAI host now runs the admitted model instead of only `gpt-6-luna`. Argon is no longer channel-only: a worker running Argon gets the same bounded token envelope as Flash, capped by its budget and the per-turn hold, and is never run on the 4096-token cap.
- Agents can use `gpt-6-astra` (OpenAI) and `gemini-4-argon` (Gemini) as their model, and a fleet can run such an agent as a worker. Astra is in the catalog at its published rates ($10 input, $50 output, $1 cached input, $12.50 cache writes per million tokens). The OpenAI host now runs the admitted model instead of only `gpt-6-luna`. Argon is no longer channel-only: a worker running Argon gets the same bounded token envelope as Flash, capped by its budget and the per-turn hold, and is never run on the 4096-token cap. A Gemini worker's explicit `max_input_tokens`/`max_output_tokens` must now also fit: if their worst-case cost at the model's tariff exceeds the worker's budget or the per-turn hold, the spawn is refused (Flash had the same gap). Astra usage above 272k input tokens is priced at OpenAI's long-context rates (2x input and cache, 1.5x output) rather than left unpriced, and an Astra session never runs without a native spend limit.

- Agent edits now reach an open self-hosted conversation at its next message: changed instructions, model, tools, skills or MCP servers apply on the same session, so workspace files stay. A turn already running finishes on its old configuration and its usage is metered at the model and tariff it used. Environment changes and archived agents still require a fresh session.

- Anthropic backend configurations that require `steer`, `native_event_replay`, `environments_fork` or `native_schedules` are now refused at admission; the Anthropic driver never supported them. The self-hosted profile now declares native settlement over model-request usage (`native_requests`) instead of native totals; recorded self-hosted usage and billing are unchanged. A new CI test checks every capability a profile declares against its driver.
- Anthropic backend configurations that require `steer`, `native_event_replay`, `environments_fork` or `native_schedules` are now refused at admission; the Anthropic driver never supported them. The self-hosted profile now declares native settlement over model-request usage (`native_requests`) instead of native totals; recorded self-hosted usage and billing are unchanged. A new CI test checks every capability a profile declares against its driver. The Anthropic driver now implements `reconcile`, projecting the session status and recent events into a snapshot; Daimon does not call it yet.
- When a turn refuses because of its model provider, the message now says why and what to do: the channel is pinned to a provider, the provider is not turned on for the workspace, or the agent runs on a provider that is not set up yet. It no longer blames the channel by default. Switching an agent's provider in chat now also says when this channel pins its own provider, so the switch will not apply there until the pin is cleared.

- Ship mux's offline test doubles and conformance kits as `mux.testing` inside `daimon-mux` (moved from `daimon.testing`), and run the mux tests in CI against an environment with only `daimon-mux` installed.
- OpenAI agents need no per-agent setup either: a tenant-wide OpenAI template row (`"root_materialization": true`) serves every agent switched to OpenAI, publishing each agent version's counterpart on its first turn, as Gemini templates already do.

- Fleet workers no longer need a runtime row per agent version and channel. One worker template row per tenant and provider (`"worker_materialization": true`, `"*"` source agent and version) lets any granted agent's workers run in any channel, and editing the agent no longer breaks them.
- Agents have an owner: the person who creates an agent in chat can change it without an admin, including its prompt, model, skills, connections, model provider and fleet. An admin can name or clear an owner with `daimon agents owner set/clear`; agents created earlier have no owner and stay admin-only. Owners can't change the provider allowlist, budgets or GitHub access, or turn on shared threads.
- An agent states its own thread mode and required capabilities ("make Researcher share threads"; an admin's call), from chat or `daimon agents backend settings`. A channel that pins a backend still overrides them, and refuses a turn rather than drop a capability the agent requires.
- Fleet: a worker can run as any agent on this server. `fleet_spawn(agent=<name>, task=…)` runs it with that agent's own prompt, model, tools, skills and memory, under the requester's permissions and inside the run's budget; `fleet_agents` lists the agents a run may use. A grant lists which agents its workers may be (stored grants keep today's behaviour: only the root's own agent), and the card names the agent each worker runs as.
- Fleet: a worker can run as any agent on this server. `fleet_spawn(agent=<name>, task=…)` runs it with that agent's own prompt, tools, skills and memory, on the model and backend its own agent config sets, under the requester's permissions and inside the run's budget and worker count; `fleet_agents` lists the agents a run may use, with their model and list price. A grant's `worker_agents` says which agents its workers may be: `"any"` (any agent the requester may use here), a list of agents, or absent (stored grants keep today's behaviour: only the root's own agent). An agent with tools that ask before running can't be a worker, since a worker has nobody to ask. The card names the agent each worker runs as.
- OpenAI and Gemini agents pinned to an exact runtime row now treat confirmation-gated and publishing Daimon tools (for example `publish_report` and the upload-URL tools) as unavailable, as template agents already did, and the MCP server refuses those tools for every OpenAI or Gemini turn credential. Such credentials are marked in the token registry (`mcp_tokens.surface = provider_turn`, migration 0120) instead of by label. Anthropic turns are unchanged.

- Fleet: whenever a worker is cancelled, the host logs why (`fleet.worker_cancel`, with a reason such as `shutdown`, `budget`, `usage_unavailable`, `run_deadline` or `terminal`), so a worker interrupted at send is no longer left without a cause.
- Gemini agents switched through a template row can keep Daimon's tools: with `"template_capabilities": "mcp"`, each turn gets a short-lived credential that acts as the person asking, is never written to disk, and is revoked when the turn ends. The agent's own tools and custom skills stay unavailable on Gemini, and the switch card says so.

- Gemini agents need no per-agent setup: one tenant-wide template row in the provider runtime manifest (`"root_materialization": true`) serves every agent switched to Gemini. Each agent version's counterpart is published on its first turn and reused after that. Channel and agent rows now serve only their own kind of turn.

- Fleet: a plain-language Gemini worker no longer fails with a provider 400. When the caller gives no token limits, a Gemini Flash worker gets a bounded envelope derived from its own prompt and budget (max input = the prompt's bytes plus headroom; max output = what the rest of its budget buys, at most 65,536 tokens), and an unbounded Gemini worker whose prompt clearly exceeds the 4096-token turn cap is refused at spawn instead of being sent.
- Switching an agent to another model provider can name the model: "switch agent to gemini-4-argon" (Gemini: gemini-3.8-flash or gemini-4-argon). The card names the model, says where the switch applies (every channel on the server for the default agent), and lists what won't be available, such as memory. Only where agent backends are on.

- Fleet: slow admissions taking turns on the tenant fleet lock no longer make the budget watcher cancel every worker in the run. While the lock is busy the watcher keeps checking every worker's spend from a lock-free read-only snapshot, logs which transaction holds the lock, and fails closed when one transaction holds it for 10 s or it stays busy for 60 s; a budget read granted late in its lock wait is no longer counted as a slow read.

- Fleet: a plain-language Gemini worker is no longer refused for its deadline. A worker's default deadline is now the sooner of the run's deadline and the Gemini worker runtime limit (120 s), and a later deadline is shortened the same way; Anthropic and OpenAI workers keep the run's deadline.
- Fleet: a grant whose per-provider caps add up to more than its run budget is refused when it is previewed or set ("fleet provider limits exceed budget"), instead of being accepted and then failing every spawn. `fleet_spawn` takes `planned_models`, the model of each planned worker, so each provider's cap is shared only by the planned workers on that provider; one worker per provider can use its whole provider cap. A plain-language Gemini worker's derived token envelope also counts a reviewer's embedded reviewed result and keeps its worst case within the deployment's per-turn hold.

- Fleet: a plain-language ask such as "compare X vs Y with 3 workers" now works. `fleet_spawn` takes a `task` and an optional granted `model` and fills the rest: the turn's run, the granted backend, an even share of the budget left per remaining worker slot (or per planned worker, when the root passes `planned_workers`), and a spawn refused at admission no longer uses up one of the run's worker slots, and the root's frozen deadline (a later deadline is shortened to it); an exhausted budget is refused before a worker slot is used. `fleet_status` and `fleet_collect` default to the turn's run. The root's own model is allowed in its fleet grant without being listed, and a worker on another model than the root's can declare that it runs without the root's model controls (such as effort); `task` workers do this automatically. Only agents with a fleet grant are affected; every grant cap still applies.
- Provider runtime manifest: a row for one channel with `"*"` accounts now wins over a deployment-wide `"*"` default for that channel, as documented, instead of both refusing as ambiguous. Rows that name different scope fields, or the same ones, still refuse.

- Fleet: a plain-language ask such as "compare X vs Y with 3 workers" now works. `fleet_spawn` takes a `task` and an optional granted `model` and fills the rest: the turn's run, the granted backend, an even share of the budget left per remaining worker slot, and the root's frozen deadline (a later deadline is shortened to it); an exhausted budget is refused before a worker slot is used. `fleet_status` and `fleet_collect` default to the turn's run. The root's own model is allowed in its fleet grant without being listed, and a worker on another model than the root's can declare that it runs without the root's model controls (such as effort); `task` workers do this automatically. Only agents with a fleet grant are affected; every grant cap still applies.

- A turn held to its channel (its requester is from another organisation, or the channel is shared with one) uses a memory of its own for that channel and never sees the agent's shared memory or anyone's personal memory; shared and personal memory never see the channel's. `/memory` in such a channel shows the channel's memory, and `/memory mine` and `forget-mine` say personal memory is not used there. A routine posting into a shared channel runs held the same way, and its result is never sent by direct message instead. A Teams `memory` answer for a shared channel is never moved to the 1:1 chat. A session that changes memory this way starts clean.

- Teams: people from another organisation are held to the channel they asked in and cannot use DMs.
- A turn asked by someone from another organisation, or in a channel shared with one, can be held to that channel (groundwork; Teams behaviour is unchanged).

- Slack: an external (Slack Connect) member's role is never taken from their own workspace, a mention from one is refused before anything is stored, and their queued follow-ups no longer run.

- Slack: people from another workspace in a Slack Connect channel can mention Daimon as plain users, held to that channel, and every turn in a shared channel is held to it. Re-apply the app manifest to subscribe `channel_shared`, `channel_unshared` and `member_joined_channel`.

- A conversation that moves to a different memory store starts clean: nothing from the old session is carried over, and the old session is not handed back while a turn is still running in it.

- **The "Working on it…" card shows more of the agent's progress.** On Discord,
  Slack and Teams it always keeps the agent's last three short updates, oldest
  first; `DAIMON_TURN__STATUS_LOG` now only decides whether an answer keeps its
  messages apart. Built-in tools add one safe detail to their line: a web search
  its query, a web fetch the host name, a file read, write or edit the file's
  base name; other tools stay name-only. Progress plans, off by default, let the
  agent keep a short checklist of a longer task's steps on the card: turn them on
  with `DAIMON_PROGRESS_PLANS__MODE` or per workspace, channel or thread with
  `set_progress_plans` (`get_progress_plans` reports why), under thread
  participation's cascade and permissions, Slack included. The card reads the
  latest updates first, then the plan under a Plan header, then the tool lines
  under Details, with plan steps drawn in code like the tool lines. Done and
  running items show ☑️ and ⏳, which stay visible on a dark theme. The default
  agent's prompt changes, so existing sessions restart once after the upgrade.

- Keep self-hosted tool cleanup independent of PID allocation counters and accept client cursor fences when remote recovery storage is empty.

- Isolate self-hosted checkpoint exporters from tool cleanup, retry interrupted exports, and keep sessions sealed when process termination cannot be verified.

- Keep self-hosted sessions sealed until failed tool cleanup succeeds, and retry journal reopen failures on subsequent requests.

- Reconcile remotely committed harness usage after a failed local commit, preserve
  measured WAL outcomes during updates, and reject cursors without sequence headroom.

- Attach shared selfhosted skill pins only when an authorized editor adds them; unrelated saves cannot authorize another editor’s unowned pins.
- Add an opt-in Codex app-server harness loop with Azure Responses metering and
  the existing tool approval and isolation boundary; keep the Agents SDK default.
  Bound denied calls and model continuations, disable bundled controller skills,
  and enforce one usage outcome before turn end. OpenAI live probes require
  explicit probe mode and refuse Azure configuration. Preserve marker-prefixed
  user input, preserve checkpoint compaction prompts and summaries, and honor
  cancellation or deadlines during controller shutdown. Meter internal compaction
  separately from the ordinary continuation budget.
- Preserve remotely committed usage during harness restart, retain measured responses
  across reservation outages, recover failed warning delivery, and replay completed
  turns with their original terminal event.

- Use `gpt-6-luna` for selfhosted test and smoke fixtures, refuse other live-test deployments, and reserve `gpt-6.1-sol` for production defaults.

- Reserve harness event sequences in remote blocks so stale cursors cannot reuse events after sandbox loss; retain measured usage through WAL acknowledgement failures.

- Persist each self-hosted session's highest client cursor and apply it to every event append, so stale polls cannot disable sequence fencing after restore.

- Count recovered self-hosted usage once, replay answers below receipt cursors after a restart, and flush pending checkpoints before closing shared credentials.

- Load operator bindings for correctly configured self-hosted session pools while still requiring explicit pool routing on environments.

- Honor saved allow rules during selfhosted tool inspection, and retain uploader provenance in the internal reader for already-retained skill versions attached to shared agents. This does not provide selfhosted uploads.
- Reject self-hosted session-pool credentials without pool routing and empty
  transport session IDs; document matching Entra authentication in the harness.
- Preserve measured self-hosted usage recovered from remote write-ahead records and
  deliver delayed checkpoint warnings on the next turn.

- Self-hosted harnesses durably record paid-work claims before model calls,
  preserve cleanup recovery cursors, and display checkpoint durability notices.

- Route selfhosted direct requests to Azure Container Apps session pools with a stable per-session identifier and the fixed Entra pool audience.

- A turn's `origin_context_id` no longer shows up in what Daimon lets the model read of another conversation. The id proves that a call comes from one live turn (on Teams it is what lets `microsoft_get` and `microsoft_send` reach mail, calendar and file links in the person's 1:1 chat). `list_session_events`, the agent-chat event reader and the earlier conversation quoted to a replacement agent after a handoff now replace it with `[redacted]`, in the controls at the top of each message and in tool calls. Before, a scheduled routine or a channel turn could list the owner's sessions, read the newest message of their 1:1 conversation and reuse the id. The turn's own prompt is unchanged, and normal use looks the same.

- An agent-wide `deny` tool rule on one of Daimon's own tools (`daimon-mcp/<tool>`) now stops the call. The MCP server refuses it, directly or through `call_tool`, when that agent's chat or routine session makes it and tool rules are enabled. The rule used to be saved but never enforced for Daimon's own tools. An agent-wide `deny` on `daimon-mcp/*` denies all of Daimon's own tools for that agent, tools added later included, and `search_tools` and `call_tool` too; wildcards are recognised only for `daimon-mcp`, agent-wide and `deny`. The turn driver applies `ask` and channel rules on them (see Fixed).

- **A Discord role taken away ends channel admin rights at once.** Outside a
  chat turn (MCP calls, the hub, a connector sign-in, budget notices and ask a
  human DMs), a channel admin matched by a stored Discord role counts only
  while Discord still lists the role on them, read per member and cached for a
  minute. A failed read grants nothing. Before, the stored role stood until
  their next turn.
- **Agents that only routines or threads run count as shared.** Editing an
  agent's prompt or setup, or binding a repo to it, now reads sharing as
  widely as a key change: a bound thread, someone's personal default, or
  another member's routine or live session makes the agent shared, not only
  a channel or workspace default. A member can no longer change the prompt
  an admin's routine runs; the routine's creator and admins still can.
- **Admin panel writes are audited.** Changing a channel's isolation, admins
  or environment, minting or revoking an access token, and redeeming a promo
  code from the Discord, Slack or Teams panels each record a
  `security_audit_events` row with tool name `panel:<op>`, whether it was
  allowed or refused. Rows carry no codes or token values.
- **MCP access refusals are audited as denials.** MCP calls refused by an
  access decision (an agent pin, channel isolation or protection, the
  invoker allowlist, a routine destination, a channel default binding, a
  thread handoff or an environment pick) are now recorded in `security_audit_events` as denials,
  with the action as the operation and `authz:<reason>` as the reason.
  Before, they were recorded as tool errors with no operation.
- **Teams routines stay inside an isolated channel.** A Teams routine
  posting into an isolated channel no longer falls back to its creator's 1:1
  chat when the post fails, matching Discord and Slack: the result is
  skipped and stays inside the channel.
- **A turn's credential keeps to its own turn origin.** A chat turn's
  credential can now act only on its own responder's turn origin in the
  tools that require one (credential requests, publishing, repo binding,
  task continuity, timers, channel budgets and setup targets), as agent keys
  already could. Before, it could name another responder's origin on the
  same account.
- **Routine sessions carry their channel's seal.** A routine's session is
  now stamped with the channel it fires into and the seal over it, as a chat
  turn there is (a routine with no destination, with its saved channel). The
  transcript of a sealed or isolated channel's routine can no longer be read
  from outside that channel, including by the routine's owner (a server
  admin owner still reads it from the hub, as with their own DMs). The
  session is also stamped private to its owner, so no server admin or
  channel admin can read it from the hub, as before.
- A thread handoff is decided by the access policy at the moment of the switch, under the tenant's policy lock, with protection, the invoker allowlist, pins, isolation and channel admins applied. A policy edit made first refuses it and one made during it waits. The new session keeps the old one's seal and read-only memory rules, and the old session's work is carried over only when the new agent could read it from that thread.
- A private form's pinned-agent check now runs in the same transaction that spends the form, on Discord, Slack and Teams, so a pin committed after the earlier check still refuses it and the form stays unspent. That transaction holds the tenant's policy lock from the check until the form is spent, so a pin edit made meanwhile waits and applies to the next form. The routine fire and delivery checks (protected destination, the creator still on the invoker allowlist), the protection of a turn's own notices, and the shared-agent replace/remove table now all go through the one access decision. They decide exactly as before.
- The action-time access checks now run at the last moment before each effect: right before a session is created (after the predecessor lookup and environment, vault and memory setup), after a dead-session recovery waits for the preparation lock (a seal added meanwhile defers adoption so the session is rebuilt read-only), after an agent chat resolves the agent's names, and inside the MCP OAuth vault write and server attach. A refused sign-in's saved grant is removed, retried once, and logged by id if it can't be. A DM whose source was sealed during a session replacement or recovery is closed instead of reporting a failed preparation. Session preparation decides again once it holds its lock, so a pin or seal that lands while it waits stops a checkpoint from running on the old writable session. A recovery adopts another turn's replacement only when that session is already read-only and carries the current seal, and it decides again right before the recovered message is sent. An agent-chat `start_turn` decides again right before its session is created. Every turn's first message, and a workspace checkpoint's message into the old session, is decided again right before it is sent, after the stream opens: a pin added meanwhile refuses it, and a seal makes the turn wait so the next one is prepared read-only (Discord says to send the message again).
- Access is decided again at the moment of action, not only when a turn is admitted. Before a session is found, reused, replaced or recreated after a crash, the turn's pin, protection and invoker checks are re-run on the current access policy, so a pin or protection added in between refuses it, and a seal added in between is stamped on the session and makes memory read-only. The check runs again right before a replacement session is created after a workspace transfer, and agent-chat and hub turns re-check right before they create a session or send a message, resumed sessions included. A DM is closed if its source channel was sealed since the turn started. The MCP OAuth callback checks the pinned-agent rule after the code exchange, again before the grant is saved and again before the server is attached, so a pin that lands mid sign-in stores no grant and attaches nothing. An admin reading their own DM sessions from the hub reads them as before. `daimon agents rekey-guild-ownership` keeps a report reader's link to its source agent. A published report's reader variant now counts as its source agent for pins, and publishing a pinned agent's reader needs an admin or a request from inside its channels (`publish_report(origin_context_id=…)`). An agent-scoped key is never exempt as an admin, whoever minted it.
- **Seeded defaults are protected from chat edits.** Skill-repo imports now
  gate like other attachment writes: a member needs an admin on an agent
  that answers for others, and neither the import nor a push resync
  attaches to a defaults-managed agent, even for admins. An import may not
  reuse a seeded skill's name, and only an admin import may replace an
  existing library skill. `delete_skill` refuses seeded skills, and
  `update_environment`/`archive_environment` refuse defaults-managed
  environments. The skill-import card names what did not import or attach.
  Apply frees the names of retired default skills. `sync_skills` outcomes
  gain an optional `refusal` reason. Operators can still delete a seeded
  skill with `daimon skills delete`.
- Admins are trusted: pins and seals protect members and channels, not admins. An admin is exempt from an agent's channel pin in a DM (Teams personal chats included) and in their own hub turns (`ask`, `start_turn`, `continue_turn`), where the reply reaches only them, and may list and read anyone's sealed conversations from the hub; continuing a sealed channel conversation from the hub is refused for everyone ("continue it in its channel"). Credential and configuration tools on a pinned agent are open to an admin's chat turn. Pins still hold for admins in channels, threads, handoffs and routines, and `fork_agent` still refuses a pinned source. On every turn, wherever a pinned agent runs, its sends on Discord, Slack and Teams reach only its pinned channels, the requester's own DM with daimon (Slack IM or Teams personal chat), and direct messages to the requester. Every DM session is stamped private, so admins never read another member's DM (Slack, Teams or `/dm`) from the hub. A hub caller counts as an admin by the account's stored role, which the person's next platform turn refreshes. Agent-scoped keys and tokens with no platform user are never admins, and a pin now binds a signed bearer with no platform user too (previously it skipped admission entirely). A DM closed because its source channel was sealed now says so plainly. `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` can be stored as agent keys by anyone; every other `GIT_*` name is still refused.
- **`DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS` is removed.** Setting it
  to `false` made every caller in a Discord thread share one agent session,
  minted for whoever created it: anyone in the thread acted with that
  person's connected accounts and admin rights, and several tools refused
  everyone else. Each caller now always gets their own session, as the
  default already did. Unknown keys are ignored, so a deployment still
  setting it boots normally.
- Each marimo notebook now requires its own access token. The link the host returns carries it, and a notebook's code can no longer open another notebook by curling its localhost port or reusing a slug it read from `ps`. Scratch notebooks are read-only apps; the editor needs `create_notebook_upload_url(editable=True)` on a deployment that sets `DAIMON_NOTEBOOK__ALLOW_EDITABLE`, and switching a slug between read-only and editor issues a new link. The host also needs its own `DAIMON_NOTEBOOK__ALLOW_EDITABLE` to serve an editor, and `PUT /admin/notebooks/{slug}` is read-only unless it asks for `editable`. The host kills every process of a notebook's uid before reusing or respawning it, quarantines a uid it can't clear, and deletes the uid's files in `/tmp` and `/dev/shm`. Jailed notebooks run with no-new-privileges, a process cap and a private `TMPDIR`, and switching a slug between editor and read-only wipes its home and workspace. Tokens are kept out of marimo's log and redacted from the host's logs, marimo is pinned exactly, and the host refuses to boot with plain-http links off localhost. With `DAIMON_NOTEBOOK__ORIGIN_BASE` (wildcard DNS and TLS), each notebook gets its own origin, `https://<label>.<origin_base>/`, and the host routes by Host, refuses cross-origin requests and WebSockets, and keeps cookies host-only. Without it, notebooks share one origin, so a public host admits only the tenants its operator lists in `DAIMON_NOTEBOOK__TENANTS`, for one operator's own Discord server, Slack workspace and Teams tenant; any other tenant gets a 403 naming its id. Over https, notebook cookies are `__Host-` prefixed and `Secure`, and HSTS is sent. The slug root is owned by the host and the host never follows a symlink the notebook planted when it chowns, writes or logs. httpx request logs are silenced and every log line is redacted after formatting. The Fly example sets up per-notebook origins.
- `/dm` now refuses in a sealed channel or a Discord thread under one, before reading any history. It used to copy the last 12 messages into a DM that sits outside the seal. On Slack, a thread sealed on its own is dropped from the copied history instead. An existing DM whose source channel, thread or (on Slack) any copied thread is sealed later ends on its next message: the conversation and its copied context are deleted and its sessions retired. DMs started before this release end as soon as the workspace seals anything. Run migration `0032_dm_source_ids` before deploying.
- The Slack `/routines` panel lists only your own routines unless you are an admin. `daimon agents fork` refuses a pinned source and leaves token-backed MCP servers off the copy, like the chat tool. The unused panel fork helpers and credential-copy stores are removed, and member-facing copy no longer points members at `fork_agent`, which is admin-only.
- Cross-agent protection is complete only for pinned agents: pin every client project agent. `decide_handoff` now requires the caller's admin status and the channel's agent explicitly.
- Pins can no longer be dropped by accident: removing an agent's last channel by id, naming an agent in both add and remove, mixed remove forms, unknown or look-alike agent names, Slack DM ids and `--clear` without `--replace-pins` are refused, and every newly unpinned agent is printed. Members can't add keys or connectors to a pinned agent from outside its channels (checked at request and at submit). DM conversations are outside every pin for handoffs and continuations, and `/dm` refusals show readable copy.
- `fork_agent` is admin-only, refuses to copy an agent pinned to channels, and a fork now starts with no credentials: no GitHub access, repo binding or proof of access, and no agent-wide MCP token. MCP servers that only work with a stored token are left off the copy. Previously any member could fork another project's agent and walk off with its repo access and connector tokens.
- A member can no longer operate another project's agent from outside its channels. MCP and hub turns (`start_turn`, `ask`, `continue_turn`, new or resumed) refuse a pinned agent. `hand_off_task` to an agent other than the channel's own, and `create_routine`/`update_routine` for an agent other than the one you are talking to or the destination channel's, now need an admin. Routines are listed and read only by their creator and admins.
- Operators can pin an agent to named channels (`daimon tenants access-policy set --add-pin-agent AGENT=CHANNEL_ID`, `--remove-pin-agent AGENT[=CHANNEL_ID]`). Pin edits keep every other agent's pin, and `--pin-agent`, which replaces the whole map, refuses to drop an unnamed agent's pin without `--replace-pins`. A pinned agent refuses turns anywhere else, including DMs and threads handed to it from other channels, and its routines must post into a pinned channel. Agents without a pin are unchanged.
- Agent keys are no longer stored in plaintext by default. Without `DAIMON_CRYPTO__KEYS`, saving one is refused (`request_agent_key` refuses before posting a form, and the form says why) unless `DAIMON_CRYPTO__ALLOW_PLAINTEXT=true` opts in for local development. The session factory logs `agent_env.encryption_keys_missing` at error level at startup.
- New `daimon crypto verify` (fails when keys are missing, any agent key is stored in plaintext, or an encrypted key can't be decrypted with the current keys; counts per tenant only) and `daimon crypto encrypt-plaintext` (encrypts legacy plaintext rows in place).
- `self_read_file` and `self_list_files` no longer return any agent key value, only keys and metadata; the sandbox `.env` still carries them.
- Credential submissions no longer log any part of tokens, PATs or credential request tokens.
- Make the mounted agent `.env` inert under `set -a; source`: any value bash would expand, substitute or split is now single-quoted (double-quoted only when it holds a line break), so a key value such as `$(curl …)` set by any member no longer runs code in the agent's sandbox.
- Refuse key *names* that are themselves a capability, in two structural layers rather than a fixed list. Hard-denied for everyone (including admins), and filtered out of the mounted file even for rows stored earlier: any name that controls an interpreter, archiver, loader, locale, package manager, git, an HTTP client or a CA bundle, or that redirects an SDK's own traffic — recognised by exact names plus prefix/suffix classes (`*_OPTIONS`, `*OPTS`, `*_COMMAND`, `*STARTUP`, `*RC`, `*_PATH`/`PATH`, `*_PROXY`, `*_CONFIG`, `*_PRELOAD`, `*_BASE_URL`, `*_ENDPOINT`, `*_INDEX_URL`, `LD_*`, `GIT_*`, `LC_*`, …). This closes `TAR_OPTIONS`, `BASH_ENV`, `PYTHONSTARTUP`, `GIT_SSH_COMMAND`, `LESSOPEN` and their kind as cross-agent execution routes. Non-admin members may additionally only add a *secret* name — `^[A-Z][A-Z0-9_]{1,63}$` ending in `_KEY`, `_KEY_ID`, `_TOKEN`, `_SECRET`, `_PASSWORD`, `_PASSPHRASE` or `_PAT` (plus `GH_TOKEN`/`GITHUB_TOKEN` and bare words like `TOKEN`) — never an identity or targeting name (`*_USER`, `*_ID`, `*_EMAIL`, `*_ORG`, `*_PROJECT`, `*_REGION` …) or a `*_URL`/`*_HOST`; an admin may add any name that is not hard-denied. Language and build loaders (`R_*`, `JULIA_*`, `LUA_INIT*`, `JUPYTER_*`, `PYTEST_PLUGINS`, `*FLAGS`, `CC`/`LD`, `CMAKE_*`), libc and TLS controls (`GLIBC_TUNABLES`, `MALLOC_*`, `OPENSSL_*`, `NODE_TLS_REJECT_UNAUTHORIZED`, `SSLKEYLOGFILE`), config directories (`XDG_*`, `*_CONFIG_DIR`, `GNUPGHOME`, `PG*FILE`) and endpoint overrides (`AWS_ENDPOINT_URL*`, `GH_HOST`, `GITHUB_API_URL`, `CLOUDSDK_API_ENDPOINT_OVERRIDES_*`, `TF_CLI_*`, `HELM_*`, `CONDA_*`, `BUNDLE_*`, `POETRY_*`) are hard-denied; `NPM_TOKEN`, `CARGO_REGISTRY_TOKEN`, `UV_PUBLISH_TOKEN` and `DOCKER_PASSWORD` are exempt as opaque secrets. `self_write_file` now stores only secret-named keys (every key it writes is exported into the sandbox `.env`), so it no longer accepts file-like keys such as `config.yaml`. Enforced at `request_agent_key`, the Discord and Slack forms and uploads, and `self_write_file`; the store and the `.env` assembler apply the hard-deny layer under all of them.
- Session transcript tools now honour sealed channels. `list_sessions`, `get_session` and `list_session_events`, agent chat's session tools (`list_my_sessions`, `get_my_session`, `list_events`, `continue_turn`, `ask` with a handle, and the rest), and the hub's equivalents no longer list, return or continue a conversation that ran in a sealed channel unless the call comes from a turn inside that channel. New sessions record their channel and thread; older channel sessions are hidden outside their own thread while the tenant seals anything. Agent chat's `start_turn`, `ask` and `continue_turn` also refuse a chat turn's own credential, so a sealed turn can't open or continue another session. A conversation that ran sealed stays sealed after an unseal, including a session that replaces it (transcript, checkpoint, bundle, handoff or recovery), and its own thread can still read it.
- Connecting an MCP server through `request_mcp_token` or `request_mcp_oauth` can no longer replace one the agent already has unless an admin does it, when the agent is shared (a default somewhere) or defaults-managed. Replacing means repointing an existing server name at another URL, or pasting a new token for a URL that already has the agent-wide token. Checked when the card is requested, again against the person submitting, and once more by the attach itself. New server names are unchanged for members. A pasted MCP token is published as the agent-wide credential only after the server is attached and re-checked, under one per-agent lock that the OAuth callback, `attach_mcp_server` and `update_agent` also take, so no other session can mirror a refused token. A failed attach now stores nothing.
- Key names: hard-deny more exec-on-env names (`*_RSH`, `*PASSCOMMAND`, `SVN_SSH`, `CONFIG_SITE`, `GCC_EXEC_PREFIX`, `CCACHE_PREFIX`, `RUSTC_WORKSPACE_WRAPPER`, `RUSTDOC`, `COR_PROFILER*`, `CORECLR_*`, `DOTNET_ADDITIONAL_DEPS`, `LUA_PATH*`/`LUA_CPATH*`, `CLOUDSDK_PYTHON`, `COVERAGE_PROCESS_START`/`COVERAGE_RCFILE`, `NODE_REPL_EXTERNAL_MODULE`, `PYENV_VERSION`, `LESSKEYIN`/`LESSKEY_SYSTEM`/`LESSEDIT`/`MANROFFOPT`, `AS`/`NM`/`RANLIB`/`STRIP`/`FC`/`MAKE`/`MAKESHELL`, `GOINSECURE`/`GONOPROXY`/`GODEBUG`, `FCEDIT`, `ANSIBLE_VAULT_PASSWORD_FILE`). Path-valued names — `*_FILE`, `*_CLIENT_KEY`, `ETCDCTL_KEY` — are admin-only. Adding a name a tool reads as the same credential as one already stored (`GH_TOKEN`/`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`, `AWS_SESSION_TOKEN`/`AWS_SECURITY_TOKEN`, `OPENAI_API_KEY`/`OPENAI_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`) now counts as replacing it: single keys go through the replacement gate and `.env` imports refuse it, naming both keys. The keys a turn is told about no longer include rows the mount skipped. `self_write_file` is add-only: an agent key can no longer overwrite a stored key or add an alias of one. `self_delete_file` is refused on the built-in agent and on any agent reachable in the workspace (as `remove_agent_key` is for members), so delete-then-add can no longer replace a key. Key writers take a per-agent lock before reading key names, so two concurrent writers can no longer add both names of an alias pair, and a `.env` file that contains both names of a pair is refused. Teams key submits now apply the same alias gate, in-transaction re-check and per-agent lock as Discord and Slack. Key and MCP-server replace and remove decisions (`remove_agent_key`, `detach_mcp_server`, MCP replacement, the setup wizard, every key form and `self_delete_file`) now treat an agent as shared when any of its names (display or `daimon_name`) is a channel, tenant or deployment default, when a live handoff or setup thread is bound to it, when it is anyone's personal default, when an enabled routine someone else created runs it, or when another account has a live session with it. Admins can again rotate one key of a stored AWS family; a related key added or removed after the gate still makes the write stale. The AWS access key, secret and session token are a credential family rather than aliases: a fresh agent may import them together, and adding to a stored family counts as a replacement. Alias groups also cover `GITLAB_TOKEN`/`GLAB_TOKEN`, `FLY_API_TOKEN`/`FLY_ACCESS_TOKEN`, `NPM_TOKEN`/`NODE_AUTH_TOKEN`, `AWS_SECURITY_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`, and are re-checked inside the write; the refusal names both keys. `VIMINIT`/`EXINIT` are hard-denied; `*_SSL_KEY`/`*_TLS_KEY` are admin-only.
- uvicorn access and error logs (MCP server, Teams adapter, report host) no longer print capability tokens in request paths (`/uploads/{token}`, `/slack/file/{token}`, report `/upload/`, `/publish/`) or any query value (OAuth `code`/`state`, `?k=` reader links); `httpx` request logging is quieted to warnings. The JSON and CLI log chains redact credential text in exception text and string fields before rendering, and Slack key-save failures log the error type only.
- Sentry no longer captures frame local variables or breadcrumbs, and `before_send` drops any that arrive and redacts secret-shaped text in exception messages and contexts, so decrypted agent keys and submitted form values can't reach error reports. Agent key values, `.env` entries, credential-request tokens and OAuth flow secrets are hidden from reprs; SQL errors (app and migrations) no longer carry bound parameters; CLI tracebacks no longer print locals; a malformed `DAIMON_CRYPTO__KEYS` error no longer echoes the keys. Outbound HTTP span `http.query`/`url.query` values are redacted whatever the parameter is called, and fragments dropped. Requests in error and transaction events keep only scheme, host and path in the URL (with capability tokens in paths such as `/uploads/{token}`, `/slack/file/{token}` and Discord webhook URLs replaced, and any credential-looking path segment redacted, in URLs anywhere in the event) and parameter names in the query (OAuth `code`/`state` included); credential headers, cookies, bodies and environ are dropped or redacted; JSON, dict-repr and quoted `key: value` secrets, `NAME_TOKEN=value`-style pairs (quoted values to the closing quote, unquoted to the next space), `--password value` flags and argv lists, escaped JSON, Slack (including `xapp-`), GitHub, Anthropic/OpenAI, JWT and Discord-webhook token shapes, form-encoded pairs (only `%26`/`%3D` are decoded), `Authorization` schemes, URL userinfo and query values, and pydantic `input_value` echoes inside exception and log messages are redacted, in time linear in the text (capped at 64 KB per string and 256 KB per event); a scrubber failure sends a stripped event instead of the original. User data is never sent.
- Encrypt agent environment values with rotatable deployment keys when configured, including existing rows on upgrade. Store encoding separately from user text so every literal value, including `enc:v1:` prefixes, remains valid. A database trigger keeps writes from older code tagged as plaintext; decryption errors identify the affected row without exposing values.

- Sentry's event scrubber checks nested values, so a token inside a dict local,
  such as request headers, no longer ships with an error event.
- Slack top-ups sent the checkout route an internal admin token for the
  clicking account. They now send a plain account token, as Discord does;
  the route only needs the account.
- Text daimon quotes from outside the request now arrives marked as data:
  replayed thread and channel messages on Discord and Slack sit inside
  `trust="untrusted"` envelopes, YouTube transcripts come back wrapped, and
  the channel read and search tools mark their results. Every agent's
  guidance block gains a paragraph saying such text is data, not
  instructions; agents pick it up at their next reconcile or edit.
- GitHub App installation tokens are minted for the one bound repository
  instead of every repository in the installation, and read-only when the
  binding was verified as a public repo or the token is used for skill sync.
  If you bound your own public repository with `bind_public_repo` and the
  GitHub App is installed on it, the agent can no longer push to it through
  that binding: its token is now read-only. To let the agent push, rebind the
  repository with a personal access token (the repo credential form in Discord
  or Slack).

### Removed

- **Teams treats people from another organisation as members.** A shared
  channel's external participants and Entra guests are answered wherever a
  member is, with the same commands, cards, tools, credentials and sessions,
  as everyone in a Discord server already is. Group chats are answered
  whoever is in them, and a guest owner of the admin team is an admin. Teams
  still opens no 1:1 chat across organisations, so one naming another tenant
  is refused, and a command answered there tells them so instead of pointing
  them to it. `DAIMON_TEAMS__RESTRICT_GUESTS`,
  `DAIMON_TEAMS__RESTRICT_EXTERNAL_PARTICIPANTS` and `daimon tenants
  access-policy --add-member-guest` are gone. Slack Connect is unchanged.

- **`DAIMON_TEAMS__ADMIN_USER_IDS` is no longer read.** Make those admins
  owners of the admin team; a leftover value is ignored, and anyone not an
  owner loses the stored admin role at the next owner read or restart.

### Upgrade notes

- Teams: leftover `DAIMON_TEAMS__RESTRICT_*` values and stored member guest lists are ignored. Uploading the rebuilt app package (0.6.1) is optional; it only drops the unused `ChannelMember.Read.Group` permission. The `is_external` columns are no longer read or written and are dropped in a later release.
- Teams Ask a person: a deployment that put a Teams channel (`19:…`) in `DAIMON_SUPPORT__ESCALATION_CHANNEL_ID` moves it to `DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID`; until then Teams offers no Ask a person, and Discord ignores the Teams id.
- Building the image needs Docker 23 or later: the Dockerfile uses BuildKit instructions the legacy builder rejects.
- Run migrations `0046_account_external` and `0047_turn_origin_external` before deploying.
- Isolated channels' agents no longer read other channels, nor sessions with no channel stamp: setups that relied on it stop working.
- Deployments that set `DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS=false`: each caller in an existing thread starts a fresh session of their own at their next message; the shared session is not carried over.
- Notebook links change once: after the notebook host upgrades, links shared earlier (scratch notebooks and blogs) stop working. Blogs get a token on their first respawn; `list_notebooks` returns the new links. Upgrade the notebook host before the bot. A host with a non-localhost `DAIMON_NOTEBOOK__PUBLIC_HOST` and no `https://` `DAIMON_NOTEBOOK__PUBLIC_URL_BASE` no longer boots; set one, or `DAIMON_NOTEBOOK__ALLOW_HTTP_LINKS=true` on a trusted private network. Editors need `DAIMON_NOTEBOOK__ALLOW_EDITABLE=true` on both the bot and the host. A public notebook host without `DAIMON_NOTEBOOK__ORIGIN_BASE` refuses every upload until `DAIMON_NOTEBOOK__TENANTS` lists the tenant UUIDs it may serve (comma-separated or a JSON array; `daimon tenants list --json` shows them, and each refusal names the id), and refuses tokens that name no tenant, so upgrade the bot with it. Set `ORIGIN_BASE` (wildcard DNS `*.<domain>` and a wildcard certificate) to serve any tenant in isolation. A `tenant.json` left in the data directory by a pre-release build is ignored and can be deleted.
- Set `DAIMON_CRYPTO__KEYS` before upgrading: keyless deployments still start and read existing values, but refuse new agent key writes unless `DAIMON_CRYPTO__ALLOW_PLAINTEXT=true`, including the agent's own `self_write_file` notes (the tool error names `DAIMON_CRYPTO__KEYS`). `DAIMON_CRYPTO__KEYS` now also accepts the raw `Fernet.generate_key()` output or a comma-separated list; before this, only a JSON list parsed and a bare key stopped every service at boot. After setting keys and restarting **every** process with them, run `daimon crypto encrypt-plaintext`, then `daimon crypto verify`. Stop old readers/writers before the migration when enabling encryption. Keep keys available for reads and reversible downgrade; see `docs/self-hosting.md`.
- Agents whose keys hold shell metacharacters get a re-quoted `.env` once, so those sessions pick up a fresh mount on their next turn. Keys already stored under a hard-denied or non-identifier name stop being exported (an admin-set `DATABASE_URL` still mounts — only tool-control names are dropped); each skipped name is logged as `credential_env.row_skipped` and still shows in `list_agent_keys` so it can be removed or re-added under another name. If you suspect a name like `TAR_OPTIONS` or `LD_PRELOAD` was set on a shared agent before this release, refresh that agent's sessions and rotate any credential the agent could have reached.
- Audit what members set up before the cross-agent fixes: `list_routines` as an admin for routines whose agent is not the one their destination channel answers with, and thread handoff bindings to agents from other projects. These keep working until removed.
- `add_skill` from chat waits on a confirmation card, which only tool safety shows, so with it off a chat add points to the setup panel's Add skill. A session started before this release, or before tool safety was turned on, picks up the card on its next message. Routines and other unattended runs never add a skill.
- Agent environment encryption is opt-in through `DAIMON_CRYPTO__KEYS`; keyless deployments retain plaintext storage and initialization still succeeds. Stop old readers/writers before the migration when enabling encryption. Keep keys available for reads and reversible downgrade; see `docs/self-hosting.md`.
- The seeded agents move to Sonnet 5.5 on the next defaults reconcile, so each existing `daimon` and `dev_agent` thread replaces its session on its next message, with one checkpoint turn on the old session if it had replied. Reports and spend caps reprice history at read time, so this month's Sonnet 5 and Opus 4.7 spend drops at once; set `DAIMON_BILLING__MARKUP` if the old rates stood in for a margin.
- Teams: run migration `0038_teams_parity` before deploying. The MCP server finds a team once the bot sees activity there, so a team the bot joined earlier is readable by channel tools after its next message.
- Routines now run in their channel's environment, then the workspace default, instead of always the deployment default. A channel or workspace `environment_name` already set with `daimon config set` moves those routines onto it from their next fire; `daimon config unset` it first to keep them where they were.

## [0.2.0] - 2026-09-21

Everything since the first release. Slack catches up with Discord across
setup, credentials, feedback and file delivery; conversations survive a
restart and can hand work to a fresh session; charts and published reports
get a delivery path of their own; and self-hosting is documented end to end.
140 pull requests, 23 schema migrations, two new workspace packages.

### Breaking

- **`POSTGRES_PASSWORD` has no default.** Compose refuses to start without
  it. Set it in `.env` before upgrading.
- **Postgres and the notebook host publish on `127.0.0.1` only.** Anything
  that reached either from another host now needs a tunnel or its own
  published port.
- **The scheduler starts through the `daimon-scheduler` console script**,
  not `python -m daimon.adapters.scheduler`. Compose is updated; custom
  process definitions are not.
- **MCP tools `generate_image` and `generate_audio` are removed.** The
  `pydub` dependency went with them.
- **MCP tools `create_blog_upload_url`, `delete_blog` and `list_blogs` are
  removed.** Notebooks are one tool plus `list_notebooks` and
  `delete_notebook`; published documents use `publish_report` and
  `delete_report`.
- **The duplicate `skills_*` aliases are removed.** Use `sync_skills`,
  `list_skills`, `get_skill` and `delete_skill`. No compatibility aliases
  are kept.
- **`DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN` is removed.** It made the admin
  check return true before `users.info` was called, opening every Slack
  admin gate for every member of every install on the deployment. Unknown
  keys are ignored, so a deployment still setting it boots and starts
  enforcing; promote a real workspace admin for any account that relied on
  it.
- **The seeded skill `marimo_blog` is gone**, replaced by
  `marimo_notebooks` and the report skills.
- **The root `compose.notebook.yml` and `compose.worker.yml` are deleted.**
  `docker-compose.yml` is the supported stack.
- **Changed defaults:** agent model `claude-sonnet-4-6` → `claude-sonnet-5`;
  `DAIMON_BILLING__SIGNUP_CREDIT` 5.00 → 10.00;
  `DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S` 600 → 3000, an outer process guard
  that now sits above the core's ~45 minute turn ceiling rather than below
  it; `DAIMON_PRIVACY_POLICY_URL` points at this repository's `PRIVACY.md`.
- **Minimum `anthropic` SDK is 0.117** (was 0.96), and the root
  distribution is named `daimon` (was `daimon-cma-open-source`).
- Upgrading runs **23 migrations**. Every one declares `downgrade: safe`,
  and none drops a table or column.

### Added

#### Agent and sessions

- Session continuity: a thread's work survives the session behind it being
  replaced, through snapshots, replacement lineage and continuations queued
  and dispatched back into the thread.
- `start_fresh_task` and `hand_off_task` — start clean, or move the current
  task to a new session with its context.
- `cancel_turn` stops a running turn; `get_turn_cost` reports what one cost.
- `explain_agent_resolution` answers which agent replies in a channel and
  why.
- Per-agent memory stores, with a `daimon memory` CLI group.
- Opus 5 in the pricing table; the agent model allowlist is scoped to
  Anthropic models.

#### Discord

- Structured mid-turn forms with typed answers (`post_wizard`).
- Reaction feedback votes with a private free-text follow-up.
- Opt-in organic thread participation, off by default
  (`DAIMON_THREAD_PARTICIPATION__*`).
- Automatic thread titles and `rename_thread`
  (`DAIMON_THREAD_NAMING__*`).
- `set_display_identity`: the agent can change its own nickname and
  per-server avatar when an admin asks.
- Human-support escalation, metered per user (`DAIMON_SUPPORT__*`).
- Nominated QA bots may start turns by mention
  (`DAIMON_DISCORD__QA_BOT_USER_IDS`).
- A read-only setup panel listing the agent roster, details and routing.

#### Slack

Most of this release's parity work: Slack now matches Discord on the
surfaces below.

- Feedback votes on the final answer, chat-initiated credential buttons and
  single-use private modals.
- Output file delivery (requires the `files:write` scope).
- `send_message` posts to channels as well as threads, and `parse_link`
  resolves permalinks.
- Tools that only apply to the other platform are hidden from callers.
- Cross-process turn liveness, a boot sweep and recovery-card adoption, so a
  restart no longer strands a thread.
- A read-only setup panel and targeted setup conversations.
- Self-hosted Slack setup documented, with an app manifest and a compose
  profile.

#### Setup, credentials and repositories

- Posted control cards on both platforms: private forms, card states that
  tell the truth, `.env` import, agent model chosen at creation, and the
  provenance of a request recorded when it is minted.
- `set_setup_target` aims setup at a specific conversation.
- GitHub App install-link cards on both platforms
  (`DAIMON_GITHUB__APP_SLUG`).
- Repository binding from chat (`bind_public_repo`, `request_repo_binding`),
  with proof of access taken at bind time and enforced on every write.
- Skill-repo tokens are stored apart from the working-repo binding, so
  enrolling a skill repository no longer re-points the clone target.
- A per-person connect flow for OAuth-only MCP servers
  (`request_mcp_oauth`), and `detach_mcp_server` to undo it.

#### MCP surface

- `/slack/mcp` and `/discord/mcp` OAuth hub mounts, plus a Claude Code
  plugin in `plugin/` that reaches every daimon a person can see
  (`DAIMON_HUB__*`).
- `create_file_upload_url` takes a file by URL instead of base64 tool
  arguments, and `PUT /bundles` streams bundle uploads under an hourly
  per-token limit (`DAIMON_MCP__BUNDLE_*`).
- 33 net-new tools in total; `.env.example` and the tool list are the
  reference.

#### Reports, notebooks and charts

- New app `apps/report-host`: a per-recipient report reader with chat, admin
  publish, delete and revoke routes, reader-variant agents, and sweeps for
  restart-resume, deadline-cancel, idle-archive and recipient-prune
  (`DAIMON_REPORT_HOST__*`, and the app's own `DAIMON_REPORT__*`).
- `publish_report` and `delete_report`, with `report-publish` and
  `report-reader` seeded skills.
- Charts produced during a turn are delivered from S3-compatible storage
  (`deliver_turn_charts`, `DAIMON_ARTIFACTS__*`).
- Notebooks are one tool plus `list_notebooks` and `delete_notebook`, each
  notebook isolated in its own filesystem jail on the host.

#### Skills

- Newly seeded: `workspace-setup`, `file-handling`, `pymc-artifact-style`,
  `report-publish`, `report-reader`, and a data-analysis set
  (`data-ingestion`, `data-cleaning`, `data-validation`,
  `exploratory-data-analysis`, `eda-storytelling`).
- `sync_skills` can attach to an agent and refuses unmetered models; chat
  can collect a token for a private skill repository; `remove_skill`.

#### CLI

- New commands: `daimon memory`, `daimon repo-bindings`, `daimon smoke`,
  `daimon defaults verify`, `daimon mcp mint-agent-token`, `daimon agents
  bind-google`.
- `--tenant` / `--guild` overrides on `agents`, `skills` and `config`, and
  `config --scope` accepts Slack scopes.

#### Self-hosting

- `docs/self-hosting.md` covers prerequisites, environment, the Discord app,
  the stack, Slack, Claude Code login mounts, chart delivery and connecting
  external MCP servers. The readme is rewritten around a three-step
  quickstart, and `PRIVACY.md` is new.
- Tagged releases publish a multi-architecture image to
  `ghcr.io/pymc-labs/daimon`, so the first run is a pull rather than a
  build.
- New settings blocks a self-hoster may need: `DAIMON_HUB__*` (its
  `JWT_SIGNING_KEY` must be 32 bytes once any mount is configured),
  `DAIMON_ARTIFACTS__*`, `DAIMON_REPORT_HOST__*`, `DAIMON_SUPPORT__*`,
  `DAIMON_THREAD_PARTICIPATION__*`, `DAIMON_THREAD_NAMING__*`,
  `DAIMON_MCP__BUNDLE_*` and `DAIMON_GITHUB__APP_SLUG`. All are optional
  unless the feature is wanted, and `.env.example` lists every one with its
  default.

### Changed

- One admission, session-bind and prepared-turn path now drives Discord,
  Slack and MCP, so behaviour that used to differ by adapter no longer does.
- Agents seeded from `defaults/` cannot be deleted on either platform. Both
  adapters refuse server-side; the Discord panel's disabled button was
  client-side only and the Slack panel offered deletion outright.
- Reading a routine's last run output needs the same authority as pausing or
  deleting it: workspace admin, or the routine's creator.
- `create_environment` is no longer gated.

### Fixed

- The Discord Details card's coding-tools token now stays scoped to its exact
  MA agent ID and refuses archived, missing, or foreign-tenant identities
  instead of substituting a newer agent with the same name.
- Threads no longer freeze. Dead sessions are recovered, a dropped
  mid-stream connection reconnects, turns orphaned by a restart are retired,
  and liveness is tracked across processes rather than per worker.
- One failing or unauthenticated MCP server no longer discards the whole
  reply: a session mounts only the servers its caller can authenticate, and
  a forked agent drops what it cannot.
- Channel and thread history pagination is bounded and flags truncation
  instead of quietly returning a partial view.
- Rotated MCP credentials propagate in place and reach every caller.
- Skill sync is hardened: decompressed tarballs are size- and member-capped,
  paging goes past 100 entries, the boot sweep no longer exhausts the Skills
  API rate limit, and duplicate mount names are refused.
- Billing errors are honest: the Slack top-up modal answers when checkout
  fails, error rendering no longer surfaces SQL, and pooled database
  connections are pre-pinged.
- Wiping a workspace is refused unless the workspace is marked disposable.

### Security

- Slack mentions queued behind an in-flight turn are partitioned by author,
  one turn per caller. The whole queue used to be coalesced into a single
  turn run as the first queued author, so a second member's instructions
  executed inside the first member's session, under their credentials and
  visibility, billed to them.
- `tokens_revoked` no longer tears down the install unless the event names
  the bot token. Slack also emits it when a single member revokes their own
  user token, which meant one member disconnecting could uninstall the app
  for the entire workspace.
- Slack refreshes the caller's admin role on every mention and re-checks it
  at click time; a failed lookup does not overwrite a stored role. Both
  adapters carry caller admin status into turn context, and Discord turn
  replies suppress mass mentions.

## [0.1.0] - 2026-07-15

Initial public release.

- Self-hostable Discord bot built on Anthropic Managed Agents, with one-click
  operator install and per-guild tenant isolation.
- `cli` adapter: the `daimon` admin CLI for driving turns and managing agents,
  environments, and skills from a terminal.
- `discord` adapter: mention-triggered threaded conversations and a
  slash-command admin surface.
- `mcp` adapter: an MCP server for agent-to-agent orchestration.
- `scheduler` adapter: polls due routines and dispatches headless turns.
- `slack` adapter (optional): Slack parity with the Discord adapter, off by
  default.
- Docker Compose deployment with a single-revision schema bootstrap.

[Unreleased]: https://github.com/pymc-labs/daimon/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pymc-labs/daimon/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pymc-labs/daimon/releases/tag/v0.1.0
