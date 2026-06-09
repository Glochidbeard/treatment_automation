"""
opt_plugin.py — Standalone nursery route-optimization plugin.

Public API
----------
    from opt_plugin import optimize

    ordered, total_time, errors = optimize(locations, vehicle="foot")

Parameters
----------
locations : list[str]
    Stop location strings.  locations[0] is the start depot,
    locations[-1] is the end depot.  All strings in between are
    picked stops.  Accepts canonical form ("L>1>5") or spaced
    form ("L > 1 > 05").

vehicle : str, optional
    "foot"      – walk everywhere (default)
    "cart"      – drive everywhere on cart-legal edges
    "cart_walk" – drive between stops, walk last stretch to each stop

Returns
-------
ordered : list[str]
    The middle stops reordered for the shortest route.
    Within each alley zone (p/m/e), beds are sorted by number in
    the direction of traversal (ascending p→e, descending e→p).
total_time : float | None
    Estimated travel time in minutes; None if route is disconnected.
errors : list[str]
    Warnings for any stop that could not be resolved (those stops
    are omitted from `ordered`).
"""

import re
import networkx as nx

# ════════════════════════════════════════════════════════════════════════════════
# SECTION 1 — NORMALISATION
# ════════════════════════════════════════════════════════════════════════════════

def _normalize(s):
    """Collapse spaces around '>' and strip leading zeros from numeric segments.
    'L > 1 > 10' and 'F > 15 > 01' both normalize to canonical net-map keys."""
    s = re.sub(r'\s*>\s*', '>', s.strip())
    parts = s.split('>')
    normalized = []
    for part in parts:
        stripped = part.lstrip('0')
        if stripped != part and (stripped.isdigit() or stripped == ''):
            normalized.append(stripped or '0')
        else:
            normalized.append(part)
    return '>'.join(normalized)


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 2 — ROW → ALLEY REGISTRATION
# ════════════════════════════════════════════════════════════════════════════════

_ROW_TO_ALLEYS = {}

def _register(section, row, alley):
    key = f"{section}>{row}"
    _ROW_TO_ALLEYS.setdefault(key, []).append(alley)

# B — disjoint
for _r1, _r2 in [(3,4),(5,6),(7,8),(9,10),(11,12)]:
    _register("B", str(_r1), f"B{_r1}{_r2}")
    _register("B", str(_r2), f"B{_r1}{_r2}")

# D — disjoint D2–D13
for _r1, _r2 in [(2,3),(4,5),(6,7),(8,9),(10,11),(12,13)]:
    _register("D", str(_r1), f"D{_r1}{_r2}")
    _register("D", str(_r2), f"D{_r1}{_r2}")

# D — joint D14–D18
_register("D", "14", "D1415")
for _r in [15, 16, 17]:
    _register("D", str(_r), f"D{_r-1}{_r}")
    _register("D", str(_r), f"D{_r}{_r+1}")
_register("D", "18", "D1718")

# E — joint E7–E11
_register("E", "7", "E78")
for _r in [8, 9, 10]:
    _register("E", str(_r), f"E{_r-1}{_r}")
    _register("E", str(_r), f"E{_r}{_r+1}")
_register("E", "11", "E1011")

# F1–6 — disjoint
for _r1, _r2 in [(1,2),(3,4),(5,6)]:
    _register("F", str(_r1), f"F{_r1}{_r2}")
    _register("F", str(_r2), f"F{_r1}{_r2}")

# F7–10 — disjoint
for _r1, _r2 in [(7,8),(9,10)]:
    _register("F", str(_r1), f"F{_r1}{_r2}")
    _register("F", str(_r2), f"F{_r1}{_r2}")

# F11–15 — joint
_register("F", "11", "F1112")
for _r in [12, 13, 14]:
    _register("F", str(_r), f"F{_r-1}{_r}")
    _register("F", str(_r), f"F{_r}{_r+1}")
_register("F", "15", "F1415")

# G — joint G1–G12
_register("G", "1", "G12")
for _r in range(2, 12):
    _register("G", str(_r), f"G{_r-1}{_r}")
    _register("G", str(_r), f"G{_r}{_r+1}")
_register("G", "12", "G1112")

# L — joint L1–L16
_register("L", "1", "L12")
for _r in range(2, 16):
    _register("L", str(_r), f"L{_r-1}{_r}")
    _register("L", str(_r), f"L{_r}{_r+1}")
_register("L", "16", "L1516")

# C — rows 1–3, each its own alley
for _r in range(1, 4):
    _register("C", str(_r), f"C{_r}")

# A — area nodes
for _r in [1, 2]:
    _register("A", str(_r), "A1")
for _r in [3, 4, 5]:
    _register("A", str(_r), "A3-5_p")

# M — all rows → single area node
for _r in range(1, 16):
    _register("M", str(_r), "M1-15")

# X — all rows → single area node
for _r in range(1, 12):
    _register("X", str(_r), "X1-11")

# Y — proper alley-style nodes (Y1_p/m/e, Y2_p/m/e)
_register("Y", "1", "Y1")
_register("Y", "2", "Y2")

# V — west side
_register("V", "1", "V12w");  _register("V", "2", "V12w")
_register("V", "2", "V23w");  _register("V", "3", "V23w")   # V34w skipped — no _e node

# V — east side
_register("V", "1", "V12e");  _register("V", "2", "V12e")
_register("V", "2", "V23e");  _register("V", "3", "V23e")
_register("V", "3", "V34e");  _register("V", "4", "V34e")   # V45e skipped — no _m node

# V5 — triangle corners (flat graph nodes, returned as-is by _alley_node)
_register("V", "5", "V45e_p")
_register("V", "5", "V45e_e")
_register("V", "5", "V5Ee_e")


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 3 — GRAPH EDGES
# (node_a, node_b, weight_minutes, direction, cart_ok)
# ════════════════════════════════════════════════════════════════════════════════

