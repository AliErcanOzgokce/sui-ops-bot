# How the Sui Ops bot works (for a dev lead)

A plain-language tour. This describes what the bot actually does today. Where
something is not built yet, it says so.

The bot keeps one shared Google Sheet (the "Open Questions" tab) tidy for you. It
has two halves that use the same sheet:

- an **auto-tracker** that quietly watches a Slack channel, and
- a set of **Claude commands** (the "MCP") you type in plain English.

## 1. How a question gets into the bot

There are two ways.

**A. It happens automatically in Slack.** When someone posts a new message in the
watched channel, the bot looks at it. It ignores short one-liners and small talk
(anything under about 25 characters, and things like "thanks" or "ok"). For a real
message, it asks Claude one question: "is this a new developer problem someone needs
help with?" If yes, the bot:

- adds a row to the sheet, and
- replies in that Slack thread: "Logged as #NN ... open row", with a link.

If it got it wrong and that was not really a question, react with :x: on the bot's
reply and it deletes the row.

**B. You tell it to, through Claude ("log this").** Inside Claude you say something
like: *"post this to devleads, source is <link>."* That runs the `post_message`
tool: it posts a tidy message to the channel and adds the sheet row for you. A
**source is required** (a link or where it came from). If you do not give one, it
asks for it first.

## 2. How it classifies, and what you see

For every question, the bot asks Claude to tag it on two labels plus a priority:

- **product**: which thing it is about (for example DeepBook, Walrus, Seal, SDK,
  Bridge). There are 15 options.
- **type**: what kind of ask it is (Question, Open PR, Bug, Feature Request,
  Communication).
- **priority**: High, Medium, or Low.

When you post through Claude, it fills these in for you automatically, but you can
override any of them by just saying it (for example "Walrus, Bug"). The auto-tracker
always fills them in on its own.

What you see:

- In Slack, the message shows a small **badge**, for example `#43 · Walrus · Bug ·
  High`, plus the original text, the source, and a link to the sheet row.
- In the sheet, a **row** with its own **ID number** (the `#43`) and two columns,
  **Product** and **Type**.

Note: rows that existed before this feature was added are left as "Unclassified".
Only new questions get product and type. There is no bulk back-fill of old rows.

## 3. The commands a lead uses

Two kinds. Quick ones you type in Slack, and richer ones you ask Claude.

**In Slack (quick status only):**

- `!status`  a short summary: how many open, a breakdown by product, how many are
  aging, and the oldest one.
- `!open`  the list of open items.
- `!aging`  the items that have been open too long (default over 3 days).

(These also work as `/status`, `/open`, `/aging`, or by @-mentioning the bot.)

**Through Claude (the fuller toolbox):**

- **Weekly report**  *"give me the weekly report."* Lists the open backlog grouped
  by product. It only shows you a preview; it posts to the channel **only when you
  confirm**.
- **Filter by product or type**  *"show open Walrus questions"* or *"open bugs."*
  Works on the weekly report and on status checks.
- **Check status**  *"what's the status of 12, 13?"* For each one it reads the
  thread and tells you if it looks Solved, Forwarded, or still Open, plus the last
  reply. You can also check by filter instead of IDs.
- **Mark solved**  *"mark 12 solved."* Adds a check mark on the Slack message and
  closes that row in the sheet.
- **Ping**  *"ping 12 and 13."* Posts a gentle follow-up in each thread, tagging the
  owner.

IDs are the sheet's own numbers; `12`, `#12`, or a list `12, 13, 15` all work.

## 4. What you do vs what is automatic

**The bot does on its own:**

- Spot new questions in the channel and log them.
- Fill in product, type, and priority.
- Watch a logged thread. If a reply (or a check-mark on the original message) looks
  like an answer, it asks Claude, and if it seems resolved it posts "Looks resolved
  ... react to confirm" and adds a note on the row. It does **not** close anything by
  itself.

**You do (the human decisions):**

- Confirm a close: react with a check-mark on the bot's "react to confirm" message,
  and it closes the row. Nothing is ever auto-closed.
- Discard a false alarm: react :x: on the bot's log note.
- Post questions through Claude, run reports, filter, check status, mark solved, and
  ping owners.

In short: the bot captures, labels, and nudges. A person always makes the call to
close.

## Not built yet

- **No scheduled or automatic weekly report.** The weekly report only runs when you
  ask for it, and posts only when you confirm. There is no timer that sends it on a
  schedule.
- **No back-fill** of product/type onto rows created before this feature.
- The quick Slack commands cover status only (`status`, `open`, `aging`). Posting,
  reports, filters, mark-solved, and ping are done through Claude, not as Slack
  commands.
