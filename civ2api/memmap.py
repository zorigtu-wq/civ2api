"""
Addresses and record layouts for CIV2.EXE MGE 5.4.0f (Patch 3, 2,489,344 bytes).

Every value here is traced to Civ2-UI-Additions (FoxAhead) source; see
memory-map.md for the file/line and verification status of each one.
"""

EXPECTED_EXE_SIZE = 2_489_344

# ---- globals (Civ2Proc.pas:148-210)
GAME = 0x00655AE8            # TGame, 0x14A bytes
UNITS = 0x006560F0           # TUnit[0x800], 0x20 each (may be relocated by UIA units-limit patch)
CITIES = 0x0064F340          # TCity[0x100], 0x58 each
CIVS = 0x0064C6A0            # TCiv[8], 0x594 each, index 0 = barbarians
UNIT_TYPES = 0x0064B1B8      # TUnitType[0x3E], 0x14 each
COSMIC = 0x0064BCC8          # TCosmic; byte 0 = road movement multiplier
LEADERS = 0x006554F8         # TLeader[1..21], 0x30 each
MAP_HEADER = 0x006D1160      # TMapHeader
MAP_DATA_PTR = 0x00636598    # -> TMapSquare[Area], 6 bytes each
MAP_CIV_DATA = 0x006365C0    # PByteArray[8]: per-civ remembered TerrainFeatures
HUMAN_CIV_INDEX = 0x006D1DA0  # i32, -1 between hot-seat players
UNIT_SELECTED = 0x006D1DA8   # LongBool: Game.ActiveUnitIndex is meaningful when != 0
CURSOR_X = 0x0064B1B4        # i16
CURSOR_Y = 0x0064B1B0        # i16
CURR_POPUP = 0x006CEC84      # -> TDialogWindow, 0 = no popup
LOADED_TXT_SECTION = 0x006CECB0  # char[], last GAME.TXT section loaded (unverified)

# Instruction operand that holds (Units + k). UIA's units-limit patch rewrites it,
# so (value in memory) - (value in exe file) = relocation delta.
UNITS_REF_OPERAND = 0x004108F8

# ---- sizes / counts
UNIT_SIZE, UNITS_MAX = 0x20, 0x800
CITY_SIZE, CITIES_MAX = 0x58, 0x100
CIV_SIZE, CIVS_MAX = 0x594, 8
UNIT_TYPE_SIZE, UNIT_TYPES_MAX = 0x14, 0x3E
LEADER_SIZE, LEADERS_MAX = 0x30, 21
TILE_SIZE = 6
GAME_SIZE = 0x14A
DIALOG_SIZE = 0x2F4

# ---- TGame offsets (Civ2Types.pas:977-1060)
G_TURN = 0x10            # i16
G_YEAR_COUNTER = 0x12    # u16 (turn counter used for year calc, not the calendar year)
G_ACTIVE_UNIT = 0x16     # i16
G_MULTI_TYPE = 0x1A      # u8  0 single, 1 hot seat, 3 network, 4 internet
G_HUMAN_CIV = 0x1B       # u8
G_SOME_CIV = 0x1D        # i8  probably "civ currently moving" (unverified)
G_REVEAL_MAP = 0x1F      # u8
G_DIFFICULTY = 0x20      # u8
G_ACTIVE_PLAYERS = 0x22  # u8 bitmask
G_HUMAN_PLAYERS = 0x23   # u8 bitmask
G_TOTAL_UNITS = 0x2E     # i16
G_TOTAL_CITIES = 0x30    # u16

# ---- TUnit offsets (Civ2Types.pas:1233-1276)
U_X, U_Y = 0x00, 0x02         # u16 / u16 (int16 < 0 = dead)
U_ATTR = 0x04                 # u16; 0x2000 veteran, 0x4000 wait, 0x8000 automate
U_TYPE = 0x06                 # u8
U_OWNER = 0x07                # i8
U_MOVES_SPENT = 0x08          # i8, in (move * road multiplier) units
U_VISIBILITY = 0x09           # u8 civ bitmask
U_HP_LOST = 0x0A              # u8
U_COUNTER = 0x0D              # i8 settlers work / caravan commodity / air turns
U_ORDERS = 0x0F               # i8, -1 (0xFF) none
U_HOME_CITY = 0x10            # u8, 0xFF none
U_GOTO_X, U_GOTO_Y = 0x12, 0x14
U_PREV_STACK, U_NEXT_STACK = 0x16, 0x18
U_ID = 0x1A                   # i32, 0 = empty slot

ATTR_VETERAN = 0x2000
ATTR_WAIT = 0x4000
ATTR_AUTOMATE = 0x8000

ORDERS = {
    -1: None, 1: "fortify", 2: "fortified", 3: "sleep", 4: "build_fortress",
    5: "build_road", 6: "build_irrigation", 7: "build_mine", 8: "transform",
    9: "clean_pollution", 10: "build_airbase", 11: "goto", 27: "goto",
}

# ---- TCity offsets (Civ2Types.pas:1280-1336)
C_X, C_Y = 0x00, 0x02         # i16
C_ATTR = 0x04                 # u32; 0x1 disorder, 0x2 WLTKD, 0x80 coastal
C_OWNER = 0x08                # u8
C_SIZE = 0x09                 # i8
C_FOOD = 0x1A                 # i16
C_SHIELDS = 0x1C              # i16
C_NAME = 0x20                 # char[16]
C_SPECIALISTS = 0x16          # u32, 2 bits per specialist: 1 entertainer, 2 taxman, 3 scientist
C_WORKED = 0x30               # u32: bit i = city tile i worked (bit 20 = centre); bits 26+ =
                              # number of specialists. Verified live turn 25 by clicking tiles in
                              # Washington's Resource Map and reading the bit that changed.
