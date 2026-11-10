# StreamSplitter on a free cloud server, started from Slack

Researched 2026-11-05 for the user's idea (their words): "from slack you send a message to the app with the name of
the event or a tba link with the event you want it to split matches for, it then spins up a server that does the
splitting and uploads the data to slack, youtube and/or google drive. Does this cause issues with youtube not allowing
uploads from another server?"

Research only: nothing was built, signed up for or run. Provider pages and the yt-dlp repo were read on 2026-11-05;
the date in brackets after a source is the date the page itself shows. **Unconfirmed** means only forum posts, blogs or
issue reports say it. StreamSplitter's own numbers come from README.md, the first proposal (docs/history/PROPOSAL.md),
docs/requirements-and-limits.md, docs/publish-design.md, phase0/PLAN.md and the follower log
`logs/mttd24_follow2.log`.

## Bottom line

**Feasible with caveats, and only on Oracle Cloud's Always Free tier.** Every other free offer is too small, sleeps,
runs out after months, or its terms forbid this job.

- **Uploading to YouTube from a server is fine.** The YouTube Data API is a normal server API. Nothing ties it to a
  home IP. Slack, Google Drive and The Blue Alliance are the same.
- **Recording YouTube from a server is the real risk.** YouTube refuses anonymous access from many datacenter IP
  addresses ("Sign in to confirm you're not a bot"). yt-dlp's maintainers say they cannot fix that. There is no free
  workaround that is reliable and within YouTube's terms. Whether Oracle's addresses are affected can only be found
  out by trying, for days, not minutes.
- **The free server is smaller than it was.** On 2026-06-15 Oracle quietly halved the Always Free Arm allowance to 2
  CPU cores and 12 GB. Two cores may or may not keep up with the overlay OCR at 1080p. That has to be measured too.
- **The Slack part is easy.** Don't create a server per event. Keep one free VM running and give it a small
  "dispatcher" that reads the Slack DM (as StreamSplitter already does) and starts or stops one
  `streamsplitter live` per event.

Build nothing until a cheap test passes. On a free Oracle VM, record a live YouTube stream for several days, and
replay one recorded hour through the follower to time it (section 5).

---

## 1. YouTube (and Twitch) from a server

### 1a. Uploading through the Data API with a refresh token: no problem found

