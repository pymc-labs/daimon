# Vendored pdf.js

The report viewer renders PDFs with [pdf.js](https://github.com/mozilla/pdf.js)
4.10.38 (Apache-2.0, see `pdfjs-LICENSE.txt`), served from the report host itself
so a reader's browser makes no request to a third party.

The two files are the unmodified `build/pdf.min.mjs` and `build/pdf.worker.min.mjs`
from the `pdfjs-dist@4.10.38` npm package:

| File | SHA-256 |
| --- | --- |
| `pdf.min.mjs` | `27fc2a057a00f92a4334ad06e17dbd7259912954e9fb7f76400bcca5fd190a9c` |
| `pdf.worker.min.mjs` | `1baa1844c89c80a5b2797c916e75ab29254be46d8e9cb53cb6364d7aad84be36` |

To upgrade, replace both files and the licence from the new package version and
update the hashes and `tests/test_viewer_vendor.py`.
