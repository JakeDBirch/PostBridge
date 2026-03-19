# PostBridge Session Handoff

## Pending Task: Reformat Mike Crites Script

**Source file:** `C:\Users\jaked\Downloads\Mike Crites Script.txt`
**Goal:** Convert to PostBridge format and write to `C:\Users\jaked\Downloads\Mike Crites Script - PostBridge.txt`

### PostBridge Script Format (verified from parsers.py)

```
Blood Trails: Who Murdered Mike Crites

--- INTERVIEW SESSIONS START ---
[TOKEN_NAME]           // one per line, uppercase + underscores only
--- INTERVIEW SESSIONS END ---

--- EPISODE ASSETS START ---
[PART_0_NARRATOR]      // VO asset IDs, one per part (+intro)
[PART_1_NARRATOR]
--- EPISODE ASSETS END ---

@PART Part Name Here   // increments part counter; name is free text

@VO PART_1_NARRATOR    // must match PART_\d+_[A-Z]+ regex
Narrator paragraph one.

Narrator paragraph two (blank line = paragraph break within same VO block).

@PULL TOKEN_NAME [HH:MM:SS-HH:MM:SS]    // timecodes zero-padded to HH:MM:SS
"Quote text here, no blank lines within."

// comment lines are ignored by parser
```

**Key rules:**
- `@PULL` requires token name from the declared list + timecodes in `[HH:MM:SS-HH:MM:SS]`
- `@VO` block ends when parser hits a line starting with `@` or `//`
- `@PULL` quote ends at first blank line after text starts — no blank lines inside quotes
- Token names: `[A-Z0-9_]+` — uppercase, digits, underscores only
- VO IDs: `PART_\d+_[A-Z]+` — e.g. `PART_0_NARRATOR`, `PART_1_NARRATOR`
- Plain text lines that don't start with `@` or `//` are IGNORED by parser (only in `@VO`/`@PULL` blocks is text collected)
- `cur_part` starts at 0, so intro content before first `@PART` uses `PART_0_NARRATOR`

### Timecode Conversion

Original format → PostBridge format:
- `M:SS` or `MM:SS` (no hours) → `00:MM:SS` e.g. `26:48` → `00:26:48`
- `H:MM:SS` → `0H:MM:SS` e.g. `1:00:13` → `01:00:13`
- All fields zero-padded to 2 digits

### Token Name Mapping

| Original Script Name       | PostBridge Token       |
|----------------------------|------------------------|
| Local News                 | LOCAL_NEWS             |
| Connie Take 1              | CONNIE_TAKE_1          |
| Connie Take 3              | CONNIE_TAKE_3          |
| Connie L Crites            | CONNIE_L_CRITES        |
| Shane Allen                | SHANE_ALLEN            |
| Dutton                     | DUTTON                 |
| Gloria                     | GLORIA                 |
| Randy                      | RANDY                  |
| The Shot                   | THE_SHOT               |
| Shane Shaw                 | SHANE_SHAW             |
| Ford Testimony             | FORD_TESTIMONY         |
| Ford Trial Opening         | FORD_TRIAL_OPENING     |
| Dennis Shaw                | DENNIS_SHAW            |
| Leon Ford Hung Jury        | LEON_FORD_HUNG_JURY    |
| Leon Ford Released         | LEON_FORD_RELEASED     |
| IMG_0029                   | IMG_0029               |

### Special Cases

1. **Multi-paragraph quotes** (e.g. Connie Take 1 26:48-28:11): Remove blank lines inside the quote — parser stops collecting at first blank line after text.

2. **Dennis Shaw clips** — no timecodes given in source (`Dennis Shaw ()`). Use placeholder `[00:00:00-00:00:01]` and add `// TODO: timecode needed` before the `@PULL` line.

3. **Connie Take 3 continuation** after musical interlude in Part 7 — no timecode. Same placeholder treatment.

