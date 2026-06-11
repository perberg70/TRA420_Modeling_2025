"""
Standalone script to plot annual and cumulative CO₂ savings from existing results.

Inputs: a results directory containing aggregated and per-country emission CSVs:
  <results_root>/emissions/All_countries/<mix>/co2.csv
  <results_root>/emissions/<mix>/<Country>/co2.csv

Outputs: plots under <output_root>/<mix>/..., filenames include the mix name.
Defaults target years 2030 and 2050 and demand cases scen1_lower/scen1_mean/scen1_upper.

Key flags:
- --results-root: path to results root (default: results).
- --output-root: base directory for plots (default: <results-root>/summary/additional_plots).
- --mix: one or more mix names (default: discover under emissions/All_countries).
- --years: annual reporting years (default: 2030 2050).
- --cumulative-years: endpoints for cumulative savings (default: 2030 2050).
- --baseline-demand: baseline demand case (default: base_demand).
- --mean-case / --lower-case / --upper-case: demand cases used for mean/err bars.

Run example:
  python scripts/additional_plots_standalone.py \\
    --results-root results/global \\
    --mix base_mix WEM WAM
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib.pyplot as plt
import pandas as pd

LOGGER = logging.getLogger("additional_plots_standalone")


# ---------- Data helpers ----------


def _load_deltas(
    co2_path: Path, demand_cases: Iterable[str], baseline_case: str
) -> pd.DataFrame:
    if not co2_path.exists():
        raise FileNotFoundError(f"Missing CO₂ file: {co2_path}")
    df = pd.read_csv(co2_path, comment="#")
    if "year" not in df.columns:
        raise ValueError(f"'year' column missing in {co2_path}")
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df.dropna(subset=["year"])
    df["year"] = df["year"].astype(int)
    expected = [f"delta_{d}" for d in demand_cases if d != baseline_case]
    missing = [col for col in expected if col not in df.columns]
    if missing:
        raise KeyError(f"Missing delta columns in {co2_path}: {missing}")
    return df[["year"] + expected]


def _savings_at_year(
    df: pd.DataFrame, year: int, mean_case: str, lower_case: str, upper_case: str
) -> tuple[float, float, float]:
    row = df[df["year"] == year]
    if row.empty:
        raise KeyError(f"Year {year} not found in CO₂ data.")
    mean = -float(row[f"delta_{mean_case}"].iloc[0])
    lower = -float(row[f"delta_{lower_case}"].iloc[0])
    upper = -float(row[f"delta_{upper_case}"].iloc[0])
    err_low = max(mean - lower, 0.0)
    err_high = max(upper - mean, 0.0)
    return mean, err_low, err_high


def _cumulative_savings(
    df: pd.DataFrame, up_to_year: int, mean_case: str, lower_case: str, upper_case: str
) -> tuple[float, float, float]:
    window = df[df["year"] <= up_to_year]
    if window.empty:
        raise KeyError(f"No data up to year {up_to_year} in CO₂ data.")
    mean = -float(window[f"delta_{mean_case}"].sum())
    lower = -float(window[f"delta_{lower_case}"].sum())
    upper = -float(window[f"delta_{upper_case}"].sum())
    err_low = max(mean - lower, 0.0)
    err_high = max(upper - mean, 0.0)
    return mean, err_low, err_high


def _discover_mix_names(results_root: Path) -> list[str]:
    all_countries_root = results_root / "emissions" / "All_countries"
    if not all_countries_root.exists():
        return []
    return sorted([p.name for p in all_countries_root.iterdir() if p.is_dir()])


def _infer_demand_cases(co2_path: Path, baseline_case: str) -> list[str]:
    df = pd.read_csv(co2_path, comment="#", nrows=1)
    demand_cases = []
    for col in df.columns:
        if col.startswith("delta_"):
            demand_cases.append(col.replace("delta_", ""))
    if baseline_case not in demand_cases:
        demand_cases.append(baseline_case)
    return sorted(set(demand_cases))


# ---------- Plot helpers ----------


def _plot_total_saved(
    agg_root: Path,
    demand_cases: Iterable[str],
    baseline_case: str,
    years: list[int],
    output_dir: Path,
    mix: str,
    mean_case: str,
    lower_case: str,
    upper_case: str,
    cumulative_years: list[int] | None = None,
) -> None:
    co2_path = agg_root / "co2.csv"
    df = _load_deltas(co2_path, demand_cases, baseline_case)

    means: list[float] = []
    err_lows: list[float] = []
    err_highs: list[float] = []
    for year in years:
        m, el, eh = _savings_at_year(df, year, mean_case, lower_case, upper_case)
        means.append(m)
        err_lows.append(el)
        err_highs.append(eh)

    fig, ax = plt.subplots(figsize=(6, 4))
    x = range(len(years))
    ax.bar(x, means, yerr=[err_lows, err_highs], capsize=6, color="#2a6fdb")
    ax.set_xticks(x, [str(y) for y in years])
    ax.set_ylabel("Mt CO₂ saved")
    ax.set_title(f"Annual CO₂ Saved (All countries) — {mix}")
    ax.grid(True, axis="y", linestyle="--", alpha=0.3)
    output_dir.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_dir / f"{mix}_annual_total_co2_saved.png", format="png")
    plt.close(fig)

    if cumulative_years:
        cum_means: list[float] = []
        cum_err_lows: list[float] = []
        cum_err_highs: list[float] = []
        for year in cumulative_years:
            m, el, eh = _cumulative_savings(df, year, mean_case, lower_case, upper_case)
            cum_means.append(m)
            cum_err_lows.append(el)
            cum_err_highs.append(eh)
        fig, ax = plt.subplots(figsize=(6, 4))
        x = range(len(cumulative_years))
        ax.bar(x, cum_means, yerr=[cum_err_lows, cum_err_highs], capsize=6, color="#7b68ee")
        ax.set_xticks(x, [str(y) for y in cumulative_years])
        ax.set_ylabel("Mt CO₂ saved (cumulative)")
        ax.set_title(f"Cumulative CO₂ Saved (All countries) — {mix}")
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        fig.tight_layout()
        fig.savefig(output_dir / f"{mix}_cumulative_total_co2_saved.png", format="png")
        plt.close(fig)


def _plot_country_saved(
    per_country_root: Path,
    demand_cases: Iterable[str],
    baseline_case: str,
    years: list[int],
    output_dir: Path,
    mix: str,
    mean_case: str,
    lower_case: str,
    upper_case: str,
    cumulative_years: list[int] | None = None,
) -> None:
    country_dirs = sorted([p for p in per_country_root.iterdir() if p.is_dir()])
    if not country_dirs:
        LOGGER.warning("No country directories found under %s", per_country_root)
        return

    for year in years:
        labels: list[str] = []
        means: list[float] = []
        err_lows: list[float] = []
        err_highs: list[float] = []
        for cdir in country_dirs:
            co2_path = cdir / "co2.csv"
            try:
                df = _load_deltas(co2_path, demand_cases, baseline_case)
                m, el, eh = _savings_at_year(df, year, mean_case, lower_case, upper_case)
            except Exception as exc:
                LOGGER.warning("Skipping %s: %s", co2_path, exc)
                continue
            labels.append(cdir.name)
            means.append(m)
            err_lows.append(el)
            err_highs.append(eh)

        if not labels:
            continue

        fig, ax = plt.subplots(figsize=(8, 4.5))
        x = range(len(labels))
        ax.bar(x, means, yerr=[err_lows, err_highs], capsize=5, color="#2aa05a")
        ax.set_xticks(x, labels, rotation=20, ha="right")
        ax.set_ylabel("Mt CO₂ saved")
        ax.set_title(f"Annual CO₂ Saved by Country — {year} — {mix}")
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        output_dir.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(output_dir / f"{mix}_country_co2_saved_{year}.png", format="png")
        plt.close(fig)

    if cumulative_years:
        for year in cumulative_years:
            labels_c: list[str] = []
            means_c: list[float] = []
            err_lows_c: list[float] = []
            err_highs_c: list[float] = []
            for cdir in country_dirs:
                co2_path = cdir / "co2.csv"
                try:
                    df = _load_deltas(co2_path, demand_cases, baseline_case)
                    m, el, eh = _cumulative_savings(df, year, mean_case, lower_case, upper_case)
                except Exception as exc:
                    LOGGER.warning("Skipping cumulative %s: %s", co2_path, exc)
                    continue
                labels_c.append(cdir.name)
                means_c.append(m)
                err_lows_c.append(el)
                err_highs_c.append(eh)
            if not labels_c:
                continue
            fig, ax = plt.subplots(figsize=(8, 4.5))
            x = range(len(labels_c))
            ax.bar(x, means_c, yerr=[err_lows_c, err_highs_c], capsize=5, color="#7b68ee")
            ax.set_xticks(x, labels_c, rotation=20, ha="right")
            ax.set_ylabel("Mt CO₂ saved (cumulative)")
            ax.set_title(f"CO₂ Saved by Country ({year}) — cumulative — {mix}")
            ax.grid(True, axis="y", linestyle="--", alpha=0.3)
            output_dir.mkdir(parents=True, exist_ok=True)
            fig.tight_layout()
            fig.savefig(output_dir / f"{mix}_country_co2_saved_cumulative_{year}.png", format="png")
            plt.close(fig)


def _plot_country_pairwise(
    per_country_root: Path,
    demand_cases: Iterable[str],
    baseline_case: str,
    years: list[int],
    output_dir: Path,
    mix: str,
    mean_case: str,
    lower_case: str,
    upper_case: str,
) -> None:
    """One plot per country comparing the specified years (e.g., 2030 vs 2050)."""
    target_years = sorted(years)
    country_dirs = sorted([p for p in per_country_root.iterdir() if p.is_dir()])
    if not country_dirs:
        return

    for cdir in country_dirs:
        co2_path = cdir / "co2.csv"
        try:
            df = _load_deltas(co2_path, demand_cases, baseline_case)
        except Exception as exc:
            LOGGER.warning("Skipping %s: %s", co2_path, exc)
            continue

        means: list[float] = []
        err_lows: list[float] = []
        err_highs: list[float] = []
        years_found: list[int] = []
        for year in target_years:
            try:
                m, el, eh = _savings_at_year(df, year, mean_case, lower_case, upper_case)
            except Exception as exc:
                LOGGER.warning("Skipping %s %s: %s", cdir.name, year, exc)
                continue
            means.append(m)
            err_lows.append(el)
            err_highs.append(eh)
            years_found.append(year)

        if not years_found:
            continue

        fig, ax = plt.subplots(figsize=(6, 4))
        x = range(len(years_found))
        ax.bar(x, means, yerr=[err_lows, err_highs], capsize=6, color="#db8b2a")
        ax.set_xticks(x, [str(y) for y in years_found])
        ax.set_ylabel("Mt CO₂ saved")
        ax.set_title(f"CO₂ Saved — {cdir.name} — {mix}")
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        output_dir.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fname = f"{cdir.name}_{mix}_co2_saved.png"
        fig.savefig(output_dir / fname, format="png")
        plt.close(fig)


# ---------- CLI / main ----------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Plot annual and cumulative CO₂ savings from precomputed results."
    )
    parser.add_argument(
        "--results-root",
        default="results",
        help="Path to the results root containing emissions/ (e.g., results or results/global).",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Optional root for outputs; defaults to --results-root/summary/additional_plots.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory for plots (default: <results-root>/summary/additional_plots).",
    )
    parser.add_argument(
        "--mix",
        nargs="*",
        default=None,
        help="Mix scenario(s) to plot (default: discovered under emissions/All_countries).",
    )
    parser.add_argument(
        "--years",
        nargs="*",
        type=int,
        default=[2030, 2050],
        help="Years to plot (annual bars). Default: 2030 2050.",
    )
    parser.add_argument(
        "--cumulative-years",
        nargs="*",
        type=int,
        default=[2030, 2050],
        help="Years to use as endpoints for cumulative savings. Default: 2030 2050.",
    )
    parser.add_argument(
        "--baseline-demand",
        default="base_demand",
        help="Baseline demand case label (default: base_demand).",
    )
    parser.add_argument(
        "--mean-case",
        default="scen1_mean",
        help="Demand case to use as mean for error bars (default: scen1_mean).",
    )
    parser.add_argument(
        "--lower-case",
        default="scen1_lower",
        help="Demand case to use as lower bound (default: scen1_lower).",
    )
    parser.add_argument(
        "--upper-case",
        default="scen1_upper",
        help="Demand case to use as upper bound (default: scen1_upper).",
    )
    args = parser.parse_args()

    results_root = Path(args.results_root).resolve()
    base_output_root = Path(args.output_root).resolve() if args.output_root else results_root
    output_root = (
        Path(args.output).resolve()
        if args.output
        else (base_output_root / "summary" / "additional_plots").resolve()
    )
    mixes: Sequence[str]
    if args.mix:
        mixes = [str(m).strip() for m in args.mix if str(m).strip()]
    else:
        mixes = _discover_mix_names(results_root)
    if not mixes:
        raise SystemExit("No mix scenarios found. Provide --mix or check results_root.")

    years = sorted({int(y) for y in args.years})
    cumulative_years = (
        sorted({int(y) for y in args.cumulative_years}) if args.cumulative_years else []
    )
    baseline_case = str(args.baseline_demand).strip() or "base_demand"
    mean_case = str(args.mean_case).strip()
    lower_case = str(args.lower_case).strip()
    upper_case = str(args.upper_case).strip()

    for mix in mixes:
        agg_root = results_root / "emissions" / "All_countries" / mix
        per_country_root = results_root / "emissions" / mix
        if not agg_root.exists():
            LOGGER.warning("Skipping mix %s: missing %s", mix, agg_root)
            continue
        if not per_country_root.exists():
            LOGGER.warning("Skipping mix %s: missing %s", mix, per_country_root)
            continue

        # Infer demand cases from aggregate CSV if not provided explicitly
        co2_path = agg_root / "co2.csv"
        demand_cases = _infer_demand_cases(co2_path, baseline_case)

        mix_output = output_root / mix
        per_country_output = mix_output / "per_country"

        LOGGER.info("Processing mix %s", mix)
        LOGGER.info("  aggregate: %s", agg_root)
        LOGGER.info("  per-country: %s", per_country_root)
        LOGGER.info("  output: %s", mix_output)

        _plot_total_saved(
            agg_root,
            demand_cases,
            baseline_case,
            years,
            mix_output,
            mix,
            mean_case,
            lower_case,
            upper_case,
            cumulative_years=cumulative_years,
        )
        _plot_country_saved(
            per_country_root,
            demand_cases,
            baseline_case,
            years,
            mix_output,
            mix,
            mean_case,
            lower_case,
            upper_case,
            cumulative_years=cumulative_years,
        )
        _plot_country_pairwise(
            per_country_root,
            demand_cases,
            baseline_case,
            years,
            per_country_output,
            mix,
            mean_case,
            lower_case,
            upper_case,
        )


if __name__ == "__main__":
    main()
