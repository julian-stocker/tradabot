# The Discord bot — `/check`

One interactive command, answered from the same read-only layers everything else
uses.

```
/check symbol:NVDA
```

The bot replies in **#stocks** with a single embed: company quality, valuation,
market position, portfolio context, and what the data behind it is worth.

## Configuration

Four variables in `.env`. The Discord application already exists; these connect
this machine to it.

| variable | what it is |
|---|---|
| `DISCORD_APPLICATION_ID` | the application's ID, used to scope command registration |
| `DISCORD_BOT_TOKEN` | **secret** — the bot account itself |
| `DISCORD_GUILD_ID` | the server the command is registered to |
| `DISCORD_STOCKS_CHANNEL_ID` | the only channel `/check` answers in |

**The token is the whole account.** Anyone holding it can connect as the bot and
read every channel the bot can see, until it is regenerated. It is a `SecretStr`
from the moment it is read; it never appears in a log, an exception, a report or
an artifact. The three IDs are not secrets but are not published either.

Check configuration without connecting:

```
tradabot discord-bot --check-config
```

That prints presence only — never a value.

### Resetting the token

If it is ever exposed: Discord Developer Portal → your application → Bot →
**Reset Token**, put the new value in `.env`, restart the bot. The old token
stops working immediately; nothing else in Tradabot is affected.

### Re-inviting the bot

OAuth2 → URL Generator → scopes `bot` and `applications.commands`, then these
permissions and no others:

- View Channels
- Send Messages
- Embed Links
- Use Application Commands

**No privileged intent is required or requested.** The bot connects with
`Intents.none()`: slash commands arrive as *interactions*, routed to the bot by
Discord, so it never needs to read the server's messages. Message Content,
Server Members and Presence intents stay off — a bot that cannot read messages
cannot leak what it never received.

## Running it

```
tradabot discord-bot          # foreground, Ctrl-C to stop
```

As a background agent (written by `make ops-install`, started by you):

```
launchctl load -w ~/Library/LaunchAgents/com.tradabot.discord-bot.plist
launchctl unload -w ~/Library/LaunchAgents/com.tradabot.discord-bot.plist
```

It is a **daemon**, not a scheduled job: `KeepAlive` restarts it if it exits,
with a 30-second throttle so a crash loop backs off instead of spinning. It is
deliberately outside the twelve scheduled jobs — a bot crash stops no monitoring,
and a publisher failure does not disconnect the bot.

Verify:

```
launchctl list | grep tradabot        # 12 scheduled + discord-bot
pgrep -f "app.cli discord-bot" | wc -l   # exactly 1
```

The command is registered **guild-scoped**, so changes appear immediately rather
than propagating on Discord's global schedule. Registration happens on every
start; there is nothing to run by hand.

## When the laptop sleeps

**The bot cannot answer while the Mac is asleep.** There is no queue and no
catch-up: Discord shows "the application did not respond" for anything sent
during that window. On wake, `discord.py` resumes the gateway session and
`/check` works again within seconds.

This is not 24/7 availability, and nothing here should be described as such. The
scheduled publisher jobs behave differently — launchd runs those *after* wake —
but an interaction is only answerable while someone is waiting.

## What the answers mean

`/check` distinguishes several outcomes rather than collapsing them:

| state | meaning |
|---|---|
| `SUPPORTED` | prices and SEC fundamentals both available |
| `MARKET_DATA_ONLY` | priced, but no SEC company facts — typical of ETFs |
| `DATA_NOT_SYNCED` | the fact store has never been built; run `tradabot fundamentals sync` |
| `UNKNOWN_SYMBOL` | no instrument exactly matches |
| `MALFORMED_SYMBOL` | the input is not shaped like a ticker |
| `ANALYSIS_FAILED` | the lookup itself failed — distinct from having no data |

**Missing fundamentals is an absence of data, not a weak company.** The embed
says so in those words.

### Symbols are never substituted

`/check NVD` returns "symbol not found" and may *offer* `NVDA` as a possible
match. It will not analyse it. You have to send `/check symbol:NVDA` yourself.

The same holds across venues: `SAP.DE` is a Frankfurt listing and `SAP` is a US
ADR. They are different instruments with different currency, hours and tax
treatment, and nothing maps one to the other.

A confident, well-formatted report about the wrong company is worse than an
error, because an error is obvious.

### Why the card is rarely green

The embed colour comes from what *dominates*, not from a tally of good
characteristics:

- **yellow** when data limitations dominate;
- **red** when a severe present factual risk dominates;
- **orange** when unusual market activity dominates;
- **blue** otherwise.

A company with strong margins, net cash and a sound balance sheet still gets a
blue card, because a green card reads as approval and Tradabot makes no
recommendation. The green lives on the line that says "net cash", where it means
something specific.

## Limits and behaviour

- **One invocation, one visible answer.** The command defers immediately (Discord
  closes the window in three seconds) and edits that response when ready.
- **Bounded concurrency** — three simultaneous analyses. Beyond that you get
  "try again in a moment" rather than the laptop being buried.
- **No result cache.** A stale valuation served silently would be worse than a
  slow one; every answer states its own as-of date.
- `/check` outside #stocks gets a short ephemeral reply naming the channel.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| command missing in Discord | bot not running, or never registered | start it; registration happens on connect |
| "the application did not respond" | laptop asleep, or bot stopped | wake/restart; check `launchctl list` |
| `DISCORD BOT NOT CONFIGURED` | a variable missing or a malformed ID | the message names the variable |
| every symbol unknown | price database empty | run the market-data sync |
| fundamentals unavailable everywhere | fact store missing | `tradabot fundamentals sync` |
| two replies to one command | two bot processes | `pgrep -f "app.cli discord-bot"`, kill the extra |

Distinguishing the three failures that look alike:

- **DATA NOT SYNCED** — Tradabot knows the symbol; the fundamentals store is not
  built. Fix with a sync.
- **UNKNOWN SYMBOL** — no instrument matches what you typed. Check the ticker.
- **ANALYSIS FAILED** — the symbol resolved and the analysis broke. Check the bot
  log; nothing was changed.

## What it cannot do

`/check` is observational. It places no order, cancels nothing, closes nothing,
writes no monitoring state and touches no forward-experiment table. A structural
test asserts the package imports no execution client and contains no order
vocabulary — and a second test asserts it recomputes no financial figure, so it
can never disagree with the Advisor about the same company on the same day.
