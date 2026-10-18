# News

## 2026-09-30 News now reaches you through your agent

Varyz now checks varyzai.com for news once an hour while you use it, and your agent mentions
each new post once, in one line at the end of a reply. Run `varyz news off` to stop it.

The check is a plain request for varyzai.com/news.json. It sends nothing about you or your
archive: the website sees only the request itself, as it would for any page you open. Posts
older than 30 days are never passed on, so a new install does not start with a backlog.
