# Death Fist — Discord Security Bot & Tournament Platform

Bot Discord per un server dedicato a Tekken 8: modera lo spam e gestisce
tornei veri e propri appoggiandosi all'API di [Challonge](https://challonge.com),
con una dashboard web di amministrazione separata.

## Moderazione anti-spam

Chi manda **3 messaggi identici in 10 secondi** viene automaticamente messo in
timeout per 10 minuti ("PHOENIX SMASHER!"). L'azione viene loggata in un canale
dedicato con un embed che riporta l'utente, i messaggi incriminati e un
pulsante per annullare il timeout in caso di falso positivo.

## Sistema tornei

Comandi slash per i giocatori:

| Comando | Cosa fa |
|---|---|
| `/iscriviti <personaggio> [nome_tekken]` | Iscrive al torneo attivo (se le iscrizioni sono aperte) |
| `/report <tuo_score> <avversario_score>` | Riporta il risultato del proprio match |
| `/tabellone` | Link al tabellone pubblico del torneo |
| `/help` | Elenco comandi e regole anti-spam |

Il bot mantiene inoltre un **roster** sempre aggiornato dei partecipanti (un
messaggio per membro, con personaggio scelto) in un canale dedicato.

Personaggi e ruoli Discord restano allineati nei due sensi:

- per ogni membro valgono i personaggi assegnati dalla dashboard; chi non ne
  ha compare con i personaggi dei suoi **ruoli Discord** (ruoli con lo stesso
  nome del personaggio, oppure il solo primo nome, es. `Anna` per `Anna Williams`);
- se la dashboard assegna un personaggio di cui il membro non ha il ruolo, il
  bot glielo aggiunge (i ruoli non vengono mai tolti).

Per assegnare i ruoli il bot deve avere il permesso **Gestisci ruoli** e il suo
ruolo deve stare sopra quelli dei personaggi.

## Dashboard admin (web)

Applicazione FastAPI separata (`web_app.py`), protetta da login Discord OAuth2,
riservata agli ID configurati come admin. Permette di:

- creare tornei (singola/doppia eliminazione, formato FT2/FT3) e aprirli alle iscrizioni;
- gestire partecipanti, seed e personaggi;
- avviare il torneo e seguirne lo stato;
- consultare bracket e storico pubblici (`/torneo/{id}`, `/storico`).

Tabellone e storico sono anche visibili senza login, come pagine pubbliche.

## Setup

Bot e webapp condividono lo stesso database SQLite e vanno avviati separatamente.

```bash
pip install -r requirements.txt
cp .env.example .env   # token Discord, credenziali Challonge OAuth2, SECRET_KEY, admin ID
python bot.py          # bot Discord
uvicorn web_app:app     # dashboard admin (in un altro processo)
```

Le variabili richieste sono documentate in `.env.example`: token del bot,
ID dei canali log/roster, credenziali OAuth2 di Challonge e di Discord (per il
login della dashboard), una `SECRET_KEY` per le sessioni e la lista degli ID
Discord con permessi di amministrazione.

## Stack

`discord.py`, FastAPI, SQLite, [Challonge API](https://api.challonge.com/v2.1) (OAuth2).
