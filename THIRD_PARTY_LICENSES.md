# Third-party licenses

MinerTune itself is licensed under the MIT License (see `LICENSE`). It uses the
following third-party software. MinerTune does **not** bundle or vendor any of it:
the packages are installed from PyPI into a local virtual environment by `install.sh`.

| Component | Used for | License | Notes |
|---|---|---|---|
| Python standard library (CPython) | HTTP server, JSON, networking, threading, … | PSF License Agreement (Python Software Foundation License) | Permissive, GPL-compatible. The whole control center and sweep engine run on the stdlib alone. |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | XLSX export | MIT | Permissive. |
| [et_xmlfile](https://foss.heptapod.net/openpyxl/et_xmlfile) | dependency of openpyxl | MIT | Permissive. |
| [reportlab](https://www.reportlab.com/opensource/) | PDF export | BSD (3-clause style, ReportLab open-source license) | Permissive; keep the copyright notice. |
| [Pillow](https://python-pillow.org/) | dependency of reportlab | MIT-CMU (HPND) | Permissive, MIT-style. |
| [charset-normalizer](https://github.com/jawah/charset_normalizer) | dependency of reportlab | MIT | Permissive. |

## Compatibility

All licenses listed above are permissive and **compatible with the MIT License**
of MinerTune: they allow use, modification and redistribution (also commercially)
as long as the respective copyright and license notices are preserved. None of them
is copyleft, so they impose no license requirements on MinerTune's own code.

The full license texts are shipped with each package in the virtual environment,
e.g. `/opt/minertune/venv/lib/python3.*/site-packages/<package>-*.dist-info/`.

## Trademarks

"Harlo", "Harlo-OS" and "Bitfortun" are names/trademarks of their respective owners.
MinerTune is an independent project, not affiliated with or endorsed by them; the
names are used only to describe compatibility.
