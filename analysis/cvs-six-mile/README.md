# CVS six-mile national coverage

This analysis builds a dated national map from the live CVS store directory.

## Coverage definition

- Every CVS Pharmacy listed in the live CVS directory on the build date.
- Six statute miles (9,656.064 meters) measured geodesically from each store.
- Overlapping service areas are dissolved and counted once.
- Land coverage uses U.S. Census state boundaries for the 50 states and District of Columbia.
- Population coverage uses 2020 Census block populations assigned at each block's Census internal point. This is the finest reproducible national public-data method used by this build.

## Generated files

- `output/cvs-six-mile-map.html` — interactive map
- `output/coverage-summary.json` — national result and audit metadata
- `output/state-coverage.csv` — state totals
- `output/cvs-locations.csv` — captured store snapshot
- `output/unmatched-locations.csv` — any locations that could not be geocoded

The workflow refuses to label a run complete if fewer than 99% of captured stores have valid coordinates.