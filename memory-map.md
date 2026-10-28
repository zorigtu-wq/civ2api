# Civ2 MGE 5.4.0f — memory map (civ2api)

> This file is the source of truth for the memory addresses used by the agent API. The origin of each address (file:line) is given in the tables below.
> Status: **SRC** = confirmed in Civ2UIA source · **SAV** = cross-checked against the Civ2-clone SAV reader · **RUN** = verified on a running game · **?** = awaiting runtime check.

## Verification log

Runtime check: 2026-10-27, Windows 11 x64, Python 3.12.10 (64-bit), Civ2UIA active (`bUnitsLimit=0`, `Patch64BitOn=1`), save `Ab_Auto.SAV` (Deity, Americans, Turn 15, 3300 B.C.).

| Item | Address / offset | Status | How it was verified | Result |
|---|---|---|---|---|
| Exe build | size 2,489,344 | SRC + **RUN** | `civ2.exe` size, sha256 `edd3cead…120c5c` | ✅ matches |
| Units array base | 0x6560F0 | SRC+SAV + **RUN** | `units_base=0x6560f0`, 21 units; type/position of my 4 units matched the screen | ✅ (UIA units-limit off, so no relocation) |
| Unit +0x08 moves spent | i8, in move×roadMult units | SRC+SAV + **RUN** | Elephant moving NE: 0→3, `moves_left` 2.0→1.0; screen "Moves: 1" | ✅ |
| UnitType Move/HP scale | +0x0A / +0x0E | **RUN** | Move is already ×3 (Settlers 3, Elephant 6), HP already ×10 (Warriors 10, Settlers 20) | ✅ `move_scaled=True`, `hp_scaled=True` |
| Game.ActiveUnitIndex + UnitSelected | 0x655AFE / 0x6D1DA8 | SRC + **RUN** | After load, index 17 = Elephant, matched cursor (110,36); `unit_selected=False` while a popup is open | ✅ |
| HumanCivIndex | 0x6D1DA0 vs Game+0x1B | SRC + **RUN** | Both 5 (Americans) | ✅ |
| Turn civ (whose turn) | Game+0x1D | ? | 5 during my turn. Not checked during AI turns | ⏳ |
| Cities | 0x64F340 | SRC+SAV + **RUN** | 7 cities: Rome, Zimbabwe, Paris, Thebes, London, Little Bighorn, Ulundi (name, owner, size, production) | ✅ |
| Civ gold | Civ+0x02 | SRC + **RUN** | Americans 25 gold = status panel "25 Gold" | ✅ |
| Tribe name | RULES.TXT @LEADERS[Civ+0x06] | **RUN** | "Americans" = popup "Abe Lincoln of the Americans" | ✅ (default name) |
| Popup pointer + title/lines | 0x6CEC84, +0x134, +0x230 | SRC + **RUN** | LOADOK: "Difficulty Level: Deity" / "Emperor Abe Lincoln of the Americans" / "3300 B.C." | ✅ |
| Popup HWND | `[[GI+0x50]+4]` | SRC + **RUN** | `popup.hwnd` equals the "Difficulty Level: Deity" window from EnumWindows | ✅ |
| LoadedTxtSectionName | 0x6CECB0 | **RUN** | `LOADOK`, `MAINMENU` — match GAME.TXT sections | ✅ |
| Popup button controls | Dialog+0x274 (+0x3C × i, HWND@+0x1C), NumButtonsStd@+0x30 | **RUN** | OK/Cancel button HWNDs matched the child windows. Std button text is not in ButtonTexts | ✅ |
| Popup @options lines | — | **RUN** (partial) | Each line is an `MSControlClass` child. Under Patch64Bit, `GetWindowLong(hwnd,0)` is a double pointer rather than a direct TControlInfo, so lines are currently matched to GAME.TXT `@options` **by position** (top to bottom). MAINMENU option 4 = "Load a Game" opened correctly | ⚠️ works, but the highlighted line can't be read |
| Keystroke (PostMessage) | — | **RUN** | ENTER to popup → closed; NUM9 on map → unit moved. Game does not need to be in the foreground | ✅ `post` mode is sufficient |
| Village (goody hut) | MapHeader+0x08 Seed + formula + tile byte5 high nibble == 0xF | **RUN** (partial) | Formula: Civ2-clone `Tile.cs` (Apolyton "hut pattern"). Special resources computed from the same seed matched the 2 Pheasants on screen at (113,45), (109,47) → seed=2 and coordinates are correct. The explored area has 4 formula points, all owned (already taken), consistent with no village being visible | ⚠️ seed/coords ✅, village not directly observed yet |
| Move cost (terrain) | RULES.TXT @TERRAIN × road_mult | **RUN** | Chariot (111,43)→(111,45) Grassland: planned 3 = actual 3 MP | ✅ |
| Move cost (river) | river↔river | **RUN** | Settlers (112,48)→(112,46), both river tiles: actual 3 MP (plain terrain cost) | ✅ MGE has **no** river movement bonus → `RIVER_AS_ROAD=False` |
| Move cost (road) | road↔road = 1 | ⏳ | To check from `cost_log` on the first road move | ⏳ |
| Save/Load | Menu: Save Game 684, Load Game 685 (`GetMenu` cross-process) | **RUN** | `saves/test_save_1.sav` 137 KB created, SAVEOK popup shown; load → LOADOK popup → loaded | ✅ |
| Unit ID after load | U+0x1A | **RUN** | After load, IDs 5,9,12,17 → 28,32,35,41; slot indices (4,8,11,17) unchanged | ✅ IDs are not stable → track by slot+type+owner |

### Runtime additions (2026-10-27, turns 21–25)