_RAW_EDGES = [

    # ── FACILITY ──────────────────────────────────────────────────────────────
    ("Offices",       "Will Call",      0.5,   'NS',    True ),
    ("Offices",       "Ryan's office",  0.3,   'SN',    True ),
    ("Offices",       "Seed barn",      0.5,   'WE',    False),
    ("Offices",       "Y1_p",           2.0,   'NS',    True ),
    ("Ryan's office", "Y2_p",           2.5,   'NS',    True ),

    # ── C BLOCK ───────────────────────────────────────────────────────────────
    ("Offices", "C1_p",    0.5,  'NS',    True ),
    ("Offices", "A3-5_p",  0.33, 'SN',    True ),
    ("C1_p",    "D1_exit", 0.25, 'EW',    True ),
    ("C1_p",    "C1_e",    0.5,  'WE',    True ),
    ("C1_p",    "D23_p",   0.75, 'NW_SE', True ),
    ("C2_p",    "C2_e",    1.5,  'WE',    True ),
    ("C2_e",    "C3_p",    0.1,  'WE',    True ),
    ("C3_p",    "C3_e",    1.16, 'WE',    True ),
    ("C3_p",    "D1415_p", 0.16, 'NS',    True ),
    ("C3_p",    "D1516_p", 0.16, 'NS',    True ),

    # ── A BLOCK ───────────────────────────────────────────────────────────────
    ("Offices", "A1", 400, 'SN', True),

    # ── D1 ────────────────────────────────────────────────────────────────────
    ("D1_p",   "D1_exit",        0,    'WE', True),
    ("D1_p",   "D1_e",           0.4,  'NS', True),
    ("D1_e",   "Orange canning", 0.25, 'NS', True),

    # ── D23 ───────────────────────────────────────────────────────────────────
    ("D23_p", "D23_e",         0.5,  'NS',    False),
    ("D23_p", "D1_exit",       1.0,  'EW',    True ),
    ("D23_p", "D45_p",         0.25, 'WE',    True ),
    ("D23_p", "C2_p",          0.16, 'WE',    True ),
    ("D23_p", "C2_e",          1.66, 'WE',    True ),
    ("D23_p", "Seed barn",     0.5,  'SN',    False),
    ("D23_p", "B34_p",         0.33, 'SN',    True ),
    ("D23_p", "Green canning", 0.1,  'EW',    True ),
    ("D1_exit","Green canning", 0.75, 'WE',   True ),

    # ── D BLOCK — disjoint (D45–D1213) ───────────────────────────────────────
    ("D45_p",   "D45_m",   0.33, 'NS', True), ("D45_e",   "D45_m",   0.33, 'SN', True),
    ("D67_p",   "D67_m",   0.33, 'NS', True), ("D67_e",   "D67_m",   0.33, 'SN', True),
    ("D89_p",   "D89_m",   0.33, 'NS', True), ("D89_e",   "D89_m",   0.33, 'SN', True),
    ("D1011_p", "D1011_m", 0.33, 'NS', True), ("D1011_e", "D1011_m", 0.33, 'SN', True),
    ("D1213_p", "D1213_m", 0.33, 'NS', True), ("D1213_e", "D1213_m", 0.33, 'SN', True),

    ("D45_e",   "D67_e",   0.25, 'WE', True), ("D67_e",   "D89_e",   0.25, 'WE', True),
    ("D89_e",   "D1011_e", 0.25, 'WE', True), ("D1011_e", "D1213_e", 0.25, 'WE', True),
    ("D45_p",   "D67_p",   0.25, 'WE', True), ("D67_p",   "D89_p",   0.25, 'WE', True),
    ("D89_p",   "D1011_p", 0.25, 'WE', True), ("D1011_p", "D1213_p", 0.25, 'WE', True),

    # ── D BLOCK — joint (D1415–D1718) ────────────────────────────────────────
    ("D1415_p", "D1415_m", 0.33, 'NS', True), ("D1415_e", "D1415_m", 0.33, 'SN', True),
    ("D1516_p", "D1516_m", 0.33, 'NS', True), ("D1516_e", "D1516_m", 0.33, 'SN', True),
    ("D1617_p", "D1617_m", 0.33, 'NS', True), ("D1617_e", "D1617_m", 0.33, 'SN', True),
    ("D1718_p", "D1718_m", 0.33, 'NS', True), ("D1718_e", "D1718_m", 0.33, 'SN', True),

    ("D1415_e", "D1516_e", 0.25, 'WE', True), ("D1516_e", "D1617_e", 0.25, 'WE', True),
    ("D1617_e", "D1718_e", 0.25, 'WE', True),
    ("D1415_p", "D1516_p", 0.25, 'WE', True), ("D1516_p", "D1617_p", 0.25, 'WE', True),
    ("D1617_p", "D1718_p", 0.25, 'WE', True),
    ("D1415_m", "D1516_m", 0.25, 'WE', False), ("D1516_m", "D1617_m", 0.25, 'WE', False),
    ("D1617_m", "D1718_m", 0.25, 'WE', False),

    ("D1213_e", "D1415_e", 0.25, 'WE', True), ("D1213_p", "D1415_p", 0.25, 'WE', True),

    # ── D → E ─────────────────────────────────────────────────────────────────
    ("D1415_e", "E78_p",   0.1,  'NS',    True), ("D1415_e", "E89_p",   0.51, 'NW_SE', True),
    ("D1516_e", "E89_p",   0.1,  'NS',    True), ("D1516_e", "E910_p",  0.51, 'NW_SE', True),
    ("D1617_e", "E910_p",  0.1,  'NS',    True), ("D1617_e", "E1011_p", 0.51, 'NW_SE', True),
    ("D1718_e", "E1011_p", 0.1,  'NS',    True),

    # ── E BLOCK — joint (E78–E1011) ───────────────────────────────────────────
    ("E78_p",   "E78_m",   0.35, 'NS', True), ("E78_e",   "E78_m",   0.35, 'SN', True),
    ("E89_p",   "E89_m",   0.35, 'NS', True), ("E89_e",   "E89_m",   0.35, 'SN', True),
    ("E910_p",  "E910_m",  0.35, 'NS', True), ("E910_e",  "E910_m",  0.35, 'SN', True),
    ("E1011_p", "E1011_m", 0.35, 'NS', True), ("E1011_e", "E1011_m", 0.35, 'SN', True),

    ("E78_e",  "E89_e",   0.25, 'WE', True), ("E89_e",  "E910_e",  0.25, 'WE', True),
    ("E910_e", "E1011_e", 0.25, 'WE', True),
    ("E78_p",  "E89_p",   0.25, 'WE', True), ("E89_p",  "E910_p",  0.25, 'WE', True),
    ("E910_p", "E1011_p", 0.25, 'WE', True),
    ("E78_m",  "E89_m",   0.25, 'WE', False), ("E89_m",  "E910_m",  0.25, 'WE', False),
    ("E910_m", "E1011_m", 0.25, 'WE', False),

    # ── E → F ─────────────────────────────────────────────────────────────────
    ("E78_e",  "F910_p",  0.66,  'EW',    True),
    ("F910_p", "D89_e",   1.125, 'SE_NW', True),

    # ── CANNING ───────────────────────────────────────────────────────────────
    ("Orange canning", "D23_e", 0.66, 'SN', True),
    ("Orange canning", "D1_e",  0.25, 'SN', True),

    # ── B BLOCK — disjoint (B34–B1112) ───────────────────────────────────────
    ("B34_p",   "B34_m",   0.33, 'SN', False), ("B34_e",   "B34_m",   0.33, 'NS', False),
    ("B56_p",   "B56_m",   0.33, 'SN', False), ("B56_e",   "B56_m",   0.33, 'NS', False),
    ("B78_p",   "B78_m",   0.33, 'SN', False), ("B78_e",   "B78_m",   0.33, 'NS', False),
    ("B910_p",  "B910_m",  0.33, 'SN', False), ("B910_e",  "B910_m",  0.33, 'NS', False),
    ("B1112_p", "B1112_m", 0.33, 'SN', False), ("B1112_e", "B1112_m", 0.33, 'NS', False),

    ("B34_p",  "B56_p",   0.25, 'WE', True), ("B56_p",  "B78_p",   0.25, 'WE', True),
    ("B78_p",  "B910_p",  0.25, 'WE', True), ("B910_p", "B1112_p", 0.25, 'WE', True),
    ("B34_m",  "B56_m",   0.25, 'WE', False), ("B56_m",  "B78_m",   0.25, 'WE', False),
    ("B78_m",  "B910_m",  0.25, 'WE', False), ("B910_m", "B1112_m", 0.25, 'WE', False),

    # ── F BLOCK — F12, F34, F56 (disjoint) ───────────────────────────────────
    ("F12_p", "F12_m", 0.5, 'NS', True), ("F12_m", "F12_e", 0.5, 'NS', True),
    ("F34_p", "F34_m", 0.5, 'NS', True), ("F34_m", "F34_e", 0.5, 'NS', True),
    ("F56_p", "F56_m", 0.5, 'NS', True), ("F56_m", "F56_e", 0.5, 'NS', True),
    ("F12_e", "F34_e", 0,    'WE', True), ("F34_e", "F56_e", 0.55, 'WE', True),
    ("F12_p", "F34_p", 0,    'WE', True), ("F34_p", "F56_p", 0.55, 'WE', True),
    ("F12_m", "F34_m", 0.25, 'WE', False), ("F34_m", "F56_m", 0.25, 'WE', False),

    # ── F BLOCK — F78, F910 (disjoint) ───────────────────────────────────────
    ("F78_p",  "F78_m",  0.5,  'NS', True), ("F78_m",  "F78_e",  0.5,  'NS', True),
    ("F910_p", "F910_m", 0.5,  'NS', True), ("F910_m", "F910_e", 0.5,  'NS', True),
    ("F78_e",  "F910_e", 0.55, 'WE', True),
    ("F78_p",  "F910_p", 0.55, 'WE', True),
    ("F78_m",  "F910_m", 0.25, 'WE', False),

    # ── F BLOCK — F1112–F1415 (joint) ────────────────────────────────────────
    ("F1112_p", "F1112_m", 0.5, 'NS', True), ("F1112_m", "F1112_e", 0.5, 'NS', True),
    ("F1213_p", "F1213_m", 0.5, 'NS', True), ("F1213_m", "F1213_e", 0.5, 'NS', True),
    ("F1314_p", "F1314_m", 0.5, 'NS', True), ("F1314_m", "F1314_e", 0.5, 'NS', True),
    ("F1415_p", "F1415_m", 0.5, 'NS', True), ("F1415_m", "F1415_e", 0.5, 'NS', True),
    ("F1112_e", "F1213_e", 0.5, 'WE', True), ("F1213_e", "F1314_e", 0.5, 'WE', True),
    ("F1314_e", "F1415_e", 0.5, 'WE', True),
    ("F1112_p", "F1213_p", 0.5, 'WE', True), ("F1213_p", "F1314_p", 0.5, 'WE', True),
    ("F1314_p", "F1415_p", 0.5, 'WE', True),
    ("F1112_m", "F1213_m", 0.3, 'WE', False), ("F1213_m", "F1314_m", 0.3, 'WE', False),
    ("F1314_m", "F1415_m", 0.3, 'WE', False),

    # ── G BLOCK — joint (G12–G1112) ───────────────────────────────────────────
    ("G12_p",   "G12_m",   0.5, 'NS', True), ("G12_m",   "G12_e",   0.5, 'NS', True),
    ("G23_p",   "G23_m",   0.5, 'NS', True), ("G23_m",   "G23_e",   0.5, 'NS', True),
    ("G34_p",   "G34_m",   0.5, 'NS', True), ("G34_m",   "G34_e",   0.5, 'NS', True),
    ("G45_p",   "G45_m",   0.5, 'NS', True), ("G45_m",   "G45_e",   0.5, 'NS', True),
    ("G56_p",   "G56_m",   0.5, 'NS', True), ("G56_m",   "G56_e",   0.5, 'NS', True),
    ("G67_p",   "G67_m",   0.5, 'NS', True), ("G67_m",   "G67_e",   0.5, 'NS', True),
    ("G78_p",   "G78_m",   0.5, 'NS', True), ("G78_m",   "G78_e",   0.5, 'NS', True),
    ("G89_p",   "G89_m",   0.5, 'NS', True), ("G89_m",   "G89_e",   0.5, 'NS', True),
    ("G910_p",  "G910_m",  0.5, 'NS', True), ("G910_m",  "G910_e",  0.5, 'NS', True),
    ("G1011_p", "G1011_m", 0.5, 'NS', True), ("G1011_m", "G1011_e", 0.5, 'NS', True),
    ("G1112_p", "G1112_m", 0.5, 'NS', True), ("G1112_m", "G1112_e", 0.5, 'NS', True),

    ("G12_p",   "G23_p",   0.25, 'WE', True), ("G23_p",   "G34_p",   0.25, 'WE', True),
    ("G34_p",   "G45_p",   0.25, 'WE', True), ("G45_p",   "G56_p",   0.25, 'WE', True),
    ("G56_p",   "G67_p",   0.25, 'WE', True), ("G67_p",   "G78_p",   0.25, 'WE', True),
    ("G78_p",   "G89_p",   0.25, 'WE', True), ("G89_p",   "G910_p",  0.25, 'WE', True),
    ("G910_p",  "G1011_p", 0.25, 'WE', True), ("G1011_p", "G1112_p", 0.25, 'WE', True),
    ("G12_e",   "G23_e",   0.25, 'WE', True), ("G23_e",   "G34_e",   0.25, 'WE', True),
    ("G34_e",   "G45_e",   0.25, 'WE', True), ("G45_e",   "G56_e",   0.25, 'WE', True),
    ("G56_e",   "G67_e",   0.25, 'WE', True), ("G67_e",   "G78_e",   0.25, 'WE', True),
    ("G78_e",   "G89_e",   0.25, 'WE', True), ("G89_e",   "G910_e",  0.25, 'WE', True),
    ("G910_e",  "G1011_e", 0.25, 'WE', True), ("G1011_e", "G1112_e", 0.25, 'WE', True),
    ("G12_m",   "G23_m",   0.25, 'WE', False), ("G23_m",   "G34_m",   0.25, 'WE', False),
    ("G34_m",   "G45_m",   0.25, 'WE', False), ("G45_m",   "G56_m",   0.25, 'WE', False),
    ("G56_m",   "G67_m",   0.25, 'WE', False), ("G67_m",   "G78_m",   0.25, 'WE', False),
    ("G78_m",   "G89_m",   0.25, 'WE', False), ("G89_m",   "G910_m",  0.25, 'WE', False),
    ("G910_m",  "G1011_m", 0.25, 'WE', False), ("G1011_m", "G1112_m", 0.25, 'WE', False),

    # ── L BLOCK — joint (L12–L1516) ───────────────────────────────────────────
    ("L12_p",   "L12_m",   0.45, 'NS', True), ("L12_m",   "L12_e",   0.45, 'NS', True),
    ("L23_p",   "L23_m",   0.45, 'NS', True), ("L23_m",   "L23_e",   0.45, 'NS', True),
    ("L34_p",   "L34_m",   0.45, 'NS', True), ("L34_m",   "L34_e",   0.45, 'NS', True),
    ("L45_p",   "L45_m",   0.45, 'NS', True), ("L45_m",   "L45_e",   0.45, 'NS', True),
    ("L56_p",   "L56_m",   0.45, 'NS', True), ("L56_m",   "L56_e",   0.45, 'NS', True),
    ("L67_p",   "L67_m",   0.45, 'NS', True), ("L67_m",   "L67_e",   0.45, 'NS', True),
    ("L78_p",   "L78_m",   0.45, 'NS', True), ("L78_m",   "L78_e",   0.45, 'NS', True),
    ("L89_p",   "L89_m",   0.45, 'NS', True), ("L89_m",   "L89_e",   0.45, 'NS', True),
    ("L910_p",  "L910_m",  0.45, 'NS', True), ("L910_m",  "L910_e",  0.45, 'NS', True),
    ("L1011_p", "L1011_m", 0.45, 'NS', True), ("L1011_m", "L1011_e", 0.45, 'NS', True),
    ("L1112_p", "L1112_m", 0.45, 'NS', True), ("L1112_m", "L1112_e", 0.45, 'NS', True),
    ("L1213_p", "L1213_m", 0.45, 'NS', True), ("L1213_m", "L1213_e", 0.45, 'NS', True),
    ("L1314_p", "L1314_m", 0.45, 'NS', True), ("L1314_m", "L1314_e", 0.45, 'NS', True),
    ("L1415_p", "L1415_m", 0.45, 'NS', True), ("L1415_m", "L1415_e", 0.45, 'NS', True),
    ("L1516_p", "L1516_m", 0.45, 'NS', True), ("L1516_m", "L1516_e", 0.45, 'NS', True),

    ("L12_p",   "L23_p",   0.3, 'EW', True), ("L23_p",   "L34_p",   0.3, 'EW', True),
    ("L34_p",   "L45_p",   0.3, 'EW', True), ("L45_p",   "L56_p",   0.3, 'EW', True),
    ("L56_p",   "L67_p",   0.3, 'EW', True), ("L67_p",   "L78_p",   0.3, 'EW', True),
    ("L78_p",   "L89_p",   0.3, 'EW', True), ("L89_p",   "L910_p",  0.3, 'EW', True),
    ("L910_p",  "L1011_p", 0.3, 'EW', True), ("L1011_p", "L1112_p", 0.3, 'EW', True),
    ("L1112_p", "L1213_p", 0.3, 'EW', True), ("L1213_p", "L1314_p", 0.3, 'EW', True),
    ("L1314_p", "L1415_p", 0.3, 'EW', True), ("L1415_p", "L1516_p", 0.3, 'EW', True),
    ("L12_e",   "L23_e",   0.3, 'EW', True), ("L23_e",   "L34_e",   0.3, 'EW', True),
    ("L34_e",   "L45_e",   0.3, 'EW', True), ("L45_e",   "L56_e",   0.3, 'EW', True),
    ("L56_e",   "L67_e",   0.3, 'EW', True), ("L67_e",   "L78_e",   0.3, 'EW', True),
    ("L78_e",   "L89_e",   0.3, 'EW', True), ("L89_e",   "L910_e",  0.3, 'EW', True),
    ("L910_e",  "L1011_e", 0.3, 'EW', True), ("L1011_e", "L1112_e", 0.3, 'EW', True),
    ("L1112_e", "L1213_e", 0.3, 'EW', True), ("L1213_e", "L1314_e", 0.3, 'EW', True),
    ("L1314_e", "L1415_e", 0.3, 'EW', True), ("L1415_e", "L1516_e", 0.3, 'EW', True),
    ("L12_m",   "L23_m",   0.3, 'EW', False), ("L23_m",   "L34_m",   0.3, 'EW', False),
    ("L34_m",   "L45_m",   0.3, 'EW', False), ("L45_m",   "L56_m",   0.3, 'EW', False),
    ("L56_m",   "L67_m",   0.3, 'EW', False), ("L67_m",   "L78_m",   0.3, 'EW', False),
    ("L78_m",   "L89_m",   0.3, 'EW', False), ("L89_m",   "L910_m",  0.3, 'EW', False),
    ("L910_m",  "L1011_m", 0.3, 'EW', False), ("L1011_m", "L1112_m", 0.3, 'EW', False),
    ("L1112_m", "L1213_m", 0.3, 'EW', False), ("L1213_m", "L1314_m", 0.3, 'EW', False),
    ("L1314_m", "L1415_m", 0.3, 'EW', False), ("L1415_m", "L1516_m", 0.3, 'EW', False),

    # ── V BLOCK ───────────────────────────────────────────────────────────────
    # West — proximal spine (foot only N-S)
    ("V12w_p", "V23w_p",  0.3,  'NS',    False),
    ("V23w_p", "V34w_p",  0.3,  'NS',    False),
    # West — within-alley and cross connections
    ("V34w_p", "V34w_m",  0.16, 'WE',    True ),
    ("V34w_m", "V23w_m",  0.3,  'SN',    False),
    ("V34w_m", "V23w_e",  0.45, 'WE',    True ),
    ("V23w_m", "V23w_p",  0.3,  'EW',    True ),
    ("V23w_m", "V23w_e",  0.3,  'WE',    True ),
    ("V23w_m", "V12w_m",  0.3,  'SN',    False),
    ("V12w_m", "V12w_p",  0.3,  'EW',    True ),
    ("V12w_m", "V12w_e",  0.25, 'WE',    True ),
    ("V12w_e", "V23w_e",  0.3,  'NS',    True ),
    # West → East bridges
    ("V12w_e", "V12e_p",  0.25, 'WE',    True ),
    ("V23w_e", "V23e_p",  0.25, 'WE',    True ),
    # East — diagonal proximal spine
    ("V12e_p", "V23e_p",  0.3,  'NS',    True ),
    ("V23e_p", "V34e_p",  0.45, 'NW_SE', True ),
    ("V34e_p", "V45e_p",  0.66, 'NW_SE', True ),
    ("V45e_p", "V5Ee_e",  0.6,  'NW_SE', True ),
    # East — within-alley (p→m→e)
    ("V12e_p", "V12e_m",  0.65, 'WE',    True ),
    ("V12e_m", "V12e_e",  0.65, 'WE',    True ),
    ("V23e_p", "V23e_m",  0.65, 'WE',    True ),
    ("V23e_m", "V23e_e",  0.65, 'WE',    True ),
    ("V34e_m", "V34e_e",  0.65, 'WE',    True ),
    ("V45e_p", "V45e_e",  0.66, 'WE',    True ),
    # East — end spine (N-S)
    ("V12e_e", "V23e_e",  0.3,  'NS',    True ),
    ("V23e_e", "V34e_e",  0.3,  'NS',    True ),
    ("V34e_e", "V45e_e",  0.3,  'NS',    True ),
    ("V45e_e", "V5Ee_e",  0.3,  'NS',    True ),
    # East — middle-to-middle
    ("V23e_m", "V12e_m",  0.3,  'SN',    True ),
    ("V23e_m", "V34e_m",  0.3,  'NS',    True ),
    # Entry / exit
    ("V34w_p", "Offices", 3.0,  'NE_SW', True ),
    ("V45e_e", "C3_e",    1.0,  'NS',    False),

    # ── Y BLOCK ───────────────────────────────────────────────────────────────
    ("Y1_p", "Y1_m", 0.75, 'EW', True ),
    ("Y1_m", "Y1_e", 0.75, 'EW', True ),
    ("Y2_p", "Y2_m", 0.5,  'EW', True ),
    ("Y2_m", "Y2_e", 0.5,  'EW', True ),
    ("Y2_p", "Y1_p", 0.75, 'WE', False),  # foot only

    # ── WEST SIDE ─────────────────────────────────────────────────────────────
    ("Offices",  "L12_p",  600,  'WE', True),
    ("L12_p",    "M1-15",  400,  'WE', True),
    ("M1-15",    "N",      350,  'WE', True),
    ("N",        "O",      300,  'WE', True),
    ("O",        "F78_p",  400,  'WE', True),
    ("L12_p",    "P",      500,  'WE', True),
    ("L12_p",    "Q",      500,  'WE', True),
    ("M1-15",    "R",      400,  'WE', True),
    ("R",        "T",      350,  'WE', True),
    ("T",        "U",      350,  'WE', True),
    ("Offices",  "X1-11", 1200,  'WE', True),
    ("L1516_p",  "X1-11",    5,  'WE', True),

    # ── INTER-BLOCK BRIDGES ───────────────────────────────────────────────────
    ("F910_e",  "G45_p",   1/6,  'NS',    True),
    ("F910_e",  "ditch_1", 0.3,  'WE',    True),
    ("ditch_1", "ditch_2", 0.25, 'WE',    True),
    ("ditch_2", "ditch_3", 0.15, 'WE',    True),
    ("ditch_3", "F1112_e", 0.4,  'WE',    True),
    ("ditch_1", "G56_p",   1/6,  'NS',    True),
    ("ditch_2", "G67_p",   1/6,  'NS',    True),
    ("ditch_3", "G78_p",   1/6,  'NS',    True),

    ("F1112_e", "G89_p",   1/6,  'NS',    True),
    ("F1213_e", "G910_p",  1/6,  'NS',    True),
    ("F1314_e", "G1011_p", 1/6,  'NS',    True),
    ("F1415_e", "G1112_p", 1/6,  'NS',    True),

    ("F910_p",  "F1112_p", 5/6,  'WE',    True),
    ("F56_p",   "F78_p",   0.25, 'WE',    True),
    ("F56_e",   "F78_e",   0.25, 'WE',    True),

    ("F34_e",  "G12_p",   0.25, 'NS',    True),
    ("F78_e",  "G23_p",   1/6,  'NE_SW', True),
    ("F78_e",  "G34_p",   1/6,  'NW_SE', True),

    ("F12_p",          "Orange_canning_e", 0.5,  'SN',  True ),
    ("Orange canning", "Orange_canning_e", 0.5,  'NS',  False),

    ("F12_p",      "start_road", 0.25, 'EW', True),
    ("start_road", "end_road",   0.35, 'EW', True),
    ("end_road",   "L12_p",      0.5,  'EW', True),
]


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 4 — GRAPH CONSTRUCTION
# ════════════════════════════════════════════════════════════════════════════════

