# MinerTune – Translation Glossary / Terminology Matrix

This glossary defines how key terms are handled across translations, so that every
language file stays **consistent** and technically correct. Community translators:
please follow this table. When in doubt, keep the established English mining term.

## Guiding principle

MinerTune is a technical tool for the Bitcoin-mining community. That community uses
English technical vocabulary almost universally, regardless of spoken language. A
Spanish or Polish miner says "hashrate", not a translated variant. Therefore:

- **Keep in English:** unit-like and domain-standard terms the community uses as-is.
- **Translate:** general UI vocabulary (buttons, navigation, status words, help text).
- **Hybrid:** translate the surrounding sentence, keep the technical token.

## Terms KEPT IN ENGLISH in all languages (do not translate)

| Term | Reason |
|---|---|
| `hashrate` | universal mining term |
| `J/TH`, `TH/s`, `GH/s`, `mV`, `MHz`, `W`, `V`, `°C`, `%` | units – never translate |
| `core_mv` | literal API/field name shown in UI |
| `Vmin` | established term for minimum stable voltage |
| `stock` | mining term for factory default clocks/voltage |
| `sweep` | the core operation; widely used untranslated. May be paired: "Sweep (barrido)" on first use if a language strongly prefers, but keep `sweep` as the noun |
| `precheck` | our named 30 s stage; keep as coined term |
| `ASIC`, `VR` | hardware abbreviations |
| `MinerTune`, `MIT License`, `GitHub`, `NFDiJee`, `Harlo-OS`, `AxeOS` | names/brands |
| `save=false`, `frequency_max`, `core_max`, `/status`, `config.py` etc. | literal code/API tokens |
| `PIN` | internationally understood |

## Terms that ARE translated

| English | Concept | Notes |
|---|---|---|
| efficiency | Effizienz / eficiencia / efficacité … | general word |
| performance | translate, though many keep "performance" (esp. FR/IT) |
| knee ("performance knee") | the ΔW/ΔTH inflection point | translate descriptively; may keep "knee" if no good idiom |
| reserve | the Vmin+1 safety step | translate |
| connection, history, live, log | UI navigation | translate |
| waiting / active / done / rejected / skipped | status words | translate |
| warmup | may translate or keep; prefer translate ("Aufwärmen", "calentamiento") |
| measuring window | translate "window" as the measurement period |
| underpowered | translate ("unterversorgt", "sin suficiente tensión / con tensión insuficiente") |
| target hashrate | translate "target", keep "hashrate" → "Ziel-Hashrate", "hashrate objetivo" |
| wall power | translate ("Wandleistung", "potencia de pared/consumo") |
| emergency stop | translate – safety critical, must be instantly clear |

## Decimal separator (`_decimal` key)

Set per language convention:
- `.` → English (en)
- `,` → German, Spanish, French, Italian, Portuguese, Dutch, Polish (de, es, fr, it, pt, nl, pl)

## Placeholders — DO NOT translate or move

Every `{name}` placeholder (e.g. `{pct}`, `{mv}`, `{f}`, `{t}`, `{sensor}`) must appear
**unchanged** in the translation, and every placeholder present in the English value must
be present in the translation. HTML tags like `<b>…</b>` are kept as-is.
`\n` newlines in confirm.* strings are kept.

## Status marker

Each language file carries `"_status"`:
- `"reviewed"` – translated with care by a fluent/near-native level (es, fr, it, pt, nl)
- `"machine-draft"` – usable draft, should be reviewed by a native speaker (e.g. pl and others)
