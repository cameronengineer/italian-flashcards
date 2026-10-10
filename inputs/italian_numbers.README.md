# Numbers: difficult patterns first

`italian_numbers.csv` is the active source for the `numbers` list. Its two
columns keep the existing format:

```csv
english,italian
11 / eleven,undici
12 / twelve,dodici
```

Rows set the order within this list. `lists.toml` keeps `kind = "numbers"` and
points to this file: numbers retain their numeric identities, numeric prompts
and `num` classification, and do not need Claude enrichment. The pipeline's
overall list priorities, shared roots and Anki's existing review scheduling
still apply.

## Study order

The ordering is tailored to the reported difficulty with 11–15 and twenty versus
thirty. The later contrasts are deliberate practice choices for an English
speaker, rather than a measured ranking of difficulty for all learners.

| Data rows, excluding the header | Focus |
|---|---|
| 1–12 | 11–15 first, then 16–19, 10, and 20 immediately beside 30 |
| 13–30 | Matched twenties/thirties: 21/31, 22/32, through 29/39 |
| 31–54 | Other tens, especially 60/70; endings in 1, 8 and 3 across those tens |
| 55–101 | Familiar 0–9 and the remaining numbers through 100 |
| 102–136 | Teens versus tens inside hundreds: 113/130, 114/140, 115/150, through 119/190; then 121/131 through 129/139 |
| 137–163 | Hundred boundaries and spelling patterns: 101, 103, 108, 181, 188, 208, 218, 280 and round hundreds |
| 164–202 | Thousands and place value: 1,001/1,010/1,100, teens inside thousands, and 20,000/30,000 |
| 203–208 | Hundreds of thousands and millions |
| 209–251 | Remaining numbers from the previous active generated list, retained for later practice |

Every number appears once. Duplicating a row would not produce useful spaced
practice in this word-first pipeline; Anki handles repetition. All 178 numbers
from the previous active generated list are included, with 73 additional values
chosen to reinforce the contrasts. The old, previously unused CSV had 400 rows.

## Patterns to notice

- **The teens change shape:** 11–16 end in `-dici`; 17–19 begin with `dici-`.
  Avoid imposing English's regular “-teen” shape on every Italian teen.
- **Twenty and thirty:** compare `venti` with `trenta`, then keep the same unit
  while switching the ten: `ventidue` / `trentadue`.
- **Sixty and seventy:** compare `sessanta` / `settanta` and their compounds.
- **Before one or eight:** the tens lose their final vowel: `ventuno`,
  `ventotto`, `trentuno`, `trentotto`.
- **Final three:** standalone `tre` is unaccented; compounds such as `ventitré`,
  `trentatré` and `centoventitré` carry an acute accent.
- **Hundreds and thousands:** practise the full Italian word without inserting
  English's “and” or spaces into compounds such as `centoventitré`. Contrast
  singular `mille` with plural compounds such as `duemila`.

These spelling patterns were checked against
[Treccani's guide to numerals](https://www.treccani.it/enciclopedia/numerali-prontuario_%28Enciclopedia-dell%27Italiano%29/).
For 108 and related compounds, the source retains the application's existing
`centotto` spelling; `centootto` is also valid. See
[Treccani on centotto and centootto](https://www.treccani.it/magazine/lingua_italiana/domande_e_risposte/grammatica/grammatica_026.html).

## Editing and applying the list

Keep the English cell in `digits / English words` form, with ungrouped digits
(for example `1000 / one thousand`). The number reader checks for duplicate
values and requires the application's canonical Italian spelling to keep number
identities consistent; that convention is not a claim that other documented
Italian spellings are incorrect. The Italian column is the displayed word.

The next `./run.sh` detects this source change and imports the new order. This
edit does not reset reviewed cards, delete media or generate replacement media.
