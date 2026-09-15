# CVS six-mile national coverage

This analysis measures the national footprint created by a six-statute-mile radius around every CVS location in a dated U.S. store snapshot.

## National result

- CVS locations: 8,981
- Population inside the merged six-mile footprint: 267,863,423 (80.8158%)
- Population outside the footprint: 63,585,858 (19.1842%)
- U.S. land inside the footprint: 300,970.52 square miles (8.3448%)
- U.S. land outside the footprint: 3,305,707.36 square miles (91.6552%)
- Gross circle area before overlap and clipping: 1,015,727.17 square miles

## Coverage definition

- Store snapshot: SimpleMaps CVS database, dated July 29, 2026, licensed CC BY 4.0.
- Six statute miles (9,656.064 meters) measured geodesically from each supplied store coordinate.
- Each geodesic circle uses 72 vertices.
- Overlapping service areas are dissolved and counted once.
- Land coverage uses 2024 U.S. Census state boundaries for the 50 states and District of Columbia.
- Population coverage uses official 2020 Census block population (POP20) assigned at each block's Census internal point.

## Accuracy and use

The free store snapshot rounds latitude and longitude to three decimal places. That creates a center-point uncertainty of roughly 0.07 mile or less in most of the United States. The six-mile geometry is exact relative to those supplied coordinates, but it is not a surveyed rooftop boundary.

This is a geographic opportunity footprint. It does not account for FAA airspace, terrain, obstacles, weather, route geometry, BVLOS authority, launch-site suitability, rooftop access, or local operating constraints.

## Generated files

- `output/cvs-six-mile-map.html` — interactive national map
- `output/coverage-summary.json` — national result and audit metadata
- `output/state-coverage.csv` — state covered and uncovered totals
- `output/cvs-locations.csv` — dated store snapshot used by the build
- `output/unmatched-locations.csv` — any locations that could not be geocoded
