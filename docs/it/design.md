# CUORE — design (valori e regole in una pagina)

Fonte dei valori: `app/index.html`, blocco `:root` (se cambiano là, aggiornare qui). Stile «strumento pro»: superfici scure, un colore per area, cifre tabellari.

## Principi
- Si migliora, non si rifà. Per ogni cambio grafico: prima/dopo + alternative, si sceglie pezzo per pezzo.
- Una dashboard sola per telefono, iPad e computer. Intestazione pulita: una riga di stato e il titolo, nient'altro.
- Tutto apribile nel dettaglio; modificabile dove ha senso. Solo variabili `--*`: niente colori scritti a mano.
- Tema scuro di base, chiaro automatico (`data-theme="auto"`). Ogni colore nuovo va definito in entrambi.

## Colori (scuro → chiaro)
| Uso | Variabile | Scuro | Chiaro |
|---|---|---|---|
| Sfondo / riquadro / riquadro 2 | `--bg` `--surface` `--surface-2` | #0b0c0f · #121317 · #17191e | #f6f6f8 · #fff · #f1f1f4 |
| Linee | `--line` `--line-2` | #24262d · #2e3139 | #e3e3e8 · #d5d5dc |
| Testo / secondario / tenue | `--text` `--muted` `--faint` | #e8e9ec · #9a9ca5 · #6b6e78 | #16171b · #5d606a · #8b8e98 |
| School | `--school` | #9d8cff | #6a55e8 |
| Finance | `--money` | #3ecf8e | #12935f |
| Sistema / Home | `--system` | #b4b7c0 | #4b4f5a |
| Stato buono / attenzione / errore | `--good` `--warn` `--bad` | #3ecf8e · #e9a23b · #f0616d | #12935f · #b8740f · #d23b48 |
| Serie dei grafici | `--c2`…`--c8` | #e9a23b #9d8cff #5ab0f0 #e57fb0 #7cc36a #e38a5c #8b8e98 | versioni più scure |

Ogni colore ha la sua variante `-soft` (sfondo tenue). L'accento cambia con l'area (`data-app`): school viola, money verde, home grigio.

## Forma e testo
- Raggio `--r` 10px. Font di sistema (`--font`: SF Pro/Inter), numeri e codici in `--mono`.
- Significato fisso: verifica = rosso, compito = giallo, aggiornato = verde, da controllare = giallo, non disponibile = rosso.
- Mobile: seguire la skill `mobile-native` (area sicura, nessuno zoom sugli input, tocchi senza lag).

## Prima di consegnare una modifica grafica
Provarla in chiaro e scuro, a larghezza telefono, con dati estremi (skill `break-ui`), e aggiornare la versione della cache della PWA se cambiano i file serviti.