- **No IP or hosting rule.** The YouTube API Developer Policies
  ([developer-policies](https://developers.google.com/youtube/terms/developer-policies)) have nothing on uploading
  from servers. They allow storing tokens "for as long as is necessary" for the user's consent (III.E.4.a). They
  require transport encryption (III.E.5.b). The limits that matter are the same as on a laptop:
  - 100 uploads a day per Cloud project
  - the channel's own unpublished daily limit
  - the 2026-11-02 end of the private lock

  All three are in [youtube.md](youtube.md).
- **One OAuth setting is fatal on a server.** A "Testing" consent screen issues refresh tokens that die 7 days after
  consent ([OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2)). A server that runs all
  season needs the project set to **In production** (unverified is fine for one team; see youtube.md A.4).
- **Logging in on a machine with no screen.** Two ways:
  - **Easiest:** log in on a laptop with the existing browser flow, then move the token to the server inside the
    existing encrypted team settings file (`run.sh --section team`).
  - **Or:** Google's device flow (type a code on a phone) supports `youtube` and `drive.file` but not
    `youtube.upload` or `youtube.force-ssl`
    ([limited-input devices](https://developers.google.com/identity/protocols/oauth2/limited-input-device)). Our
    smallest working scope is `youtube` anyway (youtube.md A.4). It needs a separate "TVs and Limited Input" client.
- **A small policy point.** Google's OAuth policies say "You must choose the client type that best matches the
  platform" ([OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)). A Desktop-app
  token used on a server is a grey area. I found no case of Google acting on it; low risk.
- **Tokens must be encrypted at rest.** The same page says "always store encrypted tokens at rest". On a headless
  Linux box there is no keyring, so StreamSplitter falls back to `secrets.json`, which is plain text (config.py).
  Encrypt it on a server (section 3).
- **Unconfirmed:** whether Google ever flags a refresh token that is suddenly used from a datacenter in another
  state. I found nothing either way. The setup wizard's test upload, run on the server, settles it in one minute.

### 1b. Recording a YouTube live stream from a datacenter IP: the real risk

**What is confirmed (yt-dlp maintainers, on the yt-dlp tracker):**

- seproDev, 2025-02-25, on a report from an "Amazon server": "Youtube blocked anonymous access from many datacenter
  IPs." Then, closing it: "IP blocks are nothing that can be affected from the yt-dlp side." Source:
  [yt-dlp #12475](https://github.com/yt-dlp/yt-dlp/issues/12475).
- bashonly, 2026-02-12, same thread: "If you are getting this error, it means that YT does not trust your IP address
  enough to allow anonymous access." The advice given was to pass logged-in cookies.
- The catch-all issue [#10128](https://github.com/yt-dlp/yt-dlp/issues/10128) "[youtube] Sign in to confirm you're
  not a bot" has been open and pinned since 2024-06-07. It still collects duplicates (latest 2026-06-17). It is labelled
  "Issue with an external tool or site".
- **PO tokens are not the problem for live streams.** The yt-dlp PO Token Guide
  ([wiki, edited 2026-07-12](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)) says: "HLS live streams do not
  require a PO Token (excluding `ios` client)." Our recorder uses live HLS (record.py). So a PO-token provider such
  as bgutil does not help; the block is on the IP.
- **Rate limits for anonymous use.** The Extractors wiki
  ([YouTube section](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)) gives about 300 videos/hour for a guest
  session and about 2000 for an account. One stream is far below that. But the recorder asks YouTube every 45 s while
  a channel is not live (record.py `POLL_S`). That is about 80 page requests an hour, all day, on event mornings and
  breaks. On a flagged IP, fewer requests are better.
- **A JavaScript runtime (Deno) is now expected.** Since yt-dlp 2025.11.12, an external JS runtime is "required for
  full YouTube support" ([#15012](https://github.com/yt-dlp/yt-dlp/issues/15012), 2025-11-12). Live HLS still
  resolves without one (record.py docstring, checked 2026-11-04). Deno has an official Linux Arm64 wheel (section
  2c).

**What is reported (unconfirmed, but consistent):**

- **Oracle Cloud specifically.** A 2025-07-14 blog post describes a server at Oracle Cloud blocked with "Sign in to
  confirm you're not a bot". It was routed through Cloudflare WARP as a workaround, and the author says "We don't
  know if this solution is stable over time"
  ([arfevrier.fr](https://blog.arfevrier.fr/leveraging-cloudflare-warp-to-bypass-youtubes-api-restrictions/)).
  This is one report about one IP, not proof that all Oracle addresses are blocked.
- **Blocks can be partial and per address.** In [yt-dlp #16072](https://github.com/yt-dlp/yt-dlp/issues/16072)
  (2026-02-26), three identical instances ran on one unnamed cloud host, each on its own IP:
  - one of them got "The channel is not currently live" for channels that *were* live, and a bot check about 9 % of
    the time
  - the other two worked
  - the issue was closed without a maintainer answer

  For us that is the worst failure: the VM would sit "waiting for the stream" through a whole event day.
- **Nothing on the client side gets past the block.** A server operator (2026-08-21) found:
  - all 9 player clients blocked, and a PO-token provider did not help
  - cookies made it worse (3 of 3 failed with cookies, 3 of 3 worked without)
  - waiting cleared it, but it came back

  Source: [rainrag #41](https://github.com/yidakra/rainrag/issues/41).
- **Only a residential IP worked.** A developer on Alibaba Cloud (2026) tried cookies, public download APIs and the
  Data API. Only a paid residential proxy worked
  ([DEV Community](https://dev.to/osovsky/i-was-building-a-cloud-video-service-youtube-turned-me-into-an-ip-trafficker-1l9o)).
  The same pattern shows on Hugging Face Spaces
  ([openai/whisper #121](https://huggingface.co/spaces/openai/whisper/discussions/121)).
- **Datacenter blocks are years old.** A contributor said on 2026-05-17 that IP blocking "has been doing that for
  years at this point" ([#16747](https://github.com/yt-dlp/yt-dlp/issues/16747)). This is not new in 2026.
- **GitHub Actions runners.** Their outbound IPs come from "a multi-cloud pool", not only Azure
  ([runner docs](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)). I found no dated
  report specific to Actions. The general datacenter reports apply.
- **No provider list.** I found no list of which providers' ranges are blocked. Reports name AWS, Alibaba, Hetzner
  and Oracle. It behaves like reputation scoring per address, which can change from one day to the next.

**What gets around it, and what each costs:**

| Workaround | Works? | Cost and risk |
|---|---|---|
| Logged-in cookies | The maintainers' advice. Mixed reports from cloud IPs (rainrag found cookies made it worse) | The yt-dlp wiki warns that using an account with yt-dlp risks "it being banned (temporarily or permanently)" and suggests a throwaway account ([Extractors](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)). Cookies must be exported from a private window and rotate. Never use the channel owner's account: that is the account that holds the upload rights. |
| PO-token provider (bgutil) | Not relevant for live HLS (guide above); does not lift IP blocks (rainrag) | Extra moving part |
| Residential proxy | The only fix that reports say works consistently | Not free. YouTube's media URLs carry the requesting IP (an `ip=` field), and reports say they fail from another address (**unconfirmed**), so the whole stream must go through the proxy: 2-3 GB an hour, 40-80 GB an event. At typical per-GB prices that is far beyond a free design. |
| Cloudflare WARP via `wgcf` | One Oracle report, stability unknown | Unofficial client; WARP addresses are datacenter addresses too |
| Route YouTube through a team member's home (Tailscale exit node or a small proxy on a Pi at home) | Should work: the home IP is residential (my inference, not tested) | Free: the Tailscale Personal plan has 6 users, unlimited devices and exit nodes ([pricing](https://tailscale.com/pricing)). But the home connection carries the stream twice (about 6 Mbit/s down and 6 up, all day), and the team depends on a home connection again, though not on a laptop. Only yt-dlp and ffmpeg should use it, not the uploads. |
| Use a different datacenter IP | Sometimes (#16072: two of three IPs worked) | A lottery; the good IP can go bad mid-event |

**The rules.** Recording is already against the letter of YouTube's terms. The ToS forbids automated access, and the
API Developer Policies say an API client must not "download, import, backup, cache, or store copies of YouTube
audiovisual content without YouTube's prior written approval" (III.E.1.a,
[developer-policies](https://developers.google.com/youtube/terms/developer-policies)). StreamSplitter is both an API
client (it uploads) and a recorder. This is true on a laptop too, and PROPOSAL.md section 10 already lists it. A cloud
server does not change that. But getting around a block YouTube has put on purpose (throwaway accounts, borrowed or
bought residential IPs) moves further into "circumventing" territory. If enforcement ever came, the asset at risk is
the team's Cloud project and channel, which do the uploads. Keep the organisers' permission in writing, as the
requirements doc says.

### 1c. Twitch

- **No datacenter blocks found.** I found no report of Twitch blocking datacenter IPs for watching a live stream, in
  yt-dlp or streamlink issues or elsewhere. The only search hits were proxy vendors' marketing. Treat it as low risk
  but **unmeasured**.
- **Ads are the known Twitch problem, wherever the recorder runs.** Pre-roll and mid-roll ads replace the live
  picture for viewers who are not logged in. Streamlink can filter them, which "paused" the output (a hole in the
  recording). Sometimes it needs a "client-integrity" token, and making one needs a Chromium browser on the machine
  ([streamlink Twitch docs](https://streamlink.github.io/cli/plugins/twitch.html)). That is awkward on a headless Arm
  server. StreamSplitter records Twitch with yt-dlp today (record.py), and PROPOSAL.md already lists the ad problem.
- **Shorter replay window.** Twitch keeps about 10 s of replay, against YouTube's hour, so any drop on a server
  leaves a hole (requirements doc).

### 1d. Slack, Google Drive and TBA from a server

- **No IP concerns for any of them.** They are ordinary APIs used from servers every day.
- **Drive.** The limit is 750 GB uploaded per user per day
  ([Drive limits](https://developers.google.com/workspace/drive/api/guides/limits)); we use about 4 GB an event.
- **Slack command polling.** Slack's 2025 cut of `conversations.history` to 1 call a minute applies to apps
  distributed outside the Marketplace. "Internal customer-built apps are not impacted"
  ([changelog 2025-05-29](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps),
  [clarification 2025-06-03](https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity)). StreamSplitter's
  team-made app is internal, so the 20-s DM poll keeps working from a server (commands.py says the same).

---

## 2. Free hosts compared

### 2a. What the job needs (from our own numbers)

| Resource | Need | Source |
|---|---|---|
| CPU | A 20-s chunk is read in 1.9-7.0 s on the 16-core test PC (`logs/mttd24_follow2.log`). The work: full decode of 1080p H.264, one whole-frame OCR every 4 s (~0.45 s on a desktop), timer re-reads at 4 fps (~6 ms each). The 720p copy (libx264 veryfast) takes 6.7 s for a 170 s clip on the same PC. No GPU used. | overlay.py, pipeline.py, PLAN.md |
| RAM | Not measured (requirements doc) | |
| Disk | 2-3 GB per stream hour, kept 28 h (about 40 GB after two days, 76 GB worst case), plus 20-25 GB of clips per event, plus 1 GB of software | requirements doc |
| Download | 4-6 Mbit/s all event day; 40-80 GB an event (inbound is free at every provider below) | requirements doc |
| Upload | YouTube 9-13 GB, Slack 1-3 GB, Drive about 4 GB at 720p: roughly 4-20 GB an event | requirements doc, user brief |
| Runtime | 8-10 h a day for 2-3 days, every few weeks; idle in between | user brief |

### 2b. The offers (checked 2026-11-05)

| Host | CPU / RAM | Disk | Free egress | Always on? | Card / ID | Terms for this job | Verdict |
|---|---|---|---|---|---|---|---|
| **Oracle Cloud Always Free, Ampere A1** ([docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)) | **2 Arm cores, 12 GB** ("1,500 OCPU hours and 9,000 GB hours per month"; 1 OCPU = 1 Ampere Altra core, up to 3.0 GHz, 1 Gbps per OCPU ([shapes](https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm))) | 200 GB of boot plus block volume in total (a boot volume is at least 47-50 GB) | 10 TB a month | Yes, but idle instances may be reclaimed (2c) | Phone and credit card "for security purposes", not charged unless you upgrade ([free tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)) | Nothing found against it (Oracle's acceptable-use policy not reviewed in depth) | **The only real candidate** |
| Oracle Always Free, AMD micro | 1/8 OCPU, 1 GB, 50 Mbps | same pool | same | Yes | same | | Far too small; could host only a tiny Slack listener |
| Google Cloud free tier ([features](https://docs.cloud.google.com/free/docs/free-cloud-features)) | one e2-micro (shared, burstable), 1 GB | 30 GB | **1 GB a month** (North America) | Yes | Card | | No: RAM and egress far too small. The separate $300 / 90-day trial could rent a bigger VM for a few events, then it ends |
| AWS free plan, accounts since 2025-07-15 ([EC2 free tier](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html), [aws.amazon.com/free](https://aws.amazon.com/free/)) | Free-tier types up to c7i-flex.large / m7i-flex.large (2 vCPU, 4 or 8 GB), paid from $100 sign-up credit plus up to $100 earned | EBS from credits | 100 GB a month ([EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)) | Until credits or **6 months** run out; then the account is suspended and erased after 90 days unless upgraded ([FAQ](https://aws.amazon.com/free/free-tier-faqs/)) | "A payment method isn't required ... for most new customers" | | One season at most, then paid; a datacenter IP too |
| Azure free account ([create-free-services](https://learn.microsoft.com/azure/cost-management-billing/manage/create-free-services), 2026-03-25) | B1s (1 vCPU), B2pts v2 (2 Arm vCPU), B2ats v2 (2 vCPU): all burstable, **1 GiB RAM**; 750 h a month each, 12 months | not checked | not checked | 12 months; $200 credit for the first 30 days | Not re-checked | | No: 1 GiB is too little for decoding plus OCR (my estimate), and burstable VMs slow down under all-day load |
| GitHub Actions ([limits](https://docs.github.com/en/actions/reference/limits), [runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)) | Public repos: 4 vCPU, 16 GB, 14 GB SSD (x64 or Arm), free and unlimited. Private: 2 vCPU, 8 GB, $12 a month of included compute | 14 GB | not stated | **6 h per job**, then the job is killed | GitHub account | **Forbidden:** GitHub-hosted runners must not be used for "any other activity unrelated to the production, testing, deployment, or publication of the software project" ([additional product terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features), effective 2026-08-27) | No for events. A short test of the recorder itself counts as "testing" and is fine |
| GitHub Codespaces ([billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces), [timeout](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces)) | 120 compute hours and 15 GB a month free | 15 GB | not stated | Stops after at most 4 h with no typing or terminal output | GitHub account | **Forbidden:** no "production-facing application"; only "development or testing of the software project" | No |
| Hugging Face Spaces ([overview](https://huggingface.co/docs/hub/spaces-overview)) | CPU Basic: 2 vCPU, 16 GB | 50 GB, not persistent | not stated | Sleeps when unused | **Docker and Gradio Spaces now need a paid plan** to create | Outbound only on ports 80, 443 and 8080 | No |
| Render ([free](https://render.com/docs/free)) | Free web services only; background workers are not free | No persistent disks on free | counts against the bandwidth allowance | Spins down after 15 min with no inbound traffic | | | No |
| Railway ([pricing](https://railway.com/pricing)) | Trial: $5 for 30 days. Then $1 a month: 1 vCPU, 0.5 GB | 0.5 GB volume | $0.05 per GB | | No card for the trial | | No |
| Fly.io ([pricing](https://docs.fly.io/about/pricing)) | "New organizations don't have a free tier"; $1 of credit for 7 days | $0.15 per GB-month | $0.02 per GB | | Card after the trial | | No |
| Koyeb ([pricing FAQ](https://www.koyeb.com/docs/faqs/pricing)) | One free web service: 0.1 vCPU, 512 MB, 2 GB | 2 GB | | Scales to zero when idle (third-party sources: after 1 h, **unconfirmed**) | | | No |

**Something better, though not cloud and not free:** a Raspberry Pi 5 or an old PC left at a mentor's home or the
team's shop.

- **The IP problem goes away.** A home or school connection has a residential IP.
- **The same Slack trigger works,** because the DM poller needs no public address.
- **The same CPU test applies.** The Pi 5's Cortex-A76 cores are the design Ampere Altra's Neoverse N1 cores are
  based on, at 2.4 GHz instead of 3.0. So the Oracle CPU test (section 5) roughly predicts a 4-core Pi 5 too.
- **The cost:** a one-off hardware purchase, and someone's home internet (6 Mbit/s down all day plus the uploads).

### 2c. Oracle details that matter

- **The allowance was halved without notice.** Until mid-2026 Always Free gave 4 Arm cores and 24 GB. InfoQ
  ([2026-07-03](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)) reports:
  - it became 2 cores and 12 GB on 2026-06-15, with no announcement
  - free-only accounts' bigger instances were shut down until resized
  - Oracle support agents reportedly said Pay As You Go accounts keep 4 cores and 24 GB free, but the docs say "All
    tenancies get the first 1,500 OCPU hours", so this is **unconfirmed**

  Plan on 2 cores. Oracle can change it again.
- **Idle instances may be reclaimed.** The docs' exact criteria: during a 7-day period, all of
  - "CPU utilization for the 95th percentile is less than 20%"
  - "Network utilization is less than 20%"
  - "Memory utilization is less than 20% (applies to A1 shapes only)"

  The docs don't say what happens next. Blogs say the instance is stopped or removed and the boot volume may survive
  ([oracommit, 2026-02-03](https://oracommit.blogspot.com/2026/02/understanding-oci-always-free-compute.html)).
  - **Between events the VM will be idle for weeks**, so on a free-only account expect it to be reclaimed.
  - Reports say upgrading to Pay As You Go stops this, while you still pay nothing within the Always Free amounts
    (**unconfirmed** by Oracle's current docs).
  - Upgrading puts the card on file for real, so set a budget alert at $1.
  - Running a fake load to look busy is against the point of the rule; not recommended.
- **The VM may be impossible to get.** "Out of host capacity" errors are acknowledged in the docs. A1 VMs can only
  be made in the home region chosen at sign-up, and sign-up gives one chance to pick it. Choose a US West region near
  the team.
  - Make the VM once and keep it.
  - A plan that creates the VM on event morning can fail exactly when it is needed.
- **When the 30-day trial ends,** A1 instances above the free amount are disabled and then deleted after 30 days
  unless the account is upgraded ([free tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)).
  Create the VM at 2 cores and 12 GB from the start.
- **Unconfirmed reports** on Oracle's community forum describe active instances reclaimed anyway, and free accounts
  closed. Keep the laptop path working as the backup.

### 2d. Will StreamSplitter install on Oracle's Arm (aarch64) Linux?

Very likely yes. Everything it needs exists for Linux Arm64 (PyPI pages read 2026-11-05):

| Piece | Linux Arm64 build | Note |
|---|---|---|
| Python 3.12 (via uv) | Yes | run.sh path |
| yt-dlp, yt-dlp-ejs, rapidocr 3.10 | Pure Python (`py3-none-any`) | |
| onnxruntime 1.31.1 (2026-10-26) | `cp312 manylinux_2_28_aarch64` ([files](https://pypi.org/project/onnxruntime/#files)) | Needs glibc 2.28 or newer: Ubuntu 22.04/24.04 or Oracle Linux 8/9 are fine |
| opencv-python-headless | `abi3 manylinux_2_28_aarch64` and `manylinux2014_aarch64` ([files](https://pypi.org/project/opencv-python-headless/#files)) | |
| numpy | Standard aarch64 wheels | |
| Deno 2.9.8 (2026-10-21) | `manylinux_2_27_aarch64` ([files](https://pypi.org/project/deno/#files)) | yt-dlp's recommended runtime |
| ffmpeg + ffprobe | Martin Riedl's static Linux Arm64 builds | setup_wizard.py `ffmpeg_downloads()` already picks arm64 on Linux |
| keyring | No Secret Service on a headless server, so StreamSplitter uses `secrets.json` (or `STREAMSPLITTER_<NAME>` environment variables, which config.py reads first) | See section 3 for encrypting it |

Not tested on Arm. `sh run.sh --install-only` on the VM is step 1 of the test (section 5).

---

## 3. Slack trigger design

### 3a. Options

| Option | What runs where | Good | Bad |
|---|---|---|---|
| **A. One always-on free VM with a dispatcher (recommended)** | The Oracle VM runs `streamsplitter-dispatch` (systemd) all the time. It reads the app's DM (polling, as today, or Socket Mode) and starts one `streamsplitter live` per event | No public URL and no new service. Reuses the DM poller, the outbox, the alarm and the status code. Nothing to "spin up", so no capacity failure on event morning | The VM is one machine; it must be kept patched |
| B. Slack -> webhook -> GitHub `repository_dispatch` -> Actions job | Slack Workflow Builder webhook (free-plan availability **unconfirmed**) or a Cloudflare Worker -> GitHub REST ([repository_dispatch](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event)) -> a workflow | Truly on demand | Breaks GitHub's terms for this job, 6-h job limit, datacenter IPs, a 14 GB disk. **Rejected** |
| C. Slack -> small listener -> cloud API creates or starts a VM | Listener on the Oracle micro VM or a Cloudflare Worker (Slack's HTTP Events API needs a public HTTPS URL that answers within 3 s). The worker VM is created or started through the provider's API | Saves money on *paid* clouds (AWS credits): the VM runs only during events | On Oracle the VM is free anyway, and creating A1 VMs on demand risks "out of host capacity". Two places hold secrets (listener plus cloud API key). Only worth it on a paid cloud |

### 3b. The recommended flow (option A)

```
 Slack DM to the StreamSplitter app (from an allowed person)
   "split 2026mttd"   or   "split https://www.thebluealliance.com/event/2026mttd"   or   "split madtown"
        |
        v   (outbound only: the VM polls the DM every 20 s, or holds a Socket Mode WebSocket)
 Oracle A1 VM (always on, no open inbound ports)
   streamsplitter-dispatch.service      parses the command, looks the event up on TBA (name/city search exists in
        |                               the wizard), picks TBA's webcast link, creates the YouTube playlist if
        |                               missing, asks "Split 2026mttd MadTown Throwdown from <link>? reply yes"
        v
   streamsplitter@2026mttd.service       = streamsplitter live --event 2026mttd --tag 2026mttd ...
        |   record (yt-dlp + ffmpeg) -> detect -> cut -> durable outbox
        v
   Slack (team's matches)   YouTube (unlisted playlist)   Google Drive (720p copies)   [team wiki]
```

**Commands** (the dispatcher's; the existing per-run commands carry on):

| Command | What it does |
|---|---|
| `split <event key, TBA link or name>` | Start an event. Asks to confirm. |
| `stop <event>` | Stop an event. Asks to confirm. Today "Nothing can stop the recorder from Slack" (commands.py), on purpose. On a server nobody can double-click run.bat, so stop must exist, limited to the allowed people. |
| `list` | What is running. |
| `status [event]` | The existing status text. |
| `stream`, `teams`, `pause`, `resume` | As today, with an event name in front when two are running. |
| Auto-stop | After TBA's last event day, once the stream has been offline for a few hours, with a DM summary. |

**Who may command it.** Today only the configured contact's DMs are read (commands.py). Keep an allowlist of Slack
user IDs, set on the server, not from Slack.

**Polling or Socket Mode.**

- **Polling (today's way).** No new tokens or scopes, and it is within the rate limits (1d). Replies take up to 20 s.
- **Socket Mode** ([docs](https://docs.slack.dev/apis/events-api/using-socket-mode)): instant replies and real slash
  commands, still with no public URL.
  - Needs an app-level token (`xapp-`) for `apps.connections.open`, which needs no scopes and is Tier 3
    ([method](https://docs.slack.dev/reference/methods/apps.connections.open)).
  - Turn on Socket Mode in the app and subscribe to `message.im`.
  - Socket Mode apps are "not currently allowed in the public Slack Marketplace". That is irrelevant for a team app.
- **Verdict:** polling first; Socket Mode later if a 20-s reply delay annoys anyone.

**Code changes this needs** (not small, but contained):

- **A dispatcher** (new module plus a systemd unit template).
- **Per-event settings.** config.toml holds one `[event]` today, plus playlist and wiki page per event. An event
  started from Slack needs its own settings file or overrides.
- **One DM reader.** Two runs plus the dispatcher must not all poll the same DM. The runs take commands from the
  dispatcher through a local file or socket. `request_stop(tag)` and `status.json` already exist in live.py.
- **A "server mode":**
  - no keep-awake
  - logs to the journal
  - stop from Slack
  - the 45-s "is it live yet" poll slowed down (see 1b)

### 3c. Secrets on the server

- **What is on the server.** The TBA key, the Slack bot token (`xoxb-`), the YouTube and Drive token JSON and the
  Google client JSON. Plus the app-level `xapp-` token if Socket Mode is used.
- **Getting them there.** Copy the existing team settings file (AES-256-GCM, passphrase) to the VM over SSH. The
  user types the passphrase there once (`run.sh --section team`). Never paste tokens into Slack or a repo; the
  requirements doc says the same.
- **Where they live.** With no keyring, StreamSplitter writes `secrets.json` (plain text) or reads
  `STREAMSPLITTER_<NAME>` environment variables. Google wants tokens "encrypted at rest" (1a), so:
  - run as a dedicated non-root `streamsplitter` user, with secrets readable only by it (0600)
  - better: keep them encrypted with `systemd-creds` and hand them to the service through `LoadCredentialEncrypted=`
    (a systemd feature; check the exact setup when building)
  - an OCI Vault secret is possible but adds an API dependency; not needed
- **Locking down the VM:**
  - no inbound ports except SSH with a key (or none, using Tailscale SSH)
  - automatic security updates
  - multi-factor sign-in on the Oracle console
  - only the scopes already chosen
- **The YouTube consent screen must be "In production"** (1a), or the server's login dies after a week.
- **If the VM is ever suspected compromised,** revoke the Slack token and the Google grant; the requirements doc
  lists where.

### 3d. Status, stopping, two events, and a dead VM

- **Status.** As today: the started and stream-started DMs, errors, `status` replies. The **stopped-running alarm**
  is a Slack scheduled message that the run keeps pushing back. Slack delivers it even if the VM dies, so a dead VM
  is reported within about 7 minutes (`alarm_after_s = 420`; publish/notify.py says why not sooner).
- **Two events at once.** CPU is the limit, not Slack.
  - On 2 Arm cores, one 1080p event is the realistic maximum until measured (section 4).
  - A second event goes to a laptop, as today, or waits for the CPU numbers.
  - Both share YouTube's 100 uploads a day per project (requirements doc).
- **The VM reboots mid-event** (maintenance, a crash): systemd restarts the dispatcher, and the run restarts with the
  same tag. That path is already built and tested:
  - the half-written chunk is salvaged
  - the durable outbox resumes uploads
  - YouTube's hour of replay refills the gap; Twitch's 10 s does not
- **The VM is reclaimed or its account suspended:**
  - the alarm fires
  - a teammate starts a laptop from the team settings file (the existing backup path)
  - matches already uploaded are safe; anything only on the VM's disk may be gone
- **The idle rule cannot fire during an event,** because it needs 7 quiet days. Between events it will (2c).

---

## 4. Can each clip reach the drive team within 4 minutes?

**Measured on the 16-core test PC:**

- A 20-s chunk is read in 1.9-7.0 s.
- The match clip is on disk 27-33 s after the match end at live speed.
- A 720p copy takes 6.7 s.

Source: PLAN.md change 21, `logs/mttd24_follow2.log`, pipeline.py.

**Estimated for 2 Ampere cores (my estimate, not measured).**

- **The assumption.** An Altra (Neoverse N1) core does perhaps a third to a half of the work of a current desktop
  core. The test PC spreads the read over many cores (ffmpeg and ONNX Runtime both use threads).
- **The parts of one chunk:**
  - decoding 20 s of 1080p60 (1,200 frames) takes roughly 8-15 core-seconds on N1
  - five whole-frame OCR reads take perhaps 1-2 s each
  - the timer re-reads take a few seconds more

| Step | Test PC (measured) | Oracle, 2 Arm cores (estimate) |
|---|---|---|
| Webcast delay (YouTube "normal latency") | not measured | not measured; YouTube only publishes "less than 5 seconds" for ultra-low ([Help](https://support.google.com/youtube/answer/7444635)) |
| Chunk closes and is listed | up to 20 s, plus 13-16 s listing lag | same |
| Read one 20-s chunk | 1.9-7.0 s | **about 10-22 s.** Under 20 s, it keeps up. Over 20 s on average, it falls further behind every minute. |
| Match clip on disk after the match end | 27-33 s | about 40-60 s if it keeps up |
| Upload a 125 MB clip to Slack | 40 s at 20 Mbit/s home upload | about 10 s (the VM has 1 Gbps per core; Slack's own intake speed not measured) |
| Make a 720p copy for Drive | 6.7 s | **about 1-2 minutes,** and it competes with the chunk reading while it runs |
| Upload a 40 MB copy to Drive | 15 s at 20 Mbit/s | a few seconds |

**What that adds up to (estimates):**

- **1080p clip to Slack:** about 1-1.5 min after the match end, plus the webcast delay. Comfortably within 4 minutes,
  *if* chunk reading keeps up.
- **720p copy to Drive:** about 2-3 min plus the webcast delay. Within 4 minutes but tight. It fails if reading is
  already behind, or if both the clip and the score version need 720p copies close together.
- **The upload side improves.** A datacenter uplink beats venue or home upload; the 6 Mbit/s requirement disappears.
- **Unchanged:** the drive team still downloads at the venue on its own connection.

**What must be measured on the VM before believing any of this:**

1. **Chunk read time** over at least an hour at live pace: median, 95th percentile, and whether a backlog grows.
2. **The 720p copy** of a 170-s clip, alone and while the follower runs.
3. **Peak RAM** of one run (never measured anywhere).
4. **Upload speed** from the VM to Slack, YouTube and Drive.
5. **The webcast delay** at a real event (also open on laptops).

**If 2 cores do not keep up,** the options are code changes, to test against the phase 0 scorecards before use:

- detect on YouTube's 720p rendition while keeping 1080p for clips (both renditions recorded; bandwidth is free in
  the cloud)
- skip non-reference frames in the overlay decode, as the clock reader already does
- a faster x264 preset for the Drive copy
- or skip the Drive copy's re-encode

---

## 5. Recommendation

**Verdict: feasible with caveats, on Oracle Always Free only. Don't build it until the test below passes.** It
removes the need for a laptop that stays awake. It does not remove the need for a backup, because one free VM is the
"single central server" PROPOSAL.md section 4.5 set out to avoid.

**The design.**

- **One host.** One Oracle A1 VM (2 cores, 12 GB, Ubuntu 24.04 Arm64, a 150-200 GB boot volume) that is always on.
  Upgrade the account to Pay As You Go with a $1 budget alert, so it is not reclaimed between events.
- **The trigger.** A dispatcher service reads the app's DM, as today. `split <event>` starts
  `streamsplitter live` for that event; `stop <event>` and `list` manage it. The existing outbox posts to Slack,
  YouTube and Drive.
- **A fallback for YouTube blocks:** route only yt-dlp and ffmpeg through a home connection (a Tailscale exit node or
  small proxy on a Pi at a mentor's house). Keep the laptop path as the backup.

**Main risks, most serious first:**

1. **YouTube blocks or half-blocks the VM's IP.**
   - It can happen any day without warning.
   - The worst form is "not currently live" while it is live.
   - The fixes either risk an account (cookies), cost real money (residential proxy), or bring back a home
     dependency (exit node).
   - Twitch is probably fine but unmeasured.
2. **Two Arm cores may be too slow** for 1080p OCR plus 720p copies. Oracle halved the allowance in June 2026 and can
   cut it again.
3. **Oracle's lifecycle:**
   - idle reclamation between events
   - "out of host capacity" if the VM ever has to be re-created
   - unannounced rule changes
   - reported surprise account closures
4. **Terms.** YouTube's terms and the API Developer Policies already forbid what the recorder does; a server and any
   block workaround make it more visible. The team's Cloud project and channel are what is at stake. Get the
   organisers' permission.
5. **Security.** The VM holds upload rights to the team's channel and a Slack bot token. Encrypt the secrets, keep
   inbound ports closed, and allowlist who can command it.

**What the user would have to do:**

- **Sign up for Oracle Cloud:** phone plus credit card; nothing is charged unless upgraded. Pick the home region
  carefully (one chance). Turn on multi-factor sign-in.
- **Create the VM:** VM.Standard.A1.Flex with 2 OCPUs and 12 GB, Ubuntu 24.04 aarch64, a 150-200 GB boot volume, and
  your SSH key. Retry another availability domain if it says "out of host capacity".
- **Optionally upgrade to Pay As You Go** with a budget alert, after the test passes.
- **Google:** make sure the OAuth consent screen is **In production** (not Testing).
- **Copy the team settings file** to the VM and type its passphrase there.
- **Slack:** nothing new for polling. For Socket Mode: turn it on, make an app-level token, subscribe to
  `message.im`, and reinstall (with an admin's approval if the workspace requires it).
- **Decide who may command the server** (their Slack user IDs).
- **Nothing new on the rules:** organisers' permission and YouTube's terms are as now.

**Effort for us** (rough, agent-days; elapsed time is longer because of the soak test):

| Work | Days |
|---|---|
| Server install path (run.sh on Arm Linux, systemd units, encrypted secrets, server mode) | 1-1.5 |
| Dispatcher (`split`, `stop`, `list`, auto-stop, allowlist, TBA lookup, per-event settings, one DM reader, playlist per event) | 3-4 |
| Performance work, only if the CPU test fails | 1-3 |
| Home exit-node or proxy fallback for YouTube | 0.5-1 |
| `docs/setup-cloud.md` (Oracle sign-up, VM, hardening) and tests | 1-1.5 |
| **Total** | **about 6-11** |

### The first test: settle both unknowns before building anything

Cost: $0 and about an hour of the user's time (sign-up and VM); the rest is unattended. It needs no new code.

1. **Install.** On a free Oracle A1 VM (2 OCPU, 12 GB, Ubuntu 24.04 Arm64), run `sh run.sh --install-only`. That
   checks the Arm install path.
2. **YouTube access (the biggest unknown).** Run `streamsplitter record <link> --tag cloudtest` with Slack and YouTube
   off.
   - **Best:** the MadTown 2026 stream, 2026-11-06 to 11-08, alongside the laptop that is publishing. If the VM is
     not ready in time, use any 24/7 YouTube live channel, plus one channel that goes offline and comes back.
   - **Run it** for at least 8 hours a day on 3 or more days, including waiting periods (the 45-s live check).
   - **Pass:**
     - no "Sign in to confirm you're not a bot", no 403, no false "not currently live"
     - recording gaps only where the laptop's recording has them too (compare the two `index.json` files)
   - **Also** record a Twitch channel for 30 minutes.
3. **CPU (independent of YouTube).** Copy one recorded hour of chunks from the test PC (for example the MadTown 2024
   `mttd24_live2` chunks) to the VM. Follow them at live pace with `streamsplitter live --no-record`. Then make one
   720p copy while the follower runs.
   - **Pass:**
     - median "read in" of 12 s or less, 95th percentile under 20 s, no growing backlog
     - the 720p copy in 90 s or less
     - peak RAM noted

If step 2 fails on Oracle, the cloud plan becomes "Oracle plus a home exit node". Or the honest answer is a Pi or old
PC at a team member's home or the shop (section 2b), triggered from Slack the same way. If step 3 fails, decide
between the code changes in section 4 and that home machine.

An optional zero-sign-up pre-check is a 30-minute GitHub Actions job that runs the recorder. That is testing the
project's own software, which the terms allow. It only tells you about GitHub's addresses, not Oracle's.

---

## Sources

All read 2026-11-05. The date a page shows is given where it has one.

**yt-dlp and YouTube recording**
- PO Token Guide (edited 2026-07-12): https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
- Extractors wiki, YouTube section (account-ban warning, rate limits): https://github.com/yt-dlp/yt-dlp/wiki/Extractors
- FAQ (edited 2025-11-18): https://github.com/yt-dlp/yt-dlp/wiki/FAQ
- EJS / JavaScript runtimes (edited 2026-07-12): https://github.com/yt-dlp/yt-dlp/wiki/EJS
- #15012 External JavaScript runtime now required (2025-11-12): https://github.com/yt-dlp/yt-dlp/issues/15012
- #10128 Sign in to confirm you're not a bot (2024-06-07, pinned): https://github.com/yt-dlp/yt-dlp/issues/10128
- #12475 datacenter IPs, maintainer comments 2025-02-25/26 and 2026-02-12: https://github.com/yt-dlp/yt-dlp/issues/12475
- #16072 "not currently live" on a flagged datacenter IP (2026-02-26): https://github.com/yt-dlp/yt-dlp/issues/16072
- #16747 "YouTube seems to have started blocking by IP" (2026-05-17): https://github.com/yt-dlp/yt-dlp/issues/16747
- rainrag #41, server IP blocked across all clients (2026-08-21): https://github.com/yidakra/rainrag/issues/41
- Oracle Cloud server and Cloudflare WARP (2025-07-14): https://blog.arfevrier.fr/leveraging-cloudflare-warp-to-bypass-youtubes-api-restrictions/
- Alibaba Cloud server, residential proxy (2026): https://dev.to/osovsky/i-was-building-a-cloud-video-service-youtube-turned-me-into-an-ip-trafficker-1l9o
- Hugging Face Space blocked: https://huggingface.co/spaces/openai/whisper/discussions/121
- Third-party server guide (not affiliated with yt-dlp): https://ytdlp.org/guides/run-yt-dlp-server
- YouTube live latency (Help): https://support.google.com/youtube/answer/7444635

**Twitch**
- Streamlink Twitch plugin (ads, client-integrity tokens): https://streamlink.github.io/cli/plugins/twitch.html

**Google, YouTube API, Drive**
- YouTube API Developer Policies: https://developers.google.com/youtube/terms/developer-policies
- OAuth 2.0 overview (7-day Testing tokens): https://developers.google.com/identity/protocols/oauth2
- OAuth 2.0 policies (client type per platform, encrypted tokens at rest): https://developers.google.com/identity/protocols/oauth2/policies
- OAuth for TV and limited-input devices (allowed scopes): https://developers.google.com/identity/protocols/oauth2/limited-input-device
- Drive API usage limits (750 GB a day): https://developers.google.com/workspace/drive/api/guides/limits
- Earlier StreamSplitter research on uploads: [youtube.md](youtube.md)

**Hosts**
- Oracle Always Free resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle Free Tier overview (trial, card, home region): https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm
- Oracle compute shapes (A1: 1 OCPU = 1 core, 1 Gbps per OCPU): https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm
- InfoQ on the A1 halving (2026-07-03): https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/
- Idle reclamation explainer (2026-02-03): https://oracommit.blogspot.com/2026/02/understanding-oci-always-free-compute.html
- Google Cloud free features: https://docs.cloud.google.com/free/docs/free-cloud-features
- AWS Free Tier: https://aws.amazon.com/free/ ; FAQ: https://aws.amazon.com/free/free-tier-faqs/ ; EC2 free tier types: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html ; 100 GB egress: https://aws.amazon.com/ec2/pricing/on-demand/
- Azure free services (2026-03-25): https://learn.microsoft.com/azure/cost-management-billing/manage/create-free-services
- GitHub additional product terms (effective 2026-08-27): https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features
- GitHub Actions limits: https://docs.github.com/en/actions/reference/limits ; runners: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- GitHub Codespaces billing: https://docs.github.com/en/billing/concepts/product-billing/github-codespaces ; timeout: https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces
- Hugging Face Spaces: https://huggingface.co/docs/hub/spaces-overview
- Render free: https://render.com/docs/free ; Railway: https://railway.com/pricing ; Fly.io: https://docs.fly.io/about/pricing ; Koyeb: https://www.koyeb.com/docs/faqs/pricing
- Tailscale pricing: https://tailscale.com/pricing
- PyPI files: https://pypi.org/project/deno/#files , https://pypi.org/project/onnxruntime/#files , https://pypi.org/project/opencv-python-headless/#files

**Slack**
- Socket Mode: https://docs.slack.dev/apis/events-api/using-socket-mode
- apps.connections.open: https://docs.slack.dev/reference/methods/apps.connections.open
- Rate-limit change for non-Marketplace apps (2025-05-29): https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps
- Clarification, internal apps unaffected (2025-06-03): https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity
- Free plan limits: https://slack.com/help/articles/115002422943-Message-file-and-app-limits-on-the-free-version-of-Slack
- GitHub repository_dispatch: https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event