4. **Ford Testimony Q&A** and **Dennis Shaw Q&A** — multi-person exchanges within one clip. Collapse into single `@PULL` block with no internal blank lines.

5. **Footnote markers** `[a]`–`[i]` — strip from all source names and quote text.

6. **Editorial notes** (`[c]`, `[f]` footnotes) — add as `// NOTE:` lines before the relevant `@PULL`.

7. **`[Music or pause]`**, `[Pause, musical interlude]`, `[Musical interlude]` — keep as `// [Music or pause]` etc.

8. **Em dash separators** (`—`) — keep as-is (ignored by parser, useful for human readability).

9. **Bottom footnote section** (lines 973–981 of source file — the Google Drive links and editor notes) — strip entirely.

### Episode Structure

- **Intro** (before @PART): teaser + Local News + Connie + Gloria + host intro line
- **@PART 1: The Hunter** — Shane Allen, Connie Take 1
- **@PART 2: Welcome to the Neighborhood** — Dutton, The Shot, Gloria
- **@PART 3: Missing** — Gloria, Dutton, Connie Take 1, Shane Allen
- **@PART 4: A Tale of Two Mikes** — Randy, Connie Take 1, Shane Allen, Connie Take 3, Gloria
- **@PART 5: The Investigation** — Shane Shaw, Dutton, Gloria, Shane Allen, Ford Testimony
- **@PART 6: Lies and Cable Ties** — Dutton, Shane Shaw, Connie L Crites, Gloria
- **@PART 7: The Defense of Leon Ford** — Ford Trial Opening, Shane Shaw, Ford Testimony, Dennis Shaw, Gloria, Connie, Leon Ford Hung Jury, Leon Ford Released
- **@PART 8: Hope** — Shane Shaw, Dutton, IMG_0029
- **Outro** — narrator/host sign-off

---

## Open Issue: VO Sync Failures

**Context:** `detect_sync_offset` (parsers.py) cross-correlates video embedded audio vs DAW reference audio to find the time offset needed when converting AAF → XML.

**What user confirmed:**
- Camera and DAW audio are separate, asynchronous — this is the whole reason sync detection exists
- Reference audio = high quality DAW recording (WAV), since the AAF references this
- No consistent pattern to failures (sometimes works, sometimes doesn't)
- Each source = ONE continuous video file + ONE continuous audio file per script section

**Likely diagnosis (not yet confirmed):** The `start_offset` passed to `detect_sync_offset` is derived from `src_in_secs` of the DAW timeline. The video file's timeline is offset from the DAW by an unknown amount (`v_offset`). If `v_offset` is large, seeking both the video and DAW audio to the same `start_offset` means the probe windows don't overlap temporally — the cross-correlation will fail or return noise.

**Suggested fix direction:** For VO sources (where v_offset is unknown), probe from position 0 in both files, or use a wider search range. Alternatively, use the existing successful sync result from one clip in a source to inform the seek position for others in the same source.

**Status:** Discussed but not yet implemented.

---

## Recent Code Fixes (already applied)

All fixes are in the current working directory `F:\PostBridge\PostBridge_Modular`.

- **parsers.py `_sig_words`**: Added roman numerals (i–xii) and pure digits to significant token set so `Part I`, `Part II`, `Part III` don't all match the same file
- **parsers.py `detect_sync_offset`**: Added `start_offset=0.0` param; seeks both files to `start_offset - 2.0s` before probing
- **engines.py `build_xml_from_pt`**: Fixed `v_offset` applied only to video src_in, not audio src_in (were both being shifted before)
- **main.py**: Full AAF step 2 UI redesign — two-row layout per source, reference audio dropdown visible per row, SYNC ALL button, `_on_aud_change` trace stale-dict bug fixed, `aud_disp` StringVar recreated fresh each rebuild, `_aaf_add_audio_batch` now calls `_rebuild_aaf_source_rows`

---

## Git Status at Handoff

Branch: `main`
Uncommitted changes: `engines.py`, `main.py`, `parsers.py`