def _build_graphs():
    G_cart = nx.Graph()
    G_foot = nx.Graph()
    for (a, b, w, _dir, cart_ok) in _RAW_EDGES:
        G_foot.add_edge(a, b, weight=w, direction=_dir)
        if cart_ok:
            G_cart.add_edge(a, b, weight=w, direction=_dir)
    return G_cart, G_foot

_G_CART, _G_FOOT = _build_graphs()

_GRAPH_MAP = {
    "foot":      _G_FOOT,
    "cart":      _G_CART,
    "cart_walk": _G_CART,
}


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 5 — BED DATA  (embedded from beds.csv)
#
# Columns: loc_row, n_beds, bed_start, rack, road, wall, fence
#   bed_start : first physical bed number (1 for all normal rows; V>5 starts at 44)
#   rack      : "R" if there is a rack stop (maps to _m, or _p for D2/D3)
#   road      : "R" if there is a road stop  (maps to _p)
#   wall      : "W" if there is a wall stop  (maps to _e)
#   fence     : "F" if there is a fence stop (maps to _e)
# ════════════════════════════════════════════════════════════════════════════════

_BEDS_RAW = [
    # loc_row   n_beds  bed_start  rack  road  wall  fence
    # ── A ─────────────────────────────────────────────────────────────────────
    ("A>1",   34,  1, "",  "",  "",  ""),
    ("A>2",   40,  1, "",  "",  "",  ""),
    ("A>3",   17,  1, "",  "",  "",  ""),
    ("A>4",   17,  1, "",  "",  "",  ""),
    ("A>5",    9,  1, "",  "",  "",  ""),
    # ── B ─────────────────────────────────────────────────────────────────────
    ("B>1",   17,  1, "R", "",  "",  ""),
    ("B>2",   17,  1, "R", "",  "",  ""),
    ("B>3",   22,  1, "R", "",  "",  ""),
    ("B>4",   17,  1, "",  "",  "",  ""),
    ("B>5",   17,  1, "",  "",  "",  ""),
    ("B>6",   20,  1, "",  "R", "",  ""),
    ("B>7",   20,  1, "",  "R", "W", ""),
    ("B>8",   20,  1, "",  "R", "W", ""),
    ("B>9",   20,  1, "",  "R", "W", ""),
    ("B>10",  20,  1, "",  "R", "W", ""),
    ("B>11",  20,  1, "",  "R", "W", ""),
    ("B>12",  21,  1, "",  "R", "W", ""),
    # ── C ─────────────────────────────────────────────────────────────────────
    ("C>1",   14,  1, "",  "",  "",  ""),
    ("C>2",   28,  1, "",  "",  "",  ""),
    ("C>3",   40,  1, "",  "",  "",  ""),
    # ── D ─────────────────────────────────────────────────────────────────────
    ("D>1",   15,  1, "",  "",  "",  ""),
    ("D>2",   17,  1, "R", "",  "",  ""),   # rack at _p (D2 is proximal-side)
    ("D>3",   17,  1, "",  "",  "",  ""),
    ("D>4",   16,  1, "",  "",  "",  ""),
    ("D>5",   22,  1, "",  "",  "",  ""),
    ("D>6",   22,  1, "",  "",  "",  ""),
    ("D>7",   22,  1, "",  "",  "",  ""),
    ("D>8",   25,  1, "",  "",  "",  ""),
    ("D>9",   24,  1, "",  "",  "",  ""),
    ("D>10",  22,  1, "",  "",  "",  ""),
    ("D>11",  22,  1, "",  "",  "",  ""),
    ("D>12",  16,  1, "",  "",  "",  ""),
    ("D>13",  16,  1, "",  "",  "",  ""),
    ("D>14",  22,  1, "",  "",  "",  ""),
    ("D>15",  22,  1, "",  "",  "",  ""),
    ("D>16",  22,  1, "",  "",  "",  ""),
    ("D>17",  18,  1, "",  "",  "",  ""),
    ("D>18",  18,  1, "",  "",  "",  ""),
    # D>Rack — no numbered beds; rack stop handled via graph node directly
    # ── E ─────────────────────────────────────────────────────────────────────
    ("E>7",   26,  1, "",  "",  "",  ""),
    ("E>8",   26,  1, "",  "",  "",  ""),
    ("E>9",   26,  1, "",  "",  "",  ""),
    ("E>10",  26,  1, "",  "",  "",  ""),
    ("E>11",  26,  1, "",  "",  "",  ""),
    # ── F ─────────────────────────────────────────────────────────────────────
    ("F>1",   33,  1, "",  "",  "",  ""),
    ("F>2",   33,  1, "",  "",  "",  ""),
    ("F>3",   33,  1, "",  "",  "",  ""),
    ("F>4",   33,  1, "",  "",  "",  ""),
    ("F>5",   33,  1, "",  "",  "",  ""),
    ("F>6",   33,  1, "",  "",  "",  ""),
    ("F>7",   33,  1, "",  "",  "",  ""),
    ("F>8",   33,  1, "",  "",  "",  ""),
    ("F>9",   33,  1, "",  "",  "",  ""),
    ("F>10",  33,  1, "",  "",  "",  ""),
    ("F>11",  32,  1, "",  "",  "",  ""),
    ("F>12",  32,  1, "",  "",  "",  ""),
    ("F>13",  32,  1, "",  "",  "",  ""),
    ("F>14",  32,  1, "",  "",  "",  ""),
    ("F>15",  32,  1, "",  "",  "",  ""),
    # ── G ─────────────────────────────────────────────────────────────────────
    ("G>1",   32,  1, "",  "R", "",  ""),
    ("G>2",   32,  1, "",  "R", "",  ""),
    ("G>3",   32,  1, "",  "R", "",  ""),
    ("G>4",   32,  1, "",  "R", "",  ""),
    ("G>5",   32,  1, "",  "R", "",  ""),
    ("G>6",   32,  1, "",  "R", "",  ""),
    ("G>7",   32,  1, "",  "R", "",  ""),
    ("G>8",   32,  1, "",  "R", "",  ""),
    ("G>9",   32,  1, "",  "R", "",  ""),
    ("G>10",  32,  1, "",  "R", "",  ""),
    ("G>11",  32,  1, "",  "R", "",  ""),
    ("G>12",  32,  1, "",  "R", "",  ""),
    # ── L ─────────────────────────────────────────────────────────────────────
    ("L>1",   32,  1, "",  "",  "",  ""),
    ("L>2",   32,  1, "",  "R", "",  ""),
    ("L>3",   32,  1, "",  "",  "",  ""),
    ("L>4",   32,  1, "",  "R", "",  ""),
    ("L>5",   32,  1, "",  "",  "",  ""),
    ("L>6",   32,  1, "",  "",  "",  ""),
    ("L>7",   32,  1, "",  "",  "",  ""),
    ("L>8",   32,  1, "",  "",  "",  ""),
    ("L>9",   32,  1, "",  "",  "",  ""),
    ("L>10",  32,  1, "",  "",  "",  ""),
    ("L>11",  32,  1, "",  "",  "",  ""),
    ("L>12",  32,  1, "",  "",  "",  ""),
    ("L>13",  32,  1, "",  "",  "",  ""),
    ("L>14",  32,  1, "",  "",  "",  ""),
    ("L>15",  32,  1, "",  "",  "",  ""),
    ("L>16",  31,  1, "",  "",  "",  ""),
    # ── M ─────────────────────────────────────────────────────────────────────
    ("M>1",   32,  1, "",  "",  "",  ""),
    ("M>2",   32,  1, "",  "",  "",  ""),
    ("M>3",   32,  1, "",  "",  "",  ""),
    ("M>4",   32,  1, "",  "",  "",  ""),
    ("M>5",   32,  1, "",  "",  "",  ""),
    ("M>6",   32,  1, "",  "",  "",  ""),
    ("M>7",   32,  1, "",  "",  "",  ""),
    ("M>8",   32,  1, "",  "",  "",  ""),
    ("M>9",   32,  1, "",  "",  "",  ""),
    ("M>10",  32,  1, "",  "",  "",  ""),
    ("M>11",  32,  1, "",  "",  "",  ""),
    ("M>12",  32,  1, "",  "",  "",  ""),
    ("M>13",  32,  1, "",  "",  "",  ""),
    ("M>14",  32,  1, "",  "",  "",  ""),
    ("M>15",  32,  1, "",  "",  "",  ""),
    # ── X ─────────────────────────────────────────────────────────────────────
    ("X>1",    7,  1, "",  "",  "",  ""),
    ("X>2",    7,  1, "",  "",  "",  ""),
    ("X>3",    8,  1, "",  "",  "",  ""),
    ("X>4",   21,  1, "",  "",  "",  ""),
    ("X>5",   21,  1, "",  "",  "",  ""),
    ("X>6",   21,  1, "",  "",  "",  ""),
    ("X>7",   22,  1, "",  "",  "",  ""),
    ("X>8",   22,  1, "",  "",  "",  ""),
    ("X>9",   22,  1, "",  "",  "",  ""),
    ("X>10",  22,  1, "",  "",  "",  ""),
    ("X>11",  22,  1, "",  "",  "",  ""),
    # ── Y ─────────────────────────────────────────────────────────────────────
    ("Y>1",   35,  1, "",  "",  "",  "F"),
    ("Y>2",   28,  1, "",  "",  "",  "F"),
    # ── V ─────────────────────────────────────────────────────────────────────
    ("V>1",   63,  1, "",  "R", "",  ""),
    ("V>2",   63,  1, "",  "R", "",  ""),
    ("V>3",   63,  1, "",  "R", "",  ""),
    ("V>4",   63,  1, "",  "R", "",  ""),
    ("V>5",   20, 44, "",  "R", "",  ""),   # physical beds 44–63
]


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 6 — NET MAP CONSTRUCTION
# ════════════════════════════════════════════════════════════════════════════════

