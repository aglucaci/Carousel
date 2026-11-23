# Starter Radars

Starter radar configurations for CAROUSEL. Each radar defines a topic, sources,
query set, signal profile, ranking weights, and outputs. The schema is defined
and validated by `carousel/config.py`.

```bash
pip install -e .
carousel validate examples/*.yml          # check every template
carousel show examples/viral_evolution.yml  # print the resolved config, defaults included
```

`radar_id` defaults to the file name (`viral_evolution.yml` becomes
`viral-evolution`). Unknown fields, unknown sources, duplicate keys, and
duplicate terms are rejected with line numbers.

## Included Templates

| Template | File |
| --- | --- |
| Cancer Evolution Radar | `cancer_evolution.yml` |
| Viral Evolution Radar | `viral_evolution.yml` |
| Metagenomics Radar | `metagenomics.yml` |
| Urban Microbiome Radar | `urban_microbiome.yml` |
| AI Drug Discovery Radar | `ai_drug_discovery.yml` |
| Protein Design Radar | `protein_design.yml` |
| Wastewater Surveillance Radar | `wastewater_surveillance.yml` |
| Single-Cell Genomics Radar | `single_cell_genomics.yml` |

## Planned Commands

These arrive with the connector and export work (see `CAROUSEL_PLAN.md`).

```bash
carousel build examples/cancer_evolution.yml
carousel clone examples/cancer_evolution.yml my_leukemia_radar.yml
carousel export results/cancer_evolution/ --format csv
carousel export results/cancer_evolution/ --format bibtex
```