| Item | Address / offset | Status | How it was verified | Result |
|---|---|---|---|---|
| City.Workers | City+0x30 (u32) | **RUN** | Clicked each tile on Washington's Resource Map and read which bit changed. Bit 20 = centre, bits 26+ = specialist count (2 entertainers → 2) | ✅ |
| City.Specialists | City+0x16 (u32) | **RUN** | Turning workers into entertainers: 0 → 0x01 → 0x05 (2 bits each, 1 = entertainer) | ✅ |
| City tile bit order | — | **RUN** | 0–7 = NE(1,-1), E(2,0), SE(1,1), S(0,2), SW(-1,1), W(-2,0), NW(-1,-1), N(0,-2); 8–11 = (2,-2), (2,2), (-2,2), (-2,-2); 12–19 = (1,-3), (3,-1), (3,1), (1,3), (-1,3), (-3,1), (-3,-1), (-1,-3); 20 = centre. 4 tiles were unexplored, so derived from the pattern | ✅ (16/20 direct, 4 by pattern) |
| Raster offset table | 0x630D38 (dx i8[21]), +24 (dy) | **RUN** | Found by memory search. Row-major, centre at index 10. **Not** the worker bit order | ℹ️ |
| List-box popup items | Dialog+0x228 first item, +0x220 selected, +0x22C last | **RUN** | PRODUCTION popup: Settlers, Warriors, Barracks, Granary, Hanging Gardens = matched the screen. TListItem: Value@+4 (unit = id, improvement = 62+id), Text@+8 ("Name \|cost..."), Next@+0x10, Prev@+0x14. Research Goal list (86 techs) also read | ✅ |
| Close City Window | — | **RUN** | Sending ESC to the main window closes it. While open, focus sits on a child window | ✅ |
| Open a city | Advisors/City Status (F1) | **RUN** | Each row is 24.1/421 high, first row at 107/421, name at x=180/622. Clicking a row opens the City Window. Row order = city table order | ✅ (≤11 cities, scrolling not done yet) |
| City Window buttons | MSControlClass child | **RUN** | Top row Buy \| Change; bottom 2×3 grid Info, Map, Rename / Happy, View, Exit | ✅ |
| Resource Map geometry | City Window 976×681 | **RUN** | Centre tile (159,237), x step 38 px, y step 19 px | ✅ |
| End turn | — | **RUN** | ENTER works when no unit is selected. The game ignores the menu "End Player Turn" command | ✅ |
| Village → city | — | **RUN** | Campaign goal="city": 10 cities from 10 villages (1–8 attempts) | ✅ |

### Gotchas found at runtime
- **Start Civ2 only via `Civ2UIALauncher.exe`.** Launching `civ2.exe` directly started a second instance without UIA and the first one closed. If two `civ2.exe` are running, `attach()` picks the one with a visible window.
- **Patch64Bit:** on 64-bit Windows a control's `TControlInfo*` lives at window long 0 (as a double pointer), not at `cbWndExtra-8`. Reading `TControlInfoRadioGroup` requires unpacking that pointer structure further.
- **x < 0 while a unit moves:** Civ2 temporarily makes x negative while sliding a unit between tiles. That looks like a dead unit, so Session holds "gone" events for 1.5 s and merges them into `unit_moved` if the unit reappears.
- **The City Window blocks orders:** it opens automatically after founding a city, and while open unit keys go to it and the unit doesn't move. The runner and campaign now detect and close it.
- **Bridge console:** in console mode, QuickEdit blocks `print()`. So the bridge runs under `pythonw` (no window) and logs to `logs/bridge.log`.

---

Target: `CIV2.EXE`, 2,489,344 bytes, 32-bit, image base 0x00400000 (non-relocatable, so all addresses below are absolute VAs usable directly with `ReadProcessMemory`).

Civ2UIA targets exactly this build. Its launcher refuses any other exe size: `Civ2-UI-Additions/src/Civ2UIAL_Proc.pas:361-362` ("Game version Multiplayer Gold Edition 5.4.0f (Patch 3) supported only"). So every address in section (a) applies to this executable.

