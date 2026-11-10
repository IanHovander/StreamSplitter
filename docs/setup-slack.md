# Setting up Slack

StreamSplitter can post your team's match videos to a Slack channel (for example `#errata`) a minute after each
match ends, and send one person a direct message (DM) when something needs attention. It posts as a small Slack app
called **StreamSplitter**.

**One app for the whole team.** One StreamSplitter app serves every laptop and every event, all season. Each app
uses one of the workspace's app slots (a free workspace has
[10](https://slack.com/help/articles/115002422943-Message-file-and-app-limits-on-the-free-version-of-Slack)), so
make it once. Other laptops use the same app, best through a team settings file (below).

The setup (`run.bat`, or `run.bat --section slack`) walks you through all of this; this page is the same steps with
more detail. Before you start, see [requirements-and-limits.md](requirements-and-limits.md).

## What you need

- A Slack account in the team's workspace, signed in in your web browser.
- Permission to add apps, or a workspace admin who approves the app (step 2 shows what that looks like). Ask days
  before the event: approvals can take a while.
- The channel for the videos, already created.

## Does your team already have the app?

The setup asks this first. Answer honestly: a second app takes another slot.

- **No:** follow steps 1 to 4 below.
- **Yes, and a teammate can send you a team settings file:** easiest. Answer yes when the setup asks "Do you have a
  team settings file from a teammate?" and type its passphrase, if it has one. Slack is then set up; you only pick
  who gets the
  alerts. (The teammate makes the file in the setup menu: *Share these settings with a teammate*.)
- **Yes, but no file:** you need the app's password, its **Bot User OAuth Token**. Open
  <https://api.slack.com/apps>, click **StreamSplitter**, then **OAuth & Permissions**, and copy the token (step 3).
  Not in your list? Ask whoever made the app to add you under **Basic Information** > **Collaborators**.

## 1. Create the app

1. The setup opens a Slack page with the app filled in (a long link starting
   `https://api.slack.com/apps?new_app=1&manifest_json=`).
2. Pick the team's workspace and click **Next**.
3. Slack shows the app's settings, already filled in. Click **Next**.
4. Slack shows a summary: an app called **StreamSplitter** with a list of permissions. Click **Create**.

You now see the app's **Basic Information** page.

If the page opens empty, do it by hand: at <https://api.slack.com/apps> click **Create New App**, then **From a
manifest**, pick the workspace, click **Next**, open the **JSON** tab, replace what is in the box with the contents
of `streamsplitter/publish/slack_manifest.json`, then **Next** and **Create**.

If Slack says the workspace has no room for another app, a workspace admin must remove an unused app first.

## 2. Install it in the workspace

1. On the left, click **Install App**.
2. Click **Install to** *your workspace*, then **Allow**.

**If the button says "Request to Install"**, your workspace needs an admin to approve new apps:

1. Click **Request to Install**, add a short note (for example "Posts our match videos to #errata"), and send it.
2. A workspace admin approves it in Slack. Slack then tells you (a Slackbot message and an email).
3. Back at <https://api.slack.com/apps>, click **StreamSplitter**, **Install App**, **Install to** *your
   workspace*, **Allow**.

While you wait, you can type `later` in the setup and finish the rest. When the app is installed, run
`run.bat --section slack` and answer that the app exists (do not create a second one).

## 3. Copy the Bot User OAuth Token (the app's password)

1. On the left, click **OAuth & Permissions**.
2. Under **OAuth Tokens**, copy the **Bot User OAuth Token**. It starts with `xoxb-`.
3. Paste it into the setup. It stays hidden as you paste.

Treat the token like a password: anyone who has it can post as the app. Do not paste it into Slack, email, a shared
document or a screenshot. StreamSplitter keeps it in the computer's password store (or, where there is none, a file
only your user can read) and never shows it. If it leaks, revoke it on the same page and install the app again for a
new token. A **User OAuth Token** (`xoxp-`) is the wrong one.

## 4. Invite the app to the channel

The setup joins a public channel by itself. For a private channel, open it in Slack and send
`/invite @StreamSplitter`.

## The contact person

The setup asks who gets the DMs: errors, warnings, and an alarm if StreamSplitter stops. They come from
**StreamSplitter** (in Slack under **Apps**). That person should have Slack notifications on their phone during the
event: the alarm is how you find out the recorder died.

The contact is StreamSplitter's **owner** in Slack, and an admin. They can reply to the app with commands (`help`
shows the few that matter right now, `help all` every one), for example `status`, `set stream <link>` to record a
different stream, `settings` to see and change every setting, `add @name` to let someone else start and stop
recordings, and `add admin @name` for a second admin. `set owner @name` sends the alerts to someone else; the old
owner stays an admin. With [Start from Slack](start-from-slack.md) on a computer, `split <event>` starts a recording
there and `stop` stops it. The setup asks for nobody else: admins add people later from Slack, and adding someone sends
them no message unless the admin says so. Replies come within about 20 seconds, and within a few seconds while you
keep writing.

## What the app may do

| Permission | Why |
|---|---|
| `chat:write` | post messages, and schedule or cancel the "StreamSplitter stopped" alarm |
| `files:write` | upload the match videos |
| `files:read` | find where a video was posted, so the results version goes in the same thread |
| `im:write` | open a DM with the owner and the people they add |
| `im:history` | read the replies to the app in those DMs (the commands) |
| `reactions:write` (optional) | put :eyes: on a command as soon as it is read, so you can see it was seen. Without it the app just answers. |
| `users:read`, `users:read.email` | find the owner by name or email during setup, and the people the admins add (`add @name`) |
| `channels:read`, `groups:read` | find the channel by name (a private one only after the invite) |
| `channels:join` | join a public channel by itself |

It cannot read channel messages, and it posts only where it was invited or joined. People write to it in its
**Messages** tab (in Slack under **Apps**, click StreamSplitter); the app's manifest keeps that tab open for messages.

**An app made before a permission was added** (for example `im:history`, added 2026-11-04): the setup notices and
walks you through it. At <https://api.slack.com/apps> click **StreamSplitter**, **OAuth & Permissions**, then under
**Scopes** > **Bot Token Scopes** click **Add an OAuth Scope** and pick the missing one. Then click **Reinstall to
Workspace** at the top and **Allow**. The token stays the same. `reactions:write` (added to the manifest 2026-11-10)
is optional: the setup does not ask for it, and an app without it keeps working; add it the same way if you want the
:eyes: on each command.

## Test it

The setup offers to post a short sample video, by DM to the contact or to the channel. If it shows up, Slack works.

## When StreamSplitter reports a Slack problem

| Message says | What to do |
|---|---|
| "Invite the StreamSplitter app to #..." | Step 4. |
| "Slack cannot find #..." | The channel was renamed, deleted or archived, or it is private and the app is not in it: step 4, or pick another channel with `run.bat --section slack`. |
| "Slack no longer accepts StreamSplitter's token" | The app was removed or the token copied wrong: steps 2 and 3, then `run.bat --section slack`. |
| "lacks the permission ..." | Add it and reinstall (see *What the app may do*). |
| "refuses large uploads" | Choose the 720p videos in `run.bat --section slack` (about 3 times smaller). |
| "does not let the app upload videos" or "does not let the StreamSplitter app post" | A workspace setting blocks it: ask a workspace admin. |
| "no connection to Slack" or "trying again later" | Nothing to do: the video waits and is posted when the internet is back. You get a DM if it keeps failing for 10 minutes. |
