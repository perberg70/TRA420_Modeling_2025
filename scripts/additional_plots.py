"""Generate additional emission-savings plots.

Outputs:
  - Bar chart of total CO₂ saved (All countries) at selected years with upper/lower error bars.
  - Bar chart of CO₂ saved per country at selected years with upper/lower error bars.
  - Per-country pairwise bars (e.g., 2030 vs 2050) with error bars.
  - Cumulative totals and per-country bars (e.g., up to 2030 and 2050).

Defaults: uses the repository config to locate emission outputs and writes plots to
``results/<run>/summary/additional_plots/<mix>/``. Positive values indicate emissions avoided.

Key flags:
- --mix: mix scenario(s) to plot (default: calc_emissions.countries.mix_scenarios or baseline mix).
- --years: annual reporting years (default: 2030 2050).
- --config: path to config.yaml (default: repository config).
- --results-root: override results root to point at a specific run (default: config/run directory).
"""

from __future__ import annotations

import argparse
import logging
from math import ceil
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import matplotlib.pyplot as plt
import pandas as pd
import yaml

from calc_emissions.constants import BASE_DEMAND_CASE, BASE_MIX_CASE
from config_paths import apply_results_run_directory, get_config_path, get_results_run_directory

LOGGER = logging.getLogger("additional_plots")
ROOT = Path(__file__).resolve().parents[1]
PM25_SCENARIO = "scen1_mean"


def _load_config(config_path: Path) -> dict:
    with config_path.open() as handle:
        return yaml.safe_load(handle) or {}


def _resolve_paths(
    config: Mapping[str, object], mix: str, results_root_override: Path | None = None
) -> tuple[Path, Path]:
    """Return (aggregate_root, per_country_root) with run directory applied or overridden."""
    if results_root_override is not None:
        base = results_root_override
        agg_root = base / "emissions" / "All_countries" / mix
        per_country_root = base / "emissions" / mix
        return agg_root, per_country_root

    run_directory = get_results_run_directory(config)
    calc_cfg = config.get("calc_emissions", {}) or {}
    countries_cfg = calc_cfg.get("countries", {}) or {}

    agg_cfg = countries_cfg.get("aggregate_output_directory", "results/emissions/All_countries")
    agg_root = Path(agg_cfg)
    if not agg_root.is_absolute():
        agg_root = (ROOT / agg_root).resolve()
    agg_root = apply_results_run_directory(agg_root, run_directory, repo_root=ROOT) / mix

    per_country_cfg = countries_cfg.get("resources_root", "results/emissions")
    per_country_root = Path(per_country_cfg)
    if not per_country_root.is_absolute():
        per_country_root = (ROOT / per_country_cfg).resolve()
    per_country_root = (
        apply_results_run_directory(per_country_root, run_directory, repo_root=ROOT) / mix
    )

    return agg_root, per_country_root


def _resolve_air_pollution_root(
    config: Mapping[str, object], results_root_override: Path | None = None
) -> Path:
    """Return the air pollution root with run directory applied or overridden."""
    if results_root_override is not None:
        return (results_root_override / "air_pollution").resolve()

    run_directory = get_results_run_directory(config)
    ap_cfg = config.get("air_pollution", {}) or {}
    ap_root = Path(ap_cfg.get("output_directory", "results/air_pollution"))
    if not ap_root.is_absolute():
        ap_root = (ROOT / ap_root).resolve()
    return apply_results_run_directory(ap_root, run_directory, repo_root=ROOT)