Sources (cloned locally):
- `Civ2-UI-Additions/` (FoxAhead, Delphi). Key files are `src/Civ2Proc.pas` (data addresses and structure-size asserts), `src/Civ2Types.pas` (record layouts), and `src/Civ2ProcImplF.inc` / `src/Civ2ProcDeclF.inc` (function addresses and signatures).
- `Civ2-clone/` (axx0, C#). The SAV reader is `Engine/src/OriginalSaves/Read.ClassicSav.cs`, and the year formula is in `Engine/src/Date.cs`.

All Delphi records are `packed record`, so there is no padding. Civ2UIA asserts these sizes at startup (`src/Civ2Proc.pas:216-240`): TUnit=$20, TCity=$58, TCiv=$594, TGame=$14A, TDialogWindow=$2F4, TMapWindow=$3F0, TCityWindow=$16E0.

Types: Byte=u8, ShortInt=i8, Word=u16, SmallInt=i16, Integer=i32, Cardinal=u32, PChar/pointers=u32. All values are little-endian.

---

## 0. Critical caveats (read first)

1. **Civ2UIA relocates the units array when "Units limit" is enabled.**
   - The original array is at 0x006560F0. With `bUnitsLimit` on, Civ2UIA allocates a new heap block and rewrites 2,391 code operands to point at it (`src/Patches/UiaPatchUnitsLimit.pas:2423-2464`).
   - The launcher also always copies its `TUIAOptions` block onto 0x006560F0 at process start (`src/Civ2UIA_Options.pas:74`, `src/Civ2UIAL_Proc.pas:318` and `:414`). In a vanilla or UIA-without-units-limit run this is overwritten once a game initialises the units array.
   - If units limit is on, find the real base this way: read the dword at a patched operand site, e.g. 0x004108F8 (the first entry of `GUnitLimitPatchAddr`, `UiaPatchUnitsLimit.pas:29`). Compare it with the same dword in the unmodified exe file to get `Diff`, then compute `Units = 0x006560F0 + Diff`.
   - Unit indices stay 0-based. Civ2UIA leaves one spare slot before element 0 (`Inc(PUnit(NewUnitsAreaAddress), 1)`, line 2436).
2. **Coordinates use Civ2's "doubled-X" isometric system.** A valid tile has `(X + Y)` even. `MapHeader.SizeX` is the doubled width.
3. **Array liveness:**
   - Iterate units `0 .. Game.TotalUnits-1` and skip records with `ID == 0`. Examples: `src/Civ2UIA_PathLine.pas:154-157`, `src/Patches/UiaPatchMapWindow.pas:61-62`.
   - Iterate cities `0 .. Game.TotalCities-1` and skip records with `ID == 0`. Example: `src/Civ2UIA_SortedCitiesList.pas:68-70`.
4. **Reads are not atomic** against the game thread. Read whole arrays in one RPM call and treat a torn read as possible (re-read if fields look inconsistent).

---

## (a) Addresses CONFIRMED in Civ2UIA source

### a.1 Global pointers and variables (`src/Civ2Proc.pas:148-210`)

| Name | Address | Type | Notes | Source line |
|---|---|---|---|---|
| Game | 0x00655AE8 | TGame (0x14A bytes) | Main game-state record; see a.2 | Civ2Proc.pas:170 |
| Units | 0x006560F0 | TUnit[0x800] (2048 × 0x20) | See a.3 (and caveat 0.1) | Civ2Proc.pas:208, Civ2Types.pas:1278 |
| Cities | 0x0064F340 | TCity[0x100] (256 × 0x58) | See a.4 | Civ2Proc.pas:153, Civ2Types.pas:1329 |
| Civs | 0x0064C6A0 | TCiv[8] (8 × 0x594) | Index 0 = barbarians; see a.5 | Civ2Proc.pas:160, Civ2Types.pas:1418 |
| UnitTypes | 0x0064B1B8 | TUnitType[0x3E] (62 × 0x14) | Rules @UNITS; see a.7 | Civ2Proc.pas:210, Civ2Types.pas:1231 |
| Improvements | 0x0064C488 | TImprovement[67] (× 8) | Rules @IMPROVE (index 0 = "Nothing") | Civ2Proc.pas:174, Civ2Types.pas:1186 |
| RulesCivilizes (techs) | 0x00627680 | TRulesCivilize[100] (× 0x10) | Rules @CIVILIZE | Civ2Proc.pas:196, Civ2Types.pas:1176 |
| Leaders | 0x006554F8 | TLeader[1..21] (× 0x30) | Rules @LEADERS | Civ2Proc.pas:175, Civ2Types.pas:1440 |
| Cosmic | 0x0064BCC8 | TCosmic (0x16) | Rules @COSMIC; byte +0 = RoadMovementMultiplier | Civ2Proc.pas:163, Civ2Types.pas:952 |
| Commodities | 0x0064B168 | Integer[] | String indices of caravan commodities | Civ2Proc.pas:162 |
| ScenarioParameters | 0x0064BC60 | TScenarioParameters | +0x52 TechParadigm, +0x54 TurnYearIncrement, +0x56 StartingYear, +0x58 MaximumTurns (computed from the record) | Civ2Proc.pas:197, Civ2Types.pas:1071 |
| MapHeader | 0x006D1160 | TMapHeader (14 bytes) | See a.6 | Civ2Proc.pas:182 |
| MapData | 0x00636598 | **pointer** → TMapSquare[Area] (6 bytes each) | Dereference first | Civ2Proc.pas:181 (`MapData: ^PMapSquares`, line 52) |
| MapCivData | 0x006365C0 | **8 pointers** (u32[8]) → byte[Area] per civ | Per-civ *known* terrain improvements | Civ2Proc.pas:180, Civ2Types.pas:1095 |
| HumanCivIndex | 0x006D1DA0 | Integer | The local human player's civ (view perspective). **0xFFFFFFFF (-1) in hot-seat between players** (UiaPatchMultiplayer.pas:70) | Civ2Proc.pas:173 |
| UnitSelected | 0x006D1DA8 | LongBool (u32) | Non-zero = a unit is selected (move mode, not view mode). Always check it before trusting `Game.ActiveUnitIndex` | Civ2Proc.pas:209; usage Civ2UIA_PathLine.pas:92 |
| CursorX / CursorY | 0x0064B1B4 / 0x0064B1B0 | SmallInt | Map cursor tile | Civ2Proc.pas:165-166 |
| CurrPopupInfo | 0x006CEC84 | **pointer** → TDialogWindow (nil = no popup) | Current modal popup; see section (d) | Civ2Proc.pas:164 |
| CityWindow | 0x006A91B8 | TCityWindow (0x16E0) | +0x159C CityIndex (= 0x006AA754) of the open city screen | Civ2Proc.pas:154, Civ2Types.pas:744-747 |
| MapWindow / MapWindows | 0x0066C7A8 | TMapWindow[8] (× 0x3F0) | +0x2E0 MapCenter (2×i16), +0x2E4 MapZoom (i16), +0x2E8 MapRect | Civ2Proc.pas:183-184, Civ2Types.pas:709-742 |
| MainWindowInfo | 0x006553D8 | TWindowInfo1 | +0x08 → TWindowStructure*, whose +0x04 = main HWND. +0x8C PopupActive (u32) = 0x00655464 | Civ2Proc.pas:179, Civ2Types.pas:185-194 |
| SideBar | 0x006ABC68 | TSideBarWindow | Status sidebar | Civ2Proc.pas:202 |
| AdvisorWindow | 0x0063EB10 | TAdvisorWindow | Open advisor | Civ2Proc.pas:150 |
| CityGlobals | 0x006A6528 | TCityGlobals (0x140) | Scratch values for the city being calculated | Civ2Proc.pas:155 |
| PowerGraph | 0x00655C38 | byte[150][8] | Power-rating history | Civ2Proc.pas:194 |
| Label string table | [0x00628420] | pointer → Integer[] | `GetLabelString(i) = GetStringInList(([0x628420])[i])` | Civ2UIA_Proc.pas:179 |
| prevPFStopX (PF cache) | 0x006787CC | Integer | Pathfinder cache | Civ2UIA_PathLine.pas:139 |
| "Don't flush messages" flag | 0x0062BCB0 | Integer | Internal | UiaPatchCommon.pas:280 |

Consistency check: these tables are contiguous. Improvements (0x64C488 + 67×8) ends exactly at Civs (0x64C6A0), and Civs (+8×0x594) ends exactly at Cities (0x64F340). That supports the record sizes.

### a.2 TGame @ 0x00655AE8 (size 0x14A). Source: `src/Civ2Types.pas:977-1069`

The `word_655xxx` placeholder names in the source match the computed offsets exactly, which confirms the layout.

| Off | Abs addr | Size | Type | Field | Meaning |
|---|---|---|---|---|---|
| 0x00 | 0x655AE8 | 2 | u16 | CustomFeatures | 0x0010 simplified combat, 0x0080 bloodlust, 0x0100 no restart, 0x8000 flat world |
| 0x02 | 0x655AEA | 4 | u32 | GraphicAndGameOptions | Bit flags (lines 984-1004); 0x8000 = cheat mode |
| 0x08 | 0x655AF0 | 2 | u16 | MapFlags | |
| 0x0C | 0x655AF4 | 2 | u16 | TutorialsShown | |
| **0x10** | **0x655AF8** | 2 | i16 | **Turn** | Game turn number |
| 0x12 | 0x655AFA | 2 | u16 | "Year" (per UIA) | **Actually the turn counter used for year calculation** (SAV: `turnNumberForGameYear`). The real year comes from `ConvertTurnToYear` (0x00403418); see (b) and (c) |
| **0x16** | **0x655AFE** | 2 | i16 | **ActiveUnitIndex** | Current unit index (-1 = none). Valid only if `UnitSelected` is non-zero |
| 0x1A | 0x655B02 | 1 | u8 | MultiType | 0 single, 1 hot-seat, 3 network, 4 internet |
| 0x1B | 0x655B03 | 1 | u8 | HumanCivIndex (byte copy) | SAV "playersCivIndex" |
| 0x1C | 0x655B04 | 1 | u8 | (byte_655B04) | SAV "whichCivsMapShown" |
| **0x1D** | **0x655B05** | 1 | i8 | **SomeCivIndex** | Civ currently taking its turn. UIA filters units with it in `OnActivateUnit` (UiaPatchUnits.pas:73) |
| 0x1F | 0x655B07 | 1 | bool | RevealMap | |
| 0x20 | 0x655B08 | 1 | u8 | DifficultyLevel | 0 Chieftain .. 5 Deity |
| 0x21 | 0x655B09 | 1 | u8 | BarbarianActivity | |
| **0x22** | **0x655B0A** | 1 | u8 | **ActivePlayers** | Bitmask of civs in play |
| **0x23** | **0x655B0B** | 1 | u8 | **HumanPlayers** | **Bitmask** of human civs; used as `HumanPlayers and (1 shl CivIndex)` in UiaPatchUnits.pas:55 |
| 0x24 | 0x655B0C | 1 | u8 | ActivePlayersOnStart | |
| 0x25 | 0x655B0D | 1 | u8 | Enemies | |
| 0x2C | 0x655B14 | 2 | u16 | PeaceTurns | |
| **0x2E** | **0x655B16** | 2 | i16 | **TotalUnits** | Used length of the units array |
| **0x30** | **0x655B18** | 2 | u16 | **TotalCities** | Used length of the cities array |
| 0x36 | 0x655B1E | 100 | u8[100] | TechsDiscoveredFirst | |
| 0x9A | 0x655B82 | 100 | u8[100] | TechsDiscovered | Bit *n* = civ *n* has the tech |
| 0xFE | 0x655BE6 | 56 | i16[28] | WonderCities | City index; -1 = not built, -2 = lost |
| 0x13A | 0x655C22 | 8 | u8[8] | CivsRating | |

### a.3 TUnit, array @ 0x006560F0, element 0x20, max 0x800 (2048). Source: `src/Civ2Types.pas:1233-1278`

Placeholder names `byte_656101` (+0x11) and `word_65610E` (+0x1E) match the base address.

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0x00 | 2 | u16 (signed per SAV) | X | Doubled-X map coordinate (SAV: <0 = dead) |
| 0x02 | 2 | u16 | Y | |
| 0x04 | 2 | u16 | Attributes | **0x2000 = Veteran**; 0x4000 = Wait ('W'); 0x8000 = automate; 0x0040 = has moved once; 0x0010 paradropped; 0x0004 in foreign territory |
| 0x06 | 1 | u8 | UnitType | Index into UnitTypes |
| 0x07 | 1 | i8 | CivIndex | **Owner** (0-7) |
| 0x08 | 1 | i8 | "MovePoints" | **Move points SPENT this turn**, in 1/RoadMovementMultiplier units. SAV calls it `movePointsLost`; TOTPP calls it `moveSpent`. Moves left = max(0, UnitTypes[t].Move×RoadMult − this); see (c) |
| 0x09 | 1 | u8 | Visibility | Bitmask of civs that currently see the unit |
| 0x0A | 1 | u8 | HPLost | **Damage** (hit points lost) |
| 0x0B | 1 | u8 | MoveDirection | Last move direction (0xFF none) |
| 0x0C | 1 | char | DebugSymbol | |
| 0x0D | 1 | i8 | Counter | Settler work / caravan commodity / air turns |
| 0x0E | 1 | u8 | MoveIteration | |
| 0x0F | 1 | i8 | **Orders** | 0x01 fortify, 0x02 fortified, 0x03 sleep/sentry, 0x04 fortress, 0x05 road/rail, 0x06 irrigate, 0x07 mine, 0x08 transform, 0x09 pollution, 0x0A airbase, 0x0B/0x1B goto, **0xFF (-1) = no orders**. UIA tests `(Orders and $F) = $B` for goto (UiaPatchUnits.pas:56) |
| 0x10 | 1 | u8 | HomeCity | City index; 0xFF = none |
| 0x11 | 1 | u8 | (unknown) | |
| 0x12 | 2 | u16 | GotoX | |
| 0x14 | 2 | u16 | GotoY | |
| 0x16 | 2 | u16 | PrevInStack | Unit index (0xFFFF = none) |
| 0x18 | 2 | u16 | NextInStack | |
| 0x1A | 4 | i32 | **ID** | **0 = empty/dead slot** |
| 0x1E | 2 | u16 | (unknown) | |

### a.4 TCity, array @ 0x0064F340, element 0x58, max 0x100 (256). Source: `src/Civ2Types.pas:1280-1329`

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0x00 | 2 | i16 | X | |
| 0x02 | 2 | i16 | Y | |
| 0x04 | 4 | u32 | Attributes | 0x1 disorder, 0x2 WLTKD, 0x4 sold, 0x8 tech stolen, 0x80 coastal, 0x10000 airlifted, 0x400000 investigated, 0x04000000 x1 objective, 0x10000000 x3 objective |
| 0x08 | 1 | u8 | **Owner** | Civ index |
| 0x09 | 1 | i8 | **Size** | |
| 0x0A | 1 | u8 | Founder | |
| 0x0B | 1 | u8 | TurnsCaptured | |
| 0x0C | 1 | u8 | KnownTo | Civ bitmask |
| 0x0D | 8 | i8[8] | RevealedSize | Size as last seen by each civ |
| 0x15 | 1 | u8 | (unknown) | |
| 0x16 | 4 | u32 | Specialists | 2 bits each: 1 entertainer, 2 taxman, 3 scientist |
| 0x1A | 2 | i16 | FoodStorage | |
| 0x1C | 2 | i16 | **BuildProgress** | Shields accumulated |
| 0x1E | 2 | i16 | BaseTrade | |
| 0x20 | 16 | char[16] | **Name** | NUL-terminated (ANSI) |
| 0x30 | 4 | i32 | Workers | Bits 0-20 = worked spiral tiles (bit 20 = centre); bits 26-31 = non-working citizen count |
| 0x34 | 5 | u8[5] | Improvements | Bitset; bit *n* = improvement *n* (bit 0 = "Nothing") |
| 0x39 | 1 | i8 | **Building** | **Production**: >=0 = UnitType index; <0 = improvement index `-Building` (QuickInfo.pas:370-378) |
| 0x3A | 1 | i8 | TradeRoutes | Count |
| 0x3B | 3 | i8[3] | SuppliedTradeItem | |
| 0x3E | 3 | i8[3] | DemandedTradeItem | |
| 0x41 | 3 | i8[3] | CommodityTraded | |
| 0x44 | 6 | i16[3] | TradePartner | City indices |
| 0x4A | 2 | i16 | Science | |
| 0x4C | 2 | i16 | Tax | |
| 0x4E | 2 | i16 | Trade | |
| 0x50 | 1 | u8 | TotalFood | |
| 0x51 | 1 | u8 | TotalShield | |
| 0x52 | 1 | u8 | HappyCitizens | |
| 0x53 | 1 | u8 | UnHappyCitizens | |
| 0x54 | 4 | i32 | **ID** | **0 = empty slot** |

### a.5 TCiv, array @ 0x0064C6A0, element 0x594, count 8 (0 = barbarians). Source: `src/Civ2Types.pas:1338-1418`

Source comments confirm the absolute addresses: Gold = 0x64C6A2, Leader = 0x64C6A6, Beakers = 0x64C6A8.

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0x000 | 2 | u16 | Flags | 0x0200 female leader; 0x0001 skip Oedo; 0x0008 can change government |
| 0x002 | 4 | i32 | **Gold** | Treasury |
| 0x006 | 2 | u16 | Leader | Index into Leaders (@LEADERS tribe id) |
| 0x008 | 2 | u16 | Beakers | Research progress |
| 0x00A | 2 | i16 | ResearchingTech | -1 / 0xFF = none |
| 0x00C | 2 | i16 | CapitalX | |
| 0x00E | 2 | i16 | TurnOfCityBuild | |
| 0x010 | 1 | u8 | Techs | Number known |
| 0x011 | 1 | u8 | FutureTechs | |
| 0x013 | 1 | u8 | ScienceRate | ×10 % |
| 0x014 | 1 | u8 | TaxRate | ×10 % |
| 0x015 | 1 | u8 | **Government** | 0 Anarchy, 1 Despotism, 2 Monarchy, 3 Communism, 4 Fundamentalism, 5 Republic, 6 Democracy |
| 0x016 | 1 | i8 | SenateChances | |
| 0x01E | 1 | u8 | Reputation | 0 spotless .. 7 atrocious |
| 0x01F | 1 | u8 | (Patience per SAV) | |
| 0x020 | 32 | u32[8] | Treaties[otherCiv] | 0x1 contact, 0x2 ceasefire, 0x4 peace, 0x8 alliance, 0x10 vendetta, 0x80 embassy, 0x2000 war |
| 0x040 | 8 | u8[8] | Attitude[otherCiv] | 0 worshipful .. 100 enraged |
| 0x154 | 62 | u8[62] | DefMinUnitBuilding | Units in production per type |
| 0x3E2 | 16 | i16[8] | (last contact per SAV) | |
| 0x400 | 1 | u8 | SpaceFlags | 0x01 building, 0x02 launched, 0x08 fusion |

Offsets 0x048-0x153 are opaque in UIA; (b) fills some of them in from the SAV layout.

### a.6 Map. Source: `src/Civ2Types.pas:1085-1153`; usage `src/Civ2UIA_PathLine.pas:146-152`

**TMapHeader @ 0x006D1160 (14 bytes):**

| Off | Abs addr | Type | Field | Meaning |
|---|---|---|---|---|
| 0x0 | 0x6D1160 | i16 | SizeX | Width in doubled-X units (SAV: "Map X dimension x2") |
| 0x2 | 0x6D1162 | i16 | SizeY | Also read directly as `PWord($006D1162)` in UiaPatchMapWindow.pas:42 |
| 0x4 | 0x6D1164 | i16 | Area | Number of tiles = SizeX/2 × SizeY. UIA copies `Area*6` bytes from `*MapData` (PathLine.pas:146) |
| 0x6 | 0x6D1166 | i16 | Flat | |
| 0x8 | 0x6D1168 | i16 | Seed | Resource seed |
| 0xA | 0x6D116A | i16 | ArrayW | Locator/minimap width |
| 0xC | 0x6D116C | i16 | ArrayH | Locator/minimap height |

**Tile array:** `tiles = *(u32*)0x00636598`, `Area` × TMapSquare (6 bytes):

| Off | Type | Field | Meaning |
|---|---|---|---|
| 0 | u8 | TerrainType | Low 4 bits: 0 desert, 1 plains, 2 grassland, 3 forest, 4 hills, 5 mountains, 6 tundra, 7 glacier, 8 swamp, 9 jungle, 10 ocean. 0x40 = no resource, 0x80 = river |
| 1 | u8 | TerrainFeatures | 0x01 unit, 0x02 city, 0x04 irrigation, 0x08 mine (0x0C farmland), 0x10 road, 0x30 railroad, 0x40 fortress (0x42 airbase), 0x80 pollution |
| 2 | u8 | CityRadii | Top 3 bits = civ owning a city radius here |
| 3 | u8 | MassIndex | Landmass/body-of-water id |
| **4** | u8 | **Visibility** | **Per-civ explored mask: bit n = explored by civ n** |
| 5 | u8 | OwnershipAndFertility | Low nibble = fertility; high nibble = owner civ (0xF = none) |

**Per-civ known improvements:** `known = (*(u32[8]*)0x006365C0)[civ]`, then `known[tileIndex]` uses the same bit meaning as TerrainFeatures. This is what that civ remembers seeing, not live state. UIA uses the same tile index for `MapData` and `MapCivData` (PathLine.pas:151).

Helper functions (cdecl) that show the indexing: `MapGetSquare(X,Y)` @ 0x00401BB3, `MapGetCivData(X,Y,Civ)` @ 0x00403823, `MapSquareIsVisibleTo(X,Y,Civ)` @ 0x00403C24, `MapWrapX` @ 0x004022ED, `IsInMapBounds` @ 0x004012D0. Source: `Civ2ProcImplF.inc:91-103`. These are 0x004xxxxx jump thunks; disassemble them to confirm the formula in (b).

### a.7 TUnitType rules @ 0x0064B1B8, element 0x14, count 0x3E (62). Source: `src/Civ2Types.pas:1188-1231`

The source comment "Role ... // 0x64B1CA" confirms +0x12 against the base.

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0x00 | 4 | u32 | StringIndex | **Name**, via `GetStringInList(StringIndex)` (fn 0x00403387). Not a direct char*; see (c) |
| 0x04 | 4 | u32 | Abilities | Flag bits (lines 1191-1205) |
| 0x08 | 1 | u8 | Until | Obsoleting tech |
| 0x09 | 1 | u8 | Domain | 0 ground, 1 air, 2 sea |
| 0x0A | 1 | u8 | **Move** | Move rate (scale: see (c)) |
| 0x0B | 1 | u8 | Range | |
| 0x0C | 1 | u8 | Att | |
| 0x0D | 1 | u8 | Def | |
| 0x0E | 1 | u8 | **HitPoints** | (scale: see (c); RULES.TXT "1h" = 10 HP) |
| 0x0F | 1 | u8 | FirePower | |
| 0x10 | 1 | u8 | Cost | |
| 0x11 | 1 | u8 | Hold | |
| 0x12 | 1 | u8 | Role | 0 attack, 1 defend, 2 naval, 3 air, 4 transport, 5 settle, 6 diplomacy, 7 trade |
| 0x13 | 1 | u8 | Preq | |

Other rules records:
- TImprovement (8 bytes): StringIndex u32, Cost, Upkeep, Preq, ?. Source: Civ2Types.pas:1178.
- TRulesCivilize (16 bytes): Name[4] (short code), TextIndex i32, ..., Preq[2]. Source: Civ2Types.pas:1164.
- TLeader (0x30): Attack, Expand, Civilize, ?, Gender, CitiesBuilt, Color u16, Style u16, NameIndex u16, NationPlural u16, NationAdjective u16, ... Source: Civ2Types.pas:1424.

### a.8 Function addresses (for disassembly or reference, not needed for RPM)

All are in `src/Civ2ProcImplF.inc`. `PThisCall` marks a thiscall method (ECX = this).

- **Popups and dialogs:**
  - `Dlg_CreateAndWait` 0x00401118 (line 37)
  - `Dlg_CreateDialog` 0x004026D5 (38)
  - `Dlg_LoadPopup` 0x0040314D (44)
  - `Dlg_LoadSimple` 0x00403B8E (45)
  - `Dlg_LoadGAMESimpleL0` 0x004037CE (42)
  - `Dlg_LoadGAMESimpleL0F0` 0x00403B25 (43)
  - `Dlg_InitWithHeap` 0x004039DB (41)
  - `Dlg_Setup` 0x004018A2 (46)
  - `Dlg_AddListboxItem` 0x00402D0B (35)
  - `PopupSimpleGAMEWithSprite` 0x004034A9 (126)
  - `PopupWithImprovementSprite` 0x00403AD0 (127)
  - `ClearPopupActive` 0x005A3C58 (23)
  - `ListItemProcLButtonUp` 0x005A3CCA (87)
  - Real bodies patched by UIA: popup loader epilogue 0x005A6C12 (returns to 0x005A6C1C); CreateAndWait body 0x005A5F40 (continues at 0x005A5F46, skip-dialog exit 0x005A6323). Source: `src/Patches/UiaPatchSuppressPopup.pas:85-86, 61, 70, 76`.
- **Game logic:**
  - `ConvertTurnToYear` 0x00403418 (25)
  - `txtStrcatYear` 0x00403C65 (182)
  - `GetStringInList` 0x00403387 (74)
  - `GetTopUnitInStack` 0x00403391 (76)
  - `GetNextUnitInStack` 0x00402E23 (70)
  - `GetCityIndexAtXY` 0x00403AF8 (67)
  - `UnitCanMove` 0x0040273E (168)
  - `CivHasTech` 0x00402E7D (21)
  - `HumanTurn` 0x00402BA8 (84)
  - `ProcessUnit` 0x00402716 (129)
  - `MoveUnit` 0x00402829 (115)
  - `AfterActiveUnitChanged` 0x004016EF (3)
- **Import table (IAT) slots used by UIA hooks** (`src/Civ2UIA_Hooks.pas:392-401`, `src/Patches/UiaPatch64Bit.pas:25-38`):
  - SetFocus 0x006E7D94
  - CreateWindowExA 0x006E7D50
  - DestroyWindow 0x006E7E1C
  - ShowWindow 0x006E7E24
  - SetWindowPos 0x006E7DB8
  - SetWindowLongA 0x006E7DB0
  - GetWindowLongA 0x006E7E2C
  - GetClassLongA 0x006E7E9C
  - mciSendCommand 0x006E7FAC
  - LoadLibraryA 0x006E7C30
- Entry point: 0x005F6E90 (`Civ2UIAL_Proc.pas:303`).

---

## (b) INFERRED from the SAV layout (Civ2-clone `Engine/src/OriginalSaves/Read.ClassicSav.cs`)

The MGE SAV mirrors memory one-to-one for the records below (same sizes: unit 32 = 0x20 (line 691), city 88 = 0x58 (line 822), civ 1428 = 0x594 (line 259)). Every field the clone reads lines up with the UIA offsets, so the two sources corroborate each other.

**b.1 Game record = SAV bytes 12 .. 12+0x14A.** The SAV option block starts at file offset 12 (line 62), which matches memory `Game+0x00`. Parameters start at `paramsOffset = 24` (line 111), which matches `Game+0x0C`:

| SAV offset (line) | Memory | Meaning | Agrees with UIA? |
|---|---|---|---|
| params+4 (118) | Game+0x10 | turnNumber | Yes (Turn) |
| params+6 (119) | Game+0x12 | `turnNumberForGameYear` | UIA calls it "Year", but it is a turn counter for the year formula |
| params+10 (120) | Game+0x16 | selectedUnitIndex (-1 = none) | Yes (ActiveUnitIndex) |
| params+15 (121) | Game+0x1B | playersCivIndex (human) | Yes |
| params+16 (122) | Game+0x1C | whichCivsMapShown | New name |
| params+17 (123) | Game+0x1D | "players civilization number used" | UIA: SomeCivIndex. Meaning ambiguous; see (c) |
| params+22 (130) | Game+0x22 | civsInPlay bitmask | Yes |
| params+23 (135) | Game+0x23 | humanPlayers bitmask | Yes |
| params+34/36 (140-141) | Game+0x2E/0x30 | numberOfUnits / numberOfCities | Yes |

**b.2 Civ record (SAV offsetT = 2278, stride 1428, lines 258-259).** These fill gaps in UIA's `Unknown9` (0x48-0x153):

| Civ off | Size | Meaning | Line |
|---|---|---|---|
| 0x001 | 1 | Gender (0 male, 2 female); high byte of Flags | 271 |
| 0x058 | 13 | **Advances known**: bitset, 100 techs | 323 |
| 0x066 | 2 | Number of military units | 327 |
| 0x068 | 2 | Number of cities | 328 |
| 0x06C | 2 | Sum of city sizes | 329 |
| 0x0D8 | 62 | Active units per unit type | 340 |
| 0x116 | 62 | Casualties per unit type | 341 |
| 0x154 | 62 | Units in production per type (= UIA DefMinUnitBuilding) | 342 |
| 0x3E2 | 16 | Last contact turn per civ (u16[8]) | ~352 |
| 0x3F3 | 9 | Throne room | ~356 |
| 0x400 / 0x402.. | | Spaceship flag and component counts | ~360-368 |

**b.3 Civ names are not part of TCiv.** In the SAV they sit between Game and Civs: 8 × 242 bytes starting at file offset 342 (line 211 uses `offsetN = 266+318` for civs 1..7).
- Layout per civ: +0 city style (byte); +2 leader name char[24]; +26 tribe name char[24]; +50 adjective char[24]; +74.. seven government titles char[24].
- An empty string means "use RULES.TXT @LEADERS", reached via `Civ.Leader` → Leaders table.
- **The memory address is unknown.** It is not contiguous with Civs in memory, because Improvements occupies the bytes just before 0x64C6A0. See (c).

**b.4 Map tile index formula** (lines 496-498, 584, 543):
- `XDim = SizeX/2`. The clone iterates `i = y*XDim + x` with half-coordinate x, and maps civ2 coordinate `X = 2*x + (y%2)`.
- So for game coordinates (X, Y): **`index = Y * (SizeX/2) + (X / 2)`**, and the tile is at `tiles + 6*index`.
- The per-civ known-improvement block uses the same index (SAV block 1, line 543).
- SAV block 1 stores only civs 1-7. Memory `MapCivData` has 8 pointers (civ 0 included).

**b.5 Unit record cross-check** (lines 710-760). Every offset agrees with a.3:
- x +0, y +2, madeFirstMove = byte4 bit6 (Attributes 0x0040), **veteran = byte5 bit5 (Attributes 0x2000)**, wait = byte5 bit6, type +6, civ +7.
- **movePointsLost +8**, visibility +9, **hitPointsLost +10**, prev-move direction +11 (0..7, 0xFF none), counter +13, **order +15**, home city +16, gotoX +18, gotoY +20.
- Stack links: +22 = "linkOtherUnitsOnTop" (UIA PrevInStack) and +24 = "linkOtherUnitsUnder" (UIA NextInStack).

**b.6 City record cross-check** (lines 836-955). Every offset agrees with a.4:
- name +32 (line 887), item in production +57 as signed byte (line 927).
- Improvement bits +52..56, skipping bit 0. Workers +48..50, with byte 51 = non-working×4.

**b.7 Year.** The year is not stored. The game calls `ConvertTurnToYear` (0x00403418), and UIA's PowerGraph uses `ConvertTurnToYear(Turn+1)` (`UiaPatchPowerGraph.pas:151`).
- Civ2-clone has a re-implementation in `Engine/src/Date.cs:25-48`. It is piecewise by difficulty, starts at 4000 BC, and handles scenario `TurnYearIncrement` (negative = monthly) and `StartingYear` (ScenarioParameters +0x54/+0x56).
- Treat it as approximate. Feed it `Game+0x12` (`turnNumberForGameYear`), not `Game.Turn`, and check it against the sidebar.

---

## (c) UNKNOWN / needs runtime verification

1. **Unit move scale.** Is `UnitTypes[t].Move` stored as whole moves (e.g. 1) or already multiplied by `Cosmic.RoadMovementMultiplier` (e.g. 3)? Test with Warriors at the start of a turn.
   - Then: `movesLeft = Move*RoadMult - Unit.MovePoints` or `Move - Unit.MovePoints`.
   - Damaged land units also get reduced movement, so the true remaining moves may be lower.
2. **HP scale.** Is `UnitTypes[t].HitPoints` 1 (= 10 HP) or 10? Then `currentHP = HitPoints*10 - HPLost` or `HitPoints - HPLost`. Verify on a damaged unit.
3. **Names behind StringIndex.** Unit type, improvement and commodity names are `GetStringInList(StringIndex)`. The text-pool base pointer is not in the UIA source; find it by disassembling 0x00403387 (a thunk; follow the JMP).
   - Practical alternative: parse `RULES.TXT` from the game or scenario folder. Unit type order equals @UNITS line order (62 entries).
4. **Civ name strings** (leader, tribe, adjective). Address unknown. Find them with a memory scan for a custom leader name, then check whether they form an 8 × 242-byte block.
5. **Whose turn it is.** `Game.SomeCivIndex` (0x655B05) is the best candidate; verify it changes during AI turns.
   - `HumanCivIndex` (0x6D1DA0, dword) is the local human's perspective. It is -1 between hot-seat players.
   - `Game+0x1B` is the byte copy. Confirm whether it equals 0x6D1DA0 in single-player.
6. **`Game.Year` (+0x12) semantics.** Confirm it is a turn-like counter (SAV) rather than a calendar year (UIA name).
7. **Units array relocation** when Civ2UIA runs with `bUnitsLimit` (caveat 0.1). Verify by reading dword 0x004108F8 in memory against the file.
   - The launcher maps one section with VA = file offset + 0x00400C00 (`Civ2UIAL_Proc.pas:442`). Verify the mapping of `.text` separately (PE section table) before comparing bytes.
8. **TotalUnits / TotalCities semantics.** UIA iterates `0..Total-1` with an ID check, so treat them as high-water marks, not live counts.
9. **TMapHeader.ArrayW/ArrayH** are locator (minimap) dims per Civ2-clone, not the tile stride. Use `SizeX/2` as the stride. Confirm that `Area == SizeX/2*SizeY`.
10. **MainWindowInfo.PopupActive (0x00655464).** The field name is from UIA but its semantics are not demonstrated in UIA code. Compare it with `[0x006CEC84] != 0` while popups are shown.
11. Unknown fields: TUnit +0x11 and +0x1E; TCity +0x15; TGame +0x14, +0x18, +0x1C, +0x1E, +0x26..+0x2A, +0x32, +0x34; most of TCiv 0x17-0x1D and 0x192-0x3E1.

---

## (d) How Civ2UIA detects and handles dialogs (popups)

**They are native Win32 windows, but custom-drawn.**
- Each Civ2 window (main, map, city, advisors, popups) is a real HWND with Civ2's own window classes. The class names seen in UIA are `MSScrollBarClass` (`UiaMain.pas:220-225`) and `MSWindowClass` (comment `UiaPatchTests.pas:612`).
- The window's content is rendered by the game into an offscreen DIB (`TDrawPort`/`TDrawInfo`: HDC, HBITMAP, pixel pointer; Civ2Types.pas:556-594) and blitted.
- Text and list items are **not** standard Win32 controls, so `GetWindowText` / accessibility will not expose popup text.
- Buttons, list items and scrollbars are child HWNDs with per-control `TControlInfo` (`HWindow` at +0x1C, control type at +0x00: 6 button, 7 listbox, 1 list item, 8 scroll; Civ2Types.pas:255-338).

Window extra bytes (`GetWindowLongA`, Civ2Types.pas:182-184, 571; UiaMain.pas:146-158):
- `GetWindowLongA(hwnd, 0x0C)` → TGraphicsInfo*
- `GetWindowLongA(hwnd, 4)` → TWindowInfo*. Known values: 0x006A9200 = City window, 0x006A66B0 = Civilopedia, 0x0066C7F0 = Map window. Each is its struct base + 0x48.
- `GetWindowLongA(hwnd, 8)` → WindowProc1 info
- `GetWindowLongA(hwnd, 0)` → MS window / control block

**Detection in UIA:** it reads `CurrPopupInfo = *(TDialogWindow**)0x006CEC84`.
- nil means no modal popup (`Civ2UIA_MapMessages.pas:109`, `UiaPatchResizableWindows.pas:225`, `UiaPatchUnitsListPopup.pas:77`).
- To match a popup to an HWND, it compares `Dialog.GraphicsInfo == GetWindowLongA(hwnd, 0x0C)`. It then classifies the popup: `NumListItems > 0 && NumLines == 0` is the units-stack chooser; `Columns > 1` is a multi-column dialog (`UiaMain.pas:155-161`).
- Advisors are identified by the call-site return address of CreateWindowEx (`UiaMain.pas:178-203`).

**UIA suppression of popups** (`src/Patches/UiaPatchSuppressPopup.pas`):
- It hooks the end of `Dlg_LoadPopup` (0x005A6C12). For GAME.TXT sections in a user list, it concatenates `Title` and the text-line list into an on-map message.
- It then sets `PressedButton := $12345678`. Its hook at the start of CreateAndWait (0x005A5F40) checks `[ecx+0xDC] == $12345678` and skips showing the window.

**TDialogWindow (0x2F4) offsets useful for a read-only reader.** Computed from Civ2Types.pas:374-516 and cross-checked against the old layout comments (lines 518-543) and the asm `[ecx+$DC]`:

| Off | Type | Field | Notes |
|---|---|---|---|
| 0x000 | ptr | GraphicsInfo | → TGraphicsInfo. HWND = `[[GraphicsInfo+0x48+0x08]+0x04]` (TWindowInfo1.WindowStructure → TWindowStructure.HWindow) |
| 0x020 | i32 | NumTextLines | |
| 0x028 | i32 | NumLines | |
| 0x02C | i32 | NumListItems | |
| 0x030 | i32 | NumButtonsStd | |
| 0x034 | i32 | NumButtons | |
| 0x038 | i32 | Columns | |
| 0x03C | u32 | Flags | 0x1 cancel button, 0x20 created, 0x40 help, 0x1000 listbox, 0x2000 choose, 0x10000 system popup, 0x2000000 no OK button |
| 0x040 | 2×i32 | ClientSize | |
| 0x0D8 | u32 | SelectedItem | 0xFFFFFFFF = none |
| 0x0DC | i32 | PressedButton | |
| 0x0E0 | 2×i32 | Position (screen) | |
| 0x134 | char* | **Title** | |
| 0x21C | ptr | _Extra | Used by UIA itself, so it can be non-zero only under UIA |
| 0x224 | ptr | SelectedListItem | → TListItem |
| 0x230 | ptr | **FirstTextLine** | Linked list of TDlgTextLine {Index i32, Flags i32, **Text char* @+8**, Size, ?, ?, ?, **Next @+0x1C**} |
| 0x234 | ptr | **FirstListItem** | Linked list of TListItem {Index i32 @0, ?, **Text char* @+8**, Sprite @+0xC, **Next @+0x10**} |
| 0x274 | ptr | ButtonControls | → TControlInfoButton[] (0x3C each; HWND at +0x1C) |
| 0x294 | char*[6] | ButtonTexts | |
| 0x2AC | {i32 StdType, i32 Left}[6] | Buttons | |

The dialog's GAME.TXT section name is not stored in the struct; UIA only gets it as a parameter of the loader. `LoadedTxtSectionName` @ 0x006CECB0 (char[], `Civ2Proc.pas:176`) is the last loaded TXT section and is a good candidate to identify which popup is open. **Verify at runtime.**

---

## (e) Suggested read recipe (pseudo-code)

```
game   = RPM(0x655AE8, 0x14A)
turn   = i16(game+0x10);  activeUnit = i16(game+0x16)
humanMask = u8(game+0x23); activeMask = u8(game+0x22); turnCiv = i8(game+0x1D)
human  = i32(RPM(0x6D1DA0,4));  unitSel = u32(RPM(0x6D1DA8,4))
nU = i16(game+0x2E); nC = u16(game+0x30)
units  = RPM(UNITS_BASE /*0x6560F0 unless UIA units-limit*/, nU*0x20)   // skip ID(+0x1A)==0
cities = RPM(0x64F340, nC*0x58)                                         // skip ID(+0x54)==0
civs   = RPM(0x64C6A0, 8*0x594)
utypes = RPM(0x64B1B8, 62*0x14); roadMult = u8(RPM(0x64BCC8,1))
hdr    = RPM(0x6D1160, 14); W2 = i16(hdr+0)/2; H = i16(hdr+2); area = i16(hdr+4)
tiles  = RPM(u32(RPM(0x636598,4)), area*6)       // tile(X,Y) = tiles[6*(Y*W2 + X/2)]
known[c] = RPM(u32(RPM(0x6365C0+4*c,4)), area)   // per-civ remembered improvements
explored(X,Y,c) = tiles[6*idx+4] & (1<<c)
popup  = u32(RPM(0x6CEC84,4)); if popup: title = cstr(u32(popup+0x134)); walk lines @+0x230 (.Text@+8, .Next@+0x1C), items @+0x234 (.Text@+8, .Next@+0x10)
```

---

## Web references consulted
- [Apolyton: Civilization II SAV/SCN file format (section sizes: unit 32 B, city 88 B in MGE)](https://apolyton.net/articles/civilization-news/civ2/modification-aa/9312966-civilization-ii-sav-scn-file-format)
- [Apolyton: Civ 2 SAV file structure thread](https://apolyton.net/forum/civilization-series/civilization-i-and-civilization-ii/162137-civ-2-sav-file-structure?p=5709114)
- [CivFanatics: TOTPP Lua function reference (field naming: `moveSpent`, `damage`, `veteran`, `order`, `visibility`, `humanPlayers`, `activeTribes`)](https://forums.civfanatics.com/threads/totpp-lua-function-reference.557527). This is for ToT, so the naming applies but the addresses do not.
- [CivFanatics: Civ2 MGE UI Additions thread](https://forums.civfanatics.com/threads/697565)
