# Acting Lab WordPress Bridge

Repository di collegamento tra ChatGPT/GitHub e il sito WordPress Elementor di Acting Lab.

## Architettura

1. ChatGPT inserisce un comando JSON in `queue/pending/`.
2. GitHub Actions esegue `scripts/process_queue.py`.
3. Lo script chiama il plugin WordPress `Acting Lab Bridge` tramite REST API autenticata con Application Password.
4. Il plugin legge o modifica in modo mirato i testi dei widget Elementor.
5. Il comando viene spostato in `queue/processed/` oppure `queue/failed/` e la risposta viene salvata in `queue/results/`.

## Installazione WordPress

Installa e attiva il plugin contenuto nella cartella `acting-lab-bridge`.

Il plugin aggiunge questi endpoint autenticati:

- `GET /wp-json/acting-lab/v1/status`
- `GET /wp-json/acting-lab/v1/pages`
- `GET /wp-json/acting-lab/v1/pages/{id}`
- `POST /wp-json/acting-lab/v1/pages/{id}/replace-text`

Le operazioni richiedono un utente WordPress con permesso di modifica della pagina. È consigliato un utente dedicato e una WordPress Application Password dedicata.

## GitHub Secrets

In **Settings → Secrets and variables → Actions** configura:

- `WP_URL` — URL base del sito, es. `https://www.example.it`
- `WP_USERNAME` — username WordPress dedicato
- `WP_APP_PASSWORD` — Application Password WordPress

Non salvare mai password o token nei file del repository.

## Sicurezza delle modifiche Elementor

La lettura restituisce per ogni campo testuale:

- `widget_id`
- tipo di widget
- `path` esatto dentro le impostazioni del widget
- valore corrente

La sostituzione richiede esplicitamente `widget_id`, `path` e `old_text`. Se il testo compare più volte nello stesso campo, l'operazione viene bloccata a meno che sia stato richiesto `replace_all=true`. È disponibile `dry_run=true` per verificare una modifica senza salvarla.

## Esempio di flusso

Prima si legge la pagina con `read_page`. Dal risultato si individua il widget e il percorso del testo. Poi si invia `replace_text`, preferibilmente una prima volta con `dry_run=true` e successivamente con `dry_run=false`.