def _bed_to_lode(bed_num, n_p, n_m):
    """Map a 1-indexed position within a row to its lode zone."""
    if bed_num <= 0:
        return 'p'
    if bed_num <= n_p:
        return 'p'
    if bed_num <= n_p + n_m:
        return 'm'
    return 'e'


def _compute_thirds(n_beds):
    """Split n_beds into (n_p, n_m, n_e). Proximal gets remainder first."""
    base = n_beds // 3
    rem  = n_beds % 3
    return base + (1 if rem >= 1 else 0), base + (1 if rem >= 2 else 0), base


def _build_net_map():
    """Build net_map and bed_info from embedded _BEDS_RAW."""

    def _alley_node(alley, lode):
        # If the registered alley string is itself a real graph node
        # (e.g. "A3-5_p"), return it as-is rather than appending a lode suffix.
        if alley in _G_FOOT.nodes:
            return alley
        return f"{alley}_{lode}"

    net_map  = {}
    bed_info = {}

    for loc_row, n_beds, bed_start, rack, road, wall, fence in _BEDS_RAW:
        parts   = loc_row.split(">")
        section = parts[0]
        rownum  = parts[1]
        alleys  = _ROW_TO_ALLEYS.get(loc_row, [])

        # Named stops — Rack, Road, Wall, Fence
        is_d23 = (section == "D" and rownum in ("2", "3", "2-4", "23"))
        if rack == "R":
            lode = "p" if is_d23 else "m"
            for alley in alleys:
                net_map.setdefault(f"{loc_row}>Rack", set()).add(_alley_node(alley, lode))
        if road == "R":
            for alley in alleys:
                net_map.setdefault(f"{loc_row}>Road", set()).add(_alley_node(alley, "p"))
        if wall == "W":
            for alley in alleys:
                net_map.setdefault(f"{loc_row}>Wall", set()).add(_alley_node(alley, "e"))
        if fence == "F":
            for alley in alleys:
                net_map.setdefault(f"{loc_row}>Fence", set()).add(_alley_node(alley, "e"))

        # Numbered beds
        if n_beds == 0:
            continue

        n_p, n_m, n_e = _compute_thirds(n_beds)
        bed_info[loc_row] = {"n_beds": n_beds, "n_p": n_p, "n_m": n_m, "n_e": n_e}

        # Bed 0 → always proximal (road/entrance side)
        if bed_start == 1:
            for alley in alleys:
                net_map.setdefault(f"{loc_row}>0", set()).add(_alley_node(alley, "p"))

        for i, bed in enumerate(range(bed_start, bed_start + n_beds)):
            lode = _bed_to_lode(i + 1, n_p, n_m)
            key  = f"{loc_row}>{bed}"
            for alley in alleys:
                net_map.setdefault(key, set()).add(_alley_node(alley, lode))

    # Convert sets → sorted lists
    return {k: sorted(v) for k, v in net_map.items()}, bed_info


