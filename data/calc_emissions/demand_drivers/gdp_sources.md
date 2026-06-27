# GDP driver sources

`gdp.csv` contains SSP1–SSP5 total GDP paths for Albania (ALB), Bosnia and Herzegovina (BIH), Kosovo (KOS), North Macedonia (MKD), Montenegro (MNE), and Serbia (SRB).

## Source workbook

- `data/GDP_and_Population_data/IIASA/SSP_Scenario_Explorer_GDP.xlsx`, sheet `data`.
- Source attribution in the workbook README: SSP Scenario Explorer data hosted by IIASA.
- Variable: `GDP|PPP`.
- Workbook unit: `billion USD_2017/yr`; driver unit label: `billion USD_2017 PPP`.

## Processing notes

- Albania, Bosnia and Herzegovina, North Macedonia, Montenegro, and Serbia are copied from country rows in the IIASA workbook for SSP1–SSP5.
- The 2023 value is linearly interpolated between the workbook's 2020 and 2025 columns so the dynamic demand model can use the 2023 electricity-demand base year.
- Kosovo is not included as a separate country row in the workbook. Kosovo paths are synthesized by applying Serbia's SSP-specific GDP growth index to a Kosovo 2023 GDP base of 27.40 billion USD_2017 PPP.
