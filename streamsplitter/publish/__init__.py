"""Publishing clips: Slack (the team's own matches), YouTube, a shared Google Drive folder and the team's wiki (every
match, by default), and notifying a person.

base.py (job + errors), slack.py, youtube.py, drive.py (Google Drive: the team's shared folder, smaller copies),
wiki.py (MediaWiki), templates.py (titles and descriptions), outbox.py (durable retries), notify.py (DMs, the
stopped-running alarm), router.py + publisher.py (which clip goes where; the live hook), commands.py (the contact's
Slack replies).
"""