_NET_MAP, _BED_INFO = _build_net_map()


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 7 — BED ORDERING HELPERS
# ════════════════════════════════════════════════════════════════════════════════

_LODE_RANK = {"_p": 0, "_m": 1, "_e": 2}


def _lode_of(node):
    """Return '_p', '_m', '_e', or None for flat/transit nodes."""
    for suffix in ("_p", "_m", "_e"):
        if node.endswith(suffix):
            return suffix
    return None


def _alley_of(node):
    """Strip lode suffix → alley prefix; None for flat nodes."""
    lode = _lode_of(node)
    return node[:-len(lode)] if lode else None


def _bed_num_of(loc):
    """Parse the bed number from 'Section>Row>BedNum'; 0 if not parseable."""
    parts = loc.split(">")
    if len(parts) == 3:
        try:
            return int(parts[2])
        except ValueError:
            pass
    return 0


def _traversal_ascending(node, idx, route_nodes):
    """
    True  → sort beds at this node ascending  (low→high, p→e direction)
    False → sort beds at this node descending (high→low, e→p direction)

    Direction is inferred from the surrounding nodes in the TSP route:
      1. If the preceding node is in the same alley at a lower lode rank,
         we are travelling p→e → ascending.
      2. If the preceding node is in the same alley at a higher lode rank,
         we are travelling e→p → descending.
      3. Fall through to next node for single-zone groups with no prior context.
      4. Default → ascending (entering from proximal / road side).
    """
    alley = _alley_of(node)
    if alley is None:
        return True  # flat node — ascending by number is sensible

    current_rank = _LODE_RANK.get(_lode_of(node), 1)

    prev_node = route_nodes[idx - 1] if idx > 0 else None
    next_node = route_nodes[idx + 1] if idx < len(route_nodes) - 1 else None

    # Check preceding node first
    if prev_node and _alley_of(prev_node) == alley:
        prev_rank = _LODE_RANK.get(_lode_of(prev_node), 1)
        return prev_rank <= current_rank

    # Fall through to following node
    if next_node and _alley_of(next_node) == alley:
        next_rank = _LODE_RANK.get(_lode_of(next_node), 1)
        return current_rank <= next_rank

    # No alley context — default: entering from proximal/road side → ascending
    return True


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 8 — ROUTING INTERNALS
# ════════════════════════════════════════════════════════════════════════════════

