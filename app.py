import io
import os
import re
import sys
from datetime import date

import pandas as pd
from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(__file__))
from opt_plugin import optimize

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_loc(s):
    """Match opt_plugin's _normalize so we can align df rows with optimized output."""
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
    """Return numeric value if size is >= 1G, else None."""
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


def _week_shifted(crop_code):
    """Last 4 digits of the crop code string (e.g. '596-22-0426' → '0426')."""
    if not isinstance(crop_code, str):
        return ""
    digits = "".join(c for c in crop_code if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


def _iso_week_key(dt):
    """(year, week) as a comparable tuple from a datetime."""
    if pd.isna(dt):
        return (0, 0)
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def _current_week_key():
    iso = date.today().isocalendar()
    return (iso[0], iso[1])


def _week_key_minus(key, n):
    """Subtract n weeks from an (year, week) key."""
    y, w = key
    total = y * 52 + w - n
    return (total // 52, total % 52 or 52)


# ── processing ────────────────────────────────────────────────────────────────

def process_inventory(df):
    col_map = {
        "Locations": "location",
        "Product": "product",
        "Species": "species",
        "Current size": "size",
        "Available qty": "qty",
        "Crop code": "crop_code",
        "Size last changed at": "date_changed",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # Species fallback to Product
    product_col = df["product"].astype(str) if "product" in df.columns else pd.Series("", index=df.index)
    if "species" not in df.columns:
        df["species"] = product_col
    else:
        df["species"] = df["species"].astype(str)
        mask = df["species"].isna() | (df["species"].str.strip().isin(["", "nan"]))
        df["species"] = df["species"].where(~mask, product_col)

    # Filter size >= 1G
    df["size_val"] = df["size"].apply(_size_gallons)
    passed = df[df["size_val"].notna()].copy()
    excluded = df[df["size_val"].isna()].copy()

    # Parse dates
    passed["date_parsed"] = pd.to_datetime(passed["date_changed"], errors="coerce")

    # 3-week window: current week and 2 prior (e.g. weeks 22-24)
    cur_key = _current_week_key()
    cutoff_key = _week_key_minus(cur_key, 2)

    def in_window(dt):
        k = _iso_week_key(dt)
        return k >= cutoff_key

    # Key by crop code, not individual row date:
    # TimeSaver only stamps a date on the first bed of a lot — the rest of the
    # beds for that lot have NaN dates.  So we find which crop codes have ANY
    # bed with an in-window date, then pull in ALL beds for those crop codes.
    passed["row_in_window"] = passed["date_parsed"].apply(in_window)
    in_window_codes = set(
        passed.loc[passed["row_in_window"], "crop_code"].dropna().unique()
    )
    in_range = passed[passed["crop_code"].isin(in_window_codes)].copy()
    out_of_range = passed[~passed["crop_code"].isin(in_window_codes)].copy()

    # Give every row a representative date (the most-recent date for its crop code)
    # so we can sort lots from newest to oldest even when individual rows lack dates.
    code_date = (
        passed.groupby("crop_code")["date_parsed"]
        .max()
        .rename("rep_date")
    )
    in_range = in_range.join(code_date, on="crop_code")

    # Sort: newest lot first, then by location within the lot
    in_range = in_range.sort_values(
        ["rep_date", "crop_code", "location"], ascending=[False, True, True]
    )

    # Week shifted column
    for frame in (in_range, out_of_range, excluded):
        if "crop_code" in frame.columns:
            frame["week_shifted"] = frame["crop_code"].apply(_week_shifted)
        else:
            frame["week_shifted"] = ""

    return in_range, out_of_range, excluded


def build_rows(df):
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "row_id": int(r.get("row_id", 0)),
            "location": str(r.get("location", "")),
            "species": str(r.get("species", "")),
            "size": str(r.get("size", "")),
            "qty": str(r.get("qty", "")),
            "week_shifted": str(r.get("week_shifted", "")),
        })
    return rows


def route_order(df):
    """Return df rows ordered by optimized route, plus optimizer metadata."""
    if df.empty:
        return df, None, []

    df = df.copy()
    df["loc_norm"] = df["location"].apply(_normalize_loc)

    # Unique locations in original order for the optimizer
    seen = []
    for loc in df["loc_norm"]:
        if loc not in seen:
            seen.append(loc)

    stops = ["Offices"] + seen + ["Offices"]
    try:
        ordered, total_time, errors = optimize(stops, vehicle="cart_walk")
    except Exception as e:
        return df, None, [str(e)]

    # Build order map: normalized loc → position
    order_map = {loc: i for i, loc in enumerate(ordered)}

    # Assign sort key; unresolved locs go to end
    df["_order"] = df["loc_norm"].map(lambda l: order_map.get(l, len(ordered)))
    df = df.sort_values("_order").drop(columns=["_order", "loc_norm"])

    return df, total_time, errors


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    if "file" not in request.files or request.files["file"].filename == "":
        return render_template("index.html", error="Please select a CSV file.")

    f = request.files["file"]
    try:
        content = f.read().decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(content))
        df["row_id"] = range(len(df))   # stable identity — survives all filtering/sorting
    except Exception as e:
        return render_template("index.html", error=f"Could not parse file: {e}")

    in_range, out_of_range, excluded = process_inventory(df)

    # Route-optimize the in-window rows
    in_range_ordered, total_time, opt_errors = route_order(in_range)

    main_rows = build_rows(in_range_ordered)
    extra_rows = build_rows(out_of_range) + build_rows(excluded)

    cur_y, cur_w = _current_week_key()
    cut_y, cut_w = _week_key_minus((cur_y, cur_w), 2)

    return render_template(
        "result.html",
        main_rows=main_rows,
        extra_rows=extra_rows,
        total_time=round(total_time, 1) if total_time else None,
        opt_errors=opt_errors,
        current_week=f"W{cur_w:02d}-{str(cur_y)[-2:]}",
        cutoff_week=f"W{cut_w:02d}-{str(cut_y)[-2:]}",
        filename=f.filename,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
