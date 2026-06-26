# Synthetic Fixtures

Fixtures in this directory must be synthetic, anonymized, and safe to commit.
They may use placeholder IDs, fictional names, invented postcodes/areas, and
small row counts that support tests or documentation examples.

`standardized_csv_v1/` is the canonical synthetic example set for the executable
CSV import contract documented in `docs/NORMALIZED_DATA_CONTRACT.md`.

Do not put raw tenant workbooks, exported emails, real alias maps, real names,
addresses, phone numbers, emails, or operational notes here. Keep those files in
ignored local storage such as `RecommenderSystem/local-data/` or managed tenant
storage.