def _load_pm25_deltas(csv_path: Path) -> pd.DataFrame:
    """Load PM2.5 deltas, ensuring required columns are present."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing PM2.5 file: {csv_path}")
    df = pd.read_csv(csv_path)
    required = {"year", "country", "delta_concentration_micro_g_per_m3"}
    missing = required.difference(df.columns)
    if missing:
        raise KeyError(f"Missing columns in {csv_path}: {sorted(missing)}")
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df.dropna(subset=["year"])
    df["year"] = df["year"].astype(int)
    return df[["year", "country", "delta_concentration_micro_g_per_m3"]]


def _collect_pm25_by_mix(
    mixes: Sequence[str],
    air_pollution_root: Path,
    scenario: str,
) -> dict[str, pd.DataFrame]:
    """Load pm25_concentration_summary.csv for each mix under the provided scenario."""
    collected: dict[str, pd.DataFrame] = {}
    for mix in mixes:
        csv_path = air_pollution_root / f"{mix}__{scenario}" / "pm25_concentration_summary.csv"
        try:
            collected[mix] = _load_pm25_deltas(csv_path)
        except Exception as exc:
            LOGGER.warning("Skipping PM2.5 for %s: %s", mix, exc)
    return collected


def _plot_pm25_delta_tiles(
    pm25_data: Mapping[str, pd.DataFrame],
    output_dir: Path,
    scenario: str,
) -> None:
    """Plot per-country tiles of PM2.5 delta concentrations across mixes."""
    if not pm25_data:
        LOGGER.warning("No PM2.5 data loaded; skipping tiled plot.")
        return

    countries: set[str] = set()
    for df in pm25_data.values():
        countries.update(df["country"].dropna().unique().tolist())
    if not countries:
        LOGGER.warning("No countries found in PM2.5 data; skipping tiled plot.")
        return

    sorted_countries = sorted(countries)
    ncols = 3
    nrows = ceil(len(sorted_countries) / ncols)
    fig, axes = plt.subplots(
        nrows=nrows,
        ncols=ncols,
        figsize=(4.5 * ncols, 3.0 * nrows),
        sharex=True,
        sharey=False,
    )
    axes_list = axes.flatten() if hasattr(axes, "flatten") else [axes]

    color_map = {
        "base_mix": "#2a6fdb",
        "WAM": "#db8b2a",
        "WEM": "#2aa05a",
    }
    handles = []
    labels = []

    for idx, country in enumerate(sorted_countries):
        ax = axes_list[idx]
        for mix, df in pm25_data.items():
            subset = df[df["country"] == country].sort_values("year")
            if subset.empty:
                continue
            color = color_map.get(mix)
            line = ax.plot(
                subset["year"],
                subset["delta_concentration_micro_g_per_m3"],
                label=mix,
                linewidth=1.6,
                color=color,
            )[0]
            if mix not in labels:
                handles.append(line)
                labels.append(mix)
        ax.axhline(0.0, color="#aaaaaa", linewidth=0.8, linestyle="--", alpha=0.8)
        ax.set_title(country)
        ax.grid(True, linestyle="--", alpha=0.3)
    for extra_ax in axes_list[len(sorted_countries) :]:
        extra_ax.axis("off")

    fig.text(0.5, 0.04, "Year", ha="center")
    fig.text(0.04, 0.5, "Δ PM2.5 concentration (µg/m³)", va="center", rotation="vertical")
    if handles:
        fig.legend(handles, labels, loc="upper center", ncol=len(labels), frameon=False)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    output_dir.mkdir(parents=True, exist_ok=True)
    fname = f"pm25_delta_concentration_tiles_{scenario}.png"
    fig.savefig(output_dir / fname, format="png")
    plt.close(fig)


def _load_deltas(co2_path: Path, demand_cases: Iterable[str], baseline_case: str) -> pd.DataFrame:
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


def _plot_total_saved(
    agg_root: Path,
    demand_cases: Iterable[str],
    baseline_case: str,
    years: list[int],
    output_dir: Path,
    mix: str,
    cumulative_years: list[int] | None = None,
) -> None:
    mean_case = "scen1_mean"
    lower_case = "scen1_lower"
    upper_case = "scen1_upper"
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
    fig.savefig(output_dir / f"{mix}_total_co2_saved.png", format="png")
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
        fig.savefig(output_dir / f"{mix}_total_co2_saved_cumulative.png", format="png")
        plt.close(fig)


def _plot_country_saved(
    per_country_root: Path,
    demand_cases: Iterable[str],
    baseline_case: str,
    years: list[int],
    output_dir: Path,
    mix: str,
    cumulative_years: list[int] | None = None,
) -> None:
    mean_case = "scen1_mean"
    lower_case = "scen1_lower"
    upper_case = "scen1_upper"
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
        ax.set_title(f"Annual CO₂ Saved by Country — {year}")
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
) -> None:
    """One plot per country comparing the specified years (e.g., 2030 vs 2050)."""
    mean_case = "scen1_mean"
    lower_case = "scen1_lower"
    upper_case = "scen1_upper"
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
        ax.set_title(f"CO₂ Saved — {cdir.name}")
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        output_dir.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fname = f"{cdir.name}_{mix}_co2_saved.png"
        fig.savefig(output_dir / fname, format="png")
        plt.close(fig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Generate additional emission-savings plots.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Path to config.yaml")
    parser.add_argument(
        "--results-root",
        default=None,
        help="Override results root to select a specific run (e.g., results/global).",
    )
    parser.add_argument(
        "--mix",
        nargs="*",
        default=None,
        help=(
            "Mix scenario(s) to plot (default: calc_emissions.countries.mix_scenarios "
            "or baseline mix)."
        ),
    )
    parser.add_argument(
        "--years",
        nargs="*",
        type=int,
        default=[2030, 2050],
        help="Years to plot (default: 2030 2050).",
    )
    args = parser.parse_args()

    config_path = get_config_path(Path(args.config))
    config = _load_config(config_path)

    calc_cfg = config.get("calc_emissions", {}) or {}
    countries_cfg = calc_cfg.get("countries", {}) or {}
    demand_cases = countries_cfg.get("demand_scenarios", []) or []
    if not demand_cases:
        raise ValueError("calc_emissions.countries.demand_scenarios must be configured.")
    baseline_case = (
        str(countries_cfg.get("baseline_demand_case", BASE_DEMAND_CASE)).strip() or BASE_DEMAND_CASE
    )
    mix_candidates: Sequence[str] = (
        args.mix
        if args.mix
        else countries_cfg.get("mix_scenarios")
        or calc_cfg.get("mix_scenarios")
        or [countries_cfg.get("baseline_mix_case") or BASE_MIX_CASE]
    )
    mixes = [str(m).strip() for m in mix_candidates if str(m).strip()]
    if not mixes:
        mixes = [BASE_MIX_CASE]

    results_root_override = Path(args.results_root).resolve() if args.results_root else None
    base_output_dir = (
        (results_root_override / "summary" / "additional_plots").resolve()
        if results_root_override
        else apply_results_run_directory(
            (ROOT / "results" / "summary" / "additional_plots").resolve(),
            get_results_run_directory(config),
            repo_root=ROOT,
        )
    )
    air_pollution_root = _resolve_air_pollution_root(config, results_root_override)
    pm25_output_dir = base_output_dir / "air_pollution"
    LOGGER.info("Using air pollution path: %s", air_pollution_root)
    LOGGER.info("PM2.5 plots will be written to: %s", pm25_output_dir)

    years = sorted({int(y) for y in args.years})

    for mix in mixes:
        agg_root, per_country_root = _resolve_paths(config, mix, results_root_override)
        output_dir = base_output_dir / mix
        output_dir.mkdir(parents=True, exist_ok=True)

        LOGGER.info("Using aggregate path: %s", agg_root)
        LOGGER.info("Using per-country path: %s", per_country_root)
        LOGGER.info("Writing plots to: %s", output_dir)

        _plot_total_saved(
            agg_root,
            demand_cases,
            baseline_case,
            years,
            output_dir,
            mix,
            cumulative_years=[2030, 2050],
        )
        _plot_country_saved(
            per_country_root,
            demand_cases,
            baseline_case,
            years,
            output_dir,
            mix,
            cumulative_years=[2030, 2050],
        )
        _plot_country_pairwise(
            per_country_root, demand_cases, baseline_case, years, output_dir / "per_country", mix
        )

    pm25_data = _collect_pm25_by_mix(mixes, air_pollution_root, PM25_SCENARIO)
    _plot_pm25_delta_tiles(pm25_data, pm25_output_dir, PM25_SCENARIO)


if __name__ == "__main__":
    main()