_CART_SPEED_FACTOR = 0.4   # cart ≈ 2.5× faster than walking
_FAN_OUT_LIMIT     = 1/12  # 5 seconds — ignore trivial walk legs in cost


def _nearest_cart_node(target):
    """Return (cart_node, walk_minutes) for the closest cart-accessible node."""
    if target in _G_CART.nodes:
        return target, 0.0
    best_node, best_dist = None, float("inf")
    for cnode in _G_CART.nodes:
        try:
            d = float(nx.shortest_path_length(_G_FOOT, cnode, target, weight="weight"))
            if d < best_dist:
                best_dist, best_node = d, cnode
        except nx.NetworkXNoPath:
            continue
    return best_node, best_dist


def _resolve_overlap(candidates, all_candidates, current_park, cart_matrix):
    """
    Given multiple candidate alley nodes for a single bed (joint row), pick the
    one most visited by other stops in this route; break ties by route cost.
    """
    if len(candidates) == 1:
        return candidates[0]

    counts    = {c: all_candidates.count(c) for c in candidates}
    max_count = max(counts.values())
    top       = [c for c, cnt in counts.items() if cnt == max_count]

    if len(top) == 1:
        return top[0]

    def _cost(node):
        park, foot = _nearest_cart_node(node)
        drive = cart_matrix.get((current_park, park), float("inf"))
        return drive + (0.0 if foot <= _FAN_OUT_LIMIT else foot)

    return min(top, key=_cost)


