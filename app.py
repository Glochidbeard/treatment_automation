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
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_loc(s):
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


def _week_shifted(crop_code):
    if not isinstance(crop_code, str):
        return ""
    digits = "".join(c for c in crop_code if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


def _iso_week_key(dt):
    if pd.isna(dt):
        return (0, 0)
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def _is_inside(location):
    """True if the location is in a covered/indoor growing area.
    Inside: F rows 1-10, A rows 3-5.
    """
    if not isinstance(location, str):
        return False
    norm = re.sub(r"\s*>\s*", ">", location.strip())
    parts = norm.split(">")
    if len(parts) < 2:
        return False
    section = parts[0].upper()
    try:
        row = int(parts[1])
    except ValueError:
        return False
    if section == "F" and 1 <= row <= 10:
        return True
    if section == "A" and 3 <= row <= 5:
        return True
    return False


def _week_options(n_back=52):
    """Return list of (value, label) for the last n_back weeks ending today."""
    today = date.today()
    options = []
    for i in range(n_back, -1, -1):
        d = today - timedelta(weeks=i)
        iso = d.isocalendar()
        y, w = iso[0], iso[1]
        value = f"{y}-{w}"          # e.g. "2026-24"  (passed in form)
        label = f"W{w:02d}-{str(y)[-2:]}"  # e.g. "W24-26"  (shown to user)
        options.append((value, label))
    # deduplicate while preserving order (same ISO week can cover multiple calendar days)
    seen = set()
    unique = []
    for v, l in options:
        if v not in seen:
            seen.add(v)
            unique.append((v, l))
    return unique


def _parse_week_value(value):
    """Parse '2026-24' → (2026, 24)."""
    y, w = value.split("-")
    return (int(y), int(w))


# ── processing ────────────────────────────────────────────────────────────────

def process_inventory(df, from_key, to_key):
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

    def in_window(dt):
        k = _iso_week_key(dt)
        return from_key <= k <= to_key

    # Key by crop code:
    # TimeSaver only stamps a date on the first bed of a lot; the rest are NaN.
    # Include beds that are explicitly in-window, plus undated siblings of
    # in-window crop codes.  Beds with an explicit out-of-window date stay out
    # even if their crop code matches (they belong to a different potting event).
    passed["row_in_window"] = passed["date_parsed"].apply(in_window)
    in_window_codes = set(
        passed.loc[passed["row_in_window"], "crop_code"].dropna().unique()
    )
    in_range = passed[
        passed["row_in_window"] |
        (passed["crop_code"].isin(in_window_codes) & passed["date_parsed"].isna())
    ].copy()
    out_of_range = passed[~passed.index.isin(in_range.index)].copy()

    # Representative date per crop code for sorting
    code_date = passed.groupby("crop_code")["date_parsed"].max().rename("rep_date")
    in_range = in_range.join(code_date, on="crop_code")
    in_range = in_range.sort_values(
        ["rep_date", "crop_code", "location"], ascending=[False, True, True]
    )

    for frame in (in_range, out_of_range, excluded):
        if "crop_code" in frame.columns:
            frame["week_shifted"] = frame["crop_code"].apply(_week_shifted)
        else:
            frame["week_shifted"] = ""
        frame["inside"] = frame["location"].apply(_is_inside)

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
            "inside": bool(r.get("inside", False)),
        })
    return rows


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
        return df, None, [str(e)]

    order_map = {loc: i for i, loc in enumerate(ordered)}
    df["_order"] = df["loc_norm"].map(lambda l: order_map.get(l, len(ordered)))
    df = df.sort_values("_order").drop(columns=["_order", "loc_norm"])

    return df, total_time, errors


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    options = _week_options(52)
    today_iso = date.today().isocalendar()
    default_to = f"{today_iso[0]}-{today_iso[1]}"
    # default from = 2 weeks back
    d_from = date.today() - timedelta(weeks=2)
    iso_from = d_from.isocalendar()
    default_from = f"{iso_from[0]}-{iso_from[1]}"
    return render_template("index.html", week_options=options,
                           default_from=default_from, default_to=default_to)


@app.route("/process", methods=["POST"])
def process():
    week_options = _week_options(52)
    today_iso = date.today().isocalendar()
    default_to = f"{today_iso[0]}-{today_iso[1]}"
    d_from = date.today() - timedelta(weeks=2)
    iso_from = d_from.isocalendar()
    default_from = f"{iso_from[0]}-{iso_from[1]}"

    if "file" not in request.files or request.files["file"].filename == "":
        return render_template("index.html", error="Please select a CSV file.",
                               week_options=week_options,
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
                               week_options=week_options,
                               default_from=default_from, default_to=default_to)

    in_range, out_of_range, excluded = process_inventory(df, from_key, to_key)

    # Route inside and outside groups independently
    inside_df  = in_range[in_range["inside"]].copy()
    outside_df = in_range[~in_range["inside"]].copy()

    inside_ordered,  inside_time,  inside_errors  = route_order(inside_df)
    outside_ordered, outside_time, outside_errors = route_order(outside_df)

    opt_errors = inside_errors + outside_errors

    inside_rows  = build_rows(inside_ordered)
    outside_rows = build_rows(outside_ordered)
    extra_rows   = build_rows(out_of_range) + build_rows(excluded)

    from_label = f"W{from_key[1]:02d}-{str(from_key[0])[-2:]}"
    to_label   = f"W{to_key[1]:02d}-{str(to_key[0])[-2:]}"

    total_time = None
    if inside_time is not None or outside_time is not None:
        total_time = round((inside_time or 0) + (outside_time or 0), 1)

    return render_template(
        "result.html",
        inside_rows=inside_rows,
        outside_rows=outside_rows,
        extra_rows=extra_rows,
        total_time=total_time,
        opt_errors=opt_errors,
        current_week=to_label,
        cutoff_week=from_label,
        filename=f.filename,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