# City tile i -> (dx, dy) in doubled-x map coordinates. 0-7 = the unit directions
# NE,E,SE,S,SW,W,NW,N; 8-11 = the far diagonals; 12-19 = the knight squares clockwise from NNE.
CITY_TILES = [(1, -1), (2, 0), (1, 1), (0, 2), (-1, 1), (-2, 0), (-1, -1), (0, -2),
              (2, -2), (2, 2), (-2, 2), (-2, -2), (1, -3), (3, -1), (3, 1), (1, 3),
              (-1, 3), (-3, 1), (-3, -1), (-1, -3), (0, 0)]
C_IMPROVEMENTS = 0x34          # u8[5] bitset, bit n = improvement n (1 Palace, 2 Barracks, 4 Temple ...)
C_BUILDING = 0x39             # i8: >=0 unit type, <0 -> improvement -value
C_SCIENCE, C_TAX, C_TRADE = 0x4A, 0x4C, 0x4E
C_TOTAL_FOOD, C_TOTAL_SHIELD = 0x50, 0x51
C_HAPPY, C_UNHAPPY = 0x52, 0x53
C_ID = 0x54                   # i32, 0 = empty slot

# ---- TCiv offsets (Civ2Types.pas:1338+)
CV_FLAGS = 0x00
CV_GOLD = 0x02                # i32
CV_LEADER = 0x06              # u16 index into LEADERS / RULES.TXT @LEADERS
CV_BEAKERS = 0x08
CV_RESEARCHING = 0x0A         # i16
CV_TECHS = 0x10               # u8 count
CV_SCIENCE_RATE = 0x13
CV_TAX_RATE = 0x14
CV_GOVERNMENT = 0x15
GOVERNMENTS = ["Anarchy", "Despotism", "Monarchy", "Communism",
               "Fundamentalism", "Republic", "Democracy"]

# ---- TUnitType offsets (Civ2Types.pas:1188-1230)
UT_STRING_INDEX = 0x00
UT_ABILITIES = 0x04
UT_DOMAIN = 0x09              # 0 ground, 1 air, 2 sea
UT_MOVE = 0x0A
UT_RANGE = 0x0B
UT_ATT, UT_DEF = 0x0C, 0x0D
UT_HP = 0x0E
UT_FP = 0x0F
UT_COST = 0x10
UT_HOLD = 0x11
UT_ROLE = 0x12
DOMAINS = ["ground", "air", "sea"]

# ---- TMapHeader (Civ2Types.pas:1085)
MH_SIZE_X, MH_SIZE_Y, MH_AREA = 0x0, 0x2, 0x4   # SizeX is the doubled width
MH_SEED = 0x8                                   # resource/hut seed (TMapHeader.Seed)

# ---- TMapSquare (Civ2Types.pas:1097+)
T_TERRAIN, T_FEATURES, T_CITY_RADII, T_MASS, T_VISIBILITY, T_OWNER = range(6)
TERRAINS = ["Desert", "Plains", "Grassland", "Forest", "Hills", "Mountains",
            "Tundra", "Glacier", "Swamp", "Jungle", "Ocean"]

# ---- TDialogWindow (Civ2Types.pas:374-516), offsets computed + cross-checked with
#      the Unknown_XX field names in the same record
D_GRAPHICS_INFO = 0x000
D_NUM_TEXT_LINES = 0x020
D_NUM_LINES = 0x028
D_NUM_LIST_ITEMS = 0x02C
D_NUM_BUTTONS_STD = 0x030
D_NUM_BUTTONS = 0x034
D_COLUMNS = 0x038
D_FLAGS = 0x03C
D_SELECTED_ITEM = 0x0D8
D_PRESSED_BUTTON = 0x0DC
D_TITLE = 0x134
D_FIRST_LISTBOX_ITEM = 0x228   # TListboxItem layout unknown
D_FIRST_TEXT_LINE = 0x230      # TDlgTextLine: Text@+8, Next@+0x1C
D_FIRST_LIST_ITEM = 0x234      # TListItem: Index@0, Text@+8, Next@+0x10
# List-box popups (PRODUCTION, FINDCITY, research...) keep their rows in a second list of
# the same TListItem layout (Value@+4 = item code, Prev@+0x14). Verified live turn 25 on
# "What shall we build in Washington?": +0x228 -> Settlers (first), +0x220 -> Warriors
# (the current choice), +0x22C -> Hanging Gardens (last).
D_LISTBOX_FIRST, D_LISTBOX_SELECTED, D_LISTBOX_LAST = 0x228, 0x220, 0x22C
LIST_ITEM_VALUE = 0x04
D_BUTTON_CONTROLS = 0x274      # TControlInfoButton[] 0x3C each, HWND@+0x1C
D_BUTTON_TEXTS = 0x294         # char*[6]
D_BUTTONS = 0x2AC              # {StdType i32, Left i32}[6]; StdType 0 OK, 1 Help, 2 Cancel

DLG_FLAGS = {
    0x1: "cancel_button", 0x4: "checkboxes", 0x20: "created", 0x40: "help_button",
    0x1000: "listbox", 0x2000: "choose", 0x10000: "system_popup",
    0x40000: "system_listbox", 0x2000000: "no_ok_button",
}
TEXT_LINE_TEXT, TEXT_LINE_NEXT = 0x08, 0x1C
LIST_ITEM_INDEX, LIST_ITEM_TEXT, LIST_ITEM_NEXT = 0x00, 0x08, 0x10
BUTTON_CTRL_SIZE, BUTTON_CTRL_HWND = 0x3C, 0x1C