def _run_tsp(unique_nodes, start, end, vehicle, dist_matrix, node_meta=None):
    """
    Nearest-neighbour TSP over unique_nodes.
    Returns (route_nodes, total_cost).
    node_meta is required for cart_walk mode.
    """
    unvisited  = list(unique_nodes)
    route      = []
    total_cost = 0.0

    if vehicle == "cart_walk" and node_meta:
        current_park = start
        while unvisited:
            def _c(n, cp=current_park):
                park = node_meta[n]["park"]
                foot = node_meta[n]["foot_dist"]
                drive = dist_matrix.get((cp, park), float("inf"))
                return drive + (0.0 if foot <= _FAN_OUT_LIMIT else foot)

            nxt          = min(unvisited, key=_c)
            park         = node_meta[nxt]["park"]
            foot         = node_meta[nxt]["foot_dist"]
            drive        = dist_matrix.get((current_park, park), float("inf"))
            total_cost  += drive + (0.0 if foot <= _FAN_OUT_LIMIT else foot)
            route.append(nxt)
            unvisited.remove(nxt)
            current_park = park
        total_cost += dist_matrix.get((current_park, end), 0.0)

    else:
        current = start
        while unvisited:
            nxt         = min(unvisited, key=lambda n: dist_matrix.get((current, n), float("inf")))
            total_cost += dist_matrix.get((current, nxt), float("inf"))
            route.append(nxt)
            unvisited.remove(nxt)
            current = nxt
        total_cost += dist_matrix.get((current, end), 0.0)

    return route, total_cost


