# Game Guide

A desktop guide for PC games: wiki pages, walkthroughs and maps on the left, a local AI chat on the right that answers from those sources and cross-checks them.

## Features

- **Every PC game on Steam and Epic.** A ranked catalog, most popular first down to indie games, built from SteamSpy, Steam charts and the Epic Games Store. It auto-detects the Steam and Epic games you have installed.
- **All the sources for a game in one place:**
  - game wikis (wiki.gg, Fandom), PCGamingWiki, Wikipedia and the Steam store page;
  - Steam Community guides;
  - guide websites found with a web search (DuckDuckGo, with Bing as a fallback).
- **Missions sidebar.** Lists missions in story order from a wiki quest list, a walkthrough hub, or the installed game's own files (UE5 IoStore index). Quests you've started are marked from your save. Missions can be renamed and searched.
- **Mission references.** Screenshots, the matching sections of other walkthroughs, step-specific guides, map screenshots and video search for each step.
- **Guide index.** Each game gets these sections:
  - walkthrough;
  - side quests;
  - secrets and collectibles;
  - maps, including searchable MapGenie markers;
  - cheats (official single-player codes and console commands only).

  Each game also gets a coverage score.
- **Background builder.** `prebuild.py` works down the catalog and prepares guide indexes ahead of time, with polite rate limits.
- **Local AI chat.** Runs on Ollama and answers only from the sources it retrieved, with inline citations.

## Requirements

- Windows, Python 3.12 (standard library only)
- Google Chrome or Microsoft Edge (the app opens in an app window)
- [Ollama](https://ollama.com) with a chat model, for example `mistral-small3.2:24b`

## Run

```bat
Start_GameGuide.bat
```

or `py -3.12 server.py` (add `--no-window` to run only the server at http://127.0.0.1:8765).

Build the catalog and guide indexes in the background:

```bat
py -3.12 catalog.py
py -3.12 prebuild.py
```

## Shared guide data (GitHub Actions)

You don't need to build the catalog on your own PC. The **Build guide data** workflow does it:
- It runs every 6 hours on GitHub's machines.
- It refreshes the Steam + Epic catalog once a week.
- It builds guide indexes down the ranking until its time budget runs out, then continues on the next run.
- It publishes the results to the [`data` branch](../../tree/data) as `catalog.json.gz` and `guides.json.gz`.

Game Guide downloads that data when it starts (`sync.py`), so every copy of the app gets it. You can start a run by hand from the **Actions** tab (*Run workflow*).

Nintendo games are not included.

## Files

| File | Purpose |
| --- | --- |
| `server.py` | HTTP server, wiki discovery, page cleaning, chat retrieval, launch |
| `extras.py` | Steam Community guides, wiki mission lists |
| `webguides.py` | Web search, guide-site pages, walkthrough-hub missions, mission references |
| `localquests.py` | Quest lists from an installed UE5 game's files and save |
| `guideindex.py` | Per-game guide index (sections, MapGenie markers, coverage) |
| `catalog.py` | Ranked Steam + Epic catalog |
| `prebuild.py` | Background guide-index builder |
| `ci.py`, `publish.py`, `sync.py` | Cloud build, data bundles, and the app-side download |
| `web/` | Front end (`app.js`, `refs.js`, `browse.js`, `style.css`) |

Content shown in the app belongs to its wikis, sites and authors. It is fetched live for personal reference.
