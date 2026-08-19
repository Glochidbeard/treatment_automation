import io
import os
import re
import sys
from datetime import date, timedelta

import pandas as pd
from flask import Flask, render_template, request

sys.path.insert(0, os.path.dirname(__file__))
from opt_plugin import optimize

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ── Bin keys ──────────────────────────────────────────────────────────────────
BIN_BANNED  = "pre_banned"   # pre-emergent inside — banned, no export
BIN_PRE     = "pre_out"      # pre-emergent outside (≥1G, outside, in window)
BIN_RS_IN   = "rs_in"        # rootshield inside   (liners: F1-5, F7-10, A3-5, B1-2)
BIN_RS_OUT  = "rs_out"       # rootshield outside  (other outside liners + sensitive large)
BIN_RPS_IN  = "rps_in"       # rootshield+seaweed inside  (liners: F7-10, A3-5, B1-2)
BIN_RPS_OUT = "rps_out"      # rootshield+seaweed outside (B3-6 any + outside liners)

ACTIVE_BINS = [BIN_PRE, BIN_RS_IN, BIN_RS_OUT, BIN_RPS_IN, BIN_RPS_OUT]

# Sensitive species → Rootshield Outside for large containers.
# Future work: drive this from a database.
SENSITIVE_GENERA: set = set()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_loc(s):
    if not isinstance(s, str):
        return ""
    s = re.sub(r"\s*>\s*", ">", s.strip())
    parts = s.split(">")
    out = []
    for part in parts:
        stripped = part.lstrip("0")
        if stripped != part and (stripped.isdigit() or stripped == ""):
            out.append(stripped or "0")
        else:
            out.append(part)
    return ">".join(out)


def _size_gallons(size_str):
    if not isinstance(size_str, str):
        return None
    s = size_str.strip().upper()
    if s.endswith("G"):
        try:
            val = float(s[:-1])
            return val if val >= 1 else None
        except ValueError:
            return None
    return None


def _is_liner(size_str):
    """Tray / flat / plug sizes — non-gallon, non-empty size strings."""
    if not isinstance(size_str, str):
        return False
    s = size_str.strip()
    return bool(s) and s.upper() not in ("", "NAN") and not s.upper().endswith("G")