# ════════════════════════════════════════════════════════════════════════════════
# SECTION 9 — PUBLIC API
# ════════════════════════════════════════════════════════════════════════════════

def optimize(locations, vehicle="foot"):
    """
    Optimise a pick route.

    Parameters
    ----------
    locations : list[str]
        locations[0]  = start depot  (e.g. "Offices")
        locations[-1] = end depot    (e.g. "Will Call")
        locations[1:-1] = stops to visit (bed strings or graph node names)
    vehicle : str
        "foot" | "cart" | "cart_walk"

    Returns
    -------
    ordered : list[str]
        Middle stops reordered for shortest route.  Beds within each
        alley zone are sorted by number in the direction of traversal.
    total_time : float | None
        Estimated minutes; None if the route is disconnected.
    errors : list[str]
        Unresolvable stops (skipped from output).
    """
    if len(locations) < 2:
        raise ValueError("locations must contain at least a start and an end.")

    start  = _normalize(locations[0])
    end    = _normalize(locations[-1])
    stops  = [_normalize(loc) for loc in locations[1:-1]]
    errors = []

    G_vehicle = _GRAPH_MAP.get(vehicle, _G_FOOT)

    # ── 1. First pass: collect all candidate nodes ────────────────────────────
    raw_resolved   = []
    all_candidates = []
    for stop in stops:
        if not stop:
            continue
        if stop in _NET_MAP:
            candidates = _NET_MAP[stop]
            raw_resolved.append({"bed": stop, "candidates": candidates})
            all_candidates.extend(candidates)
        elif stop in _G_FOOT.nodes:
            raw_resolved.append({"bed": stop, "candidates": [stop]})
            all_candidates.append(stop)
        else:
            errors.append(f"'{stop}' not found in net map or graph — skipped.")

    if not raw_resolved:
        return [], None, errors

    # ── 2. Build distance matrix ──────────────────────────────────────────────
    unique_candidates = list(set(all_candidates) | {start, end})
    dist_matrix = {}
    for a in unique_candidates:
        for b in unique_candidates:
            if a == b:
                dist_matrix[(a, b)] = 0.0
            else:
                try:
                    dist_matrix[(a, b)] = float(
                        nx.shortest_path_length(G_vehicle, a, b, weight="weight")
                    ) * _CART_SPEED_FACTOR
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    dist_matrix[(a, b)] = float("inf")

    # ── 3. Resolve each stop to a single node (overlap resolution) ────────────
    resolved     = []
    current_park = start
    for item in raw_resolved:
        node = _resolve_overlap(
            item["candidates"], all_candidates, current_park, dist_matrix
        )
        resolved.append({"bed": item["bed"], "node": node})

    # ── 4. Group beds by node (preserve input order within each node) ─────────
    node_to_beds = {}
    for item in resolved:
        node_to_beds.setdefault(item["node"], []).append(item["bed"])

    unique_nodes = list(dict.fromkeys(r["node"] for r in resolved))

    # ── 5. Build cart_walk node metadata if needed ────────────────────────────
    node_meta = None
    if vehicle == "cart_walk":
        node_meta = {}
        for node in unique_nodes:
            park, foot_dist = _nearest_cart_node(node)
            node_meta[node] = {"park": park, "foot_dist": foot_dist}

    # ── 6. TSP ────────────────────────────────────────────────────────────────
    route_nodes, total_cost = _run_tsp(
        unique_nodes, start, end, vehicle, dist_matrix, node_meta
    )

    # ── 7. Sort beds within each node using traversal direction ───────────────
    for idx, node in enumerate(route_nodes):
        beds = node_to_beds.get(node, [])
        if len(beds) > 1:
            ascending = _traversal_ascending(node, idx, route_nodes)
            beds.sort(key=_bed_num_of, reverse=not ascending)
            node_to_beds[node] = beds

    # ── 8. Flatten to ordered stop list ───────────────────────────────────────
    ordered = []
    for node in route_nodes:
        ordered.extend(node_to_beds.get(node, [node]))

    total_time = round(total_cost, 4) if total_cost < float("inf") else None
    return ordered, total_time, errors