def _week_shifted(crop_code):
    if not isinstance(crop_code, str):
        return ""
    digits = "".join(c for c in crop_code if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


def _date_week_str(dt):
    """Return WWZZ string from a parsed date, or None if NaT/NaN."""
    try:
        if pd.isna(dt):
            return None
    except Exception:
        return None
    iso = dt.isocalendar()
    return f"{iso[1]:02d}{str(iso[0])[-2:]}"


def _crop_code_week_key(crop_code):
    if not isinstance(crop_code, str):
        return None
    digits = "".join(c for c in crop_code if c.isdigit())
    if len(digits) < 4:
        return None
    last4 = digits[-4:]
    try:
        return (2000 + int(last4[2:]), int(last4[:2]))
    except ValueError:
        return None


def _iso_week_key(dt):
    if pd.isna(dt):
        return (0, 0)
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def _section_row(location):
    """Parse 'A > 3 > Bed 1' → ('A', 3). Returns (None, None) on failure."""
    if not isinstance(location, str):
        return None, None
    norm = re.sub(r"\s*>\s*", ">", location.strip())
    parts = norm.split(">")
    section = parts[0].strip().upper() if parts else None
    if len(parts) < 2:
        return section, None
    try:
        return section, int(parts[1].strip())
    except (ValueError, IndexError):
        return section, None


def _is_inside(location):
    """Covered/indoor locations: F rows 1-10 and A rows 3-5."""
    section, row = _section_row(location)
    if section == "F" and row is not None and 1 <= row <= 10:
        return True
    if section == "A" and row is not None and 3 <= row <= 5:
        return True
    return False


def _legal_code(location):
    """DXF006 for G or X areas; DXW001 everywhere else."""
    section, _ = _section_row(location)
    if section in ("G", "X"):
        return "DXF006"
    return "DXW001"


def _assign_bins(location, is_liner, is_large, species):
    """Return frozenset of bin keys for one row."""
    section, row = _section_row(location)
    bins = set()

    # Pre-emergent: large containers (≥1G) only
    if is_large:
        if _is_inside(location):
            bins.add(BIN_BANNED)
        else:
            bins.add(BIN_PRE)
            genus = (species or "").strip().split()[0].lower() if species else ""
            if genus in SENSITIVE_GENERA:
                bins.add(BIN_RS_OUT)

    # Rootshield / RPS: liner sizes
    if is_liner:
        # RS Inside zone: F1-5, F7-10, A3-5, B1-2
        in_rs_in = bool(
            (section == "F" and row is not None and (1 <= row <= 5 or 7 <= row <= 10)) or
            (section == "A" and row is not None and 3 <= row <= 5) or
            (section == "B" and row is not None and 1 <= row <= 2)
        )
        # RPS Inside zone: same minus F1-5
        in_rps_in = bool(
            (section == "F" and row is not None and 7 <= row <= 10) or
            (section == "A" and row is not None and 3 <= row <= 5) or
            (section == "B" and row is not None and 1 <= row <= 2)
        )
        f1_to_f5 = bool(section == "F" and row is not None and 1 <= row <= 5)

        if in_rs_in:
            bins.add(BIN_RS_IN)
        else:
            bins.add(BIN_RS_OUT)

        if in_rps_in:
            bins.add(BIN_RPS_IN)
        elif not f1_to_f5:
            # Not in RPS inside zone and not F1-F5 → RPS Outside
            bins.add(BIN_RPS_OUT)
        # F1-F5 liners: RS Inside only, no RPS assignment

    # B3-B6 any size → also add to RPS Outside
    if section == "B" and row is not None and 3 <= row <= 6:
        bins.add(BIN_RPS_OUT)

    return frozenset(bins)


def _primary_bin(bins):
    """First active bin in priority order, for Add-from-extras routing."""
    for b in ACTIVE_BINS:
        if b in bins:
            return b
    return ""


def _week_options(n_back=52):
    today = date.today()
    options = []
    for i in range(n_back, -1, -1):
        d = today - timedelta(weeks=i)
        iso = d.isocalendar()
        y, w = iso[0], iso[1]
        options.append((f"{y}-{w}", f"W{w:02d}-{str(y)[-2:]}"))
    seen, unique = set(), []
    for v, l in options:
        if v not in seen:
            seen.add(v)
            unique.append((v, l))
    return unique


def _parse_week_value(value):
    y, w = value.split("-")
    return (int(y), int(w))


# ── Processing ────────────────────────────────────────────────────────────────

def process_inventory(df, from_key, to_key):
    col_map = {
        "Locations":          "location",
        "Product":            "product",
        "Species":            "species",
        "Current size":       "size",
        "Available qty":      "qty",
        "Crop code":          "crop_code",
        "Size last changed at": "date_changed",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    product_col = df["product"].astype(str) if "product" in df.columns else pd.Series("", index=df.index)
    if "species" not in df.columns:
        df["species"] = product_col
    else:
        df["species"] = df["species"].astype(str)
        mask = df["species"].isna() | df["species"].str.strip().isin(["", "nan"])
        df["species"] = df["species"].where(~mask, product_col)

    df["size_val"] = df["size"].apply(_size_gallons)
    df["is_large"] = df["size_val"].notna().astype(bool)
    df["is_liner"] = df["size"].apply(_is_liner).astype(bool)

    active   = df[df["is_large"] | df["is_liner"]].copy()
    excluded = df[~(df["is_large"] | df["is_liner"])].copy()

    active["date_parsed"] = pd.to_datetime(active["date_changed"], errors="coerce")

    def in_window(dt):
        return from_key <= _iso_week_key(dt) <= to_key

    def cc_in_window(cc):
        k = _crop_code_week_key(cc)
        return k is not None and from_key <= k <= to_key

    active["row_in_window"] = active["date_parsed"].apply(in_window).astype(bool)
    mask_in_window      = active["row_in_window"]
    mask_undated_sibling = (
        active["date_parsed"].isna() &
        active["crop_code"].apply(cc_in_window).astype(bool)
    )
    in_range     = active[mask_in_window | mask_undated_sibling].copy()
    out_of_range = active[~active.index.isin(in_range.index)].copy()

    # Assign bins and compute overlap count
    in_range["bins"] = in_range.apply(
        lambda r: _assign_bins(r["location"], bool(r["is_liner"]), bool(r["is_large"]), r["species"]),
        axis=1,
    )
    in_range["bin_count"] = in_range["bins"].apply(lambda b: len(b - {BIN_BANNED}))

    # Assign bins to out_of_range so extras know which bin to route Add clicks to
    out_of_range["bins"] = out_of_range.apply(
        lambda r: _assign_bins(r["location"], bool(r["is_liner"]), bool(r["is_large"]), r["species"]),
        axis=1,
    )
    out_of_range["bin_count"] = 0

    # Sort in_range by representative date per crop code
    code_date = active.groupby("crop_code")["date_parsed"].max().rename("rep_date")
    in_range  = in_range.join(code_date, on="crop_code")
    in_range  = in_range.sort_values(
        ["rep_date", "crop_code", "location"], ascending=[False, True, True]
    )

    for frame in (in_range, out_of_range, excluded):
        frame["week_shifted"] = frame["crop_code"].apply(_week_shifted) \
            if "crop_code" in frame.columns else pd.Series("", index=frame.index)
        frame["inside"]     = frame["location"].apply(_is_inside)
        frame["legal_code"] = frame["location"].apply(_legal_code)

    return in_range, out_of_range, excluded


def route_order(df):
    if df.empty:
        return df, None, []
    df = df.copy()
    df["loc_norm"] = df["location"].apply(_normalize_loc)
    seen = []
    for loc in df["loc_norm"]:
        if loc not in seen:
            seen.append(loc)
    stops = ["Offices"] + seen + ["Offices"]
    try:
        ordered, total_time, errors = optimize(stops, vehicle="cart_walk")
    except Exception as e:
        df.drop(columns=["loc_norm"], inplace=True)
        return df, None, [str(e)]
    order_map = {loc: i for i, loc in enumerate(ordered)}
    df["_order"] = df["loc_norm"].map(lambda l: order_map.get(l, len(ordered)))
    df = df.sort_values("_order").drop(columns=["_order", "loc_norm"])
    return df, total_time, errors


def build_rows(df, include_primary_bin=False):
    rows = []
    for _, r in df.iterrows():
        row = {
            "row_id":       int(r.get("row_id", 0)),
            "location":     str(r.get("location", "")),
            "species":      str(r.get("species", "")),
            "size":         str(r.get("size", "")),
            "qty":          str(r.get("qty", "")),
            "week_shifted": str(r.get("week_shifted", "")),
            "inside":       bool(r.get("inside", False)),
            "legal_code":   str(r.get("legal_code", "DXW001")),
            "bin_count":    int(r.get("bin_count", 0)),
        }
        if include_primary_bin:
            row["primary_bin"] = _primary_bin(r.get("bins", frozenset()))
        rows.append(row)
    return rows


# ── Routes ────────────────────────────────────────────────────────────────────

def _default_weeks():
    today = date.today().isocalendar()
    d_from = date.today() - timedelta(weeks=2)
    iso_from = d_from.isocalendar()
    return (
        f"{iso_from[0]}-{iso_from[1]}",
        f"{today[0]}-{today[1]}",
    )


@app.route("/")
def index():
    options = _week_options(52)
    default_from, default_to = _default_weeks()
    return render_template("index.html", week_options=options,
                           default_from=default_from, default_to=default_to)


@app.route("/process", methods=["POST"])
def process():
    options = _week_options(52)
    default_from, default_to = _default_weeks()

    if "file" not in request.files or request.files["file"].filename == "":
        return render_template("index.html", error="Please select a CSV file.",
                               week_options=options,
                               default_from=default_from, default_to=default_to)

    from_val = request.form.get("from_week", default_from)
    to_val   = request.form.get("to_week",   default_to)
    try:
        from_key = _parse_week_value(from_val)
        to_key   = _parse_week_value(to_val)
    except Exception:
        from_key = _parse_week_value(default_from)
        to_key   = _parse_week_value(default_to)

    if from_key > to_key:
        from_key, to_key = to_key, from_key

    f = request.files["file"]
    try:
        content = f.read().decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(content))
        df["row_id"] = range(len(df))
    except Exception as e:
        return render_template("index.html", error=f"Could not parse file: {e}",
                               week_options=options,
                               default_from=default_from, default_to=default_to)

    try:
        in_range, out_of_range, excluded = process_inventory(df, from_key, to_key)
    except Exception as e:
        return render_template("index.html", error=f"Processing error: {e}",
                               week_options=options,
                               default_from=default_from, default_to=default_to)

    try:
        # Build per-bin dataframes with route optimization
        opt_errors  = []
        total_time  = 0
        bin_rows    = {}
        for bk in ACTIVE_BINS:
            bdf = in_range[in_range["bins"].apply(lambda b: bk in b)].copy()
            bdf, t, errs = route_order(bdf)
            opt_errors.extend(errs)
            if t:
                total_time += t
            bin_rows[bk] = build_rows(bdf)

        banned_rows = build_rows(
            in_range[in_range["bins"].apply(lambda b: BIN_BANNED in b)].copy()
        )
        extra_rows  = build_rows(out_of_range, include_primary_bin=True)
        extra_rows += build_rows(excluded)
    except Exception as e:
        import traceback
        return render_template("index.html",
                               error=f"Build error: {e} — {traceback.format_exc()}",
                               week_options=options,
                               default_from=default_from, default_to=default_to)

    def wlabel(k):
        return f"W{k[1]:02d}-{str(k[0])[-2:]}"

    return render_template(
        "result.html",
        filename     = f.filename,
        cutoff_week  = wlabel(from_key),
        current_week = wlabel(to_key),
        total_time   = round(total_time, 1) if total_time else None,
        banned_rows  = banned_rows,
        pre_rows     = bin_rows[BIN_PRE],
        rs_in_rows   = bin_rows[BIN_RS_IN],
        rs_out_rows  = bin_rows[BIN_RS_OUT],
        rps_in_rows  = bin_rows[BIN_RPS_IN],
        rps_out_rows = bin_rows[BIN_RPS_OUT],
        extra_rows   = extra_rows,
        opt_errors   = opt_errors,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
