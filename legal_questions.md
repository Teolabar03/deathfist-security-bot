# Domande per Privacy Policy & Termini di Servizio

Rispondi sotto ogni domanda (dove vedi `> `). Dove ho già dedotto qualcosa
guardando il codice/i repo, l'ho scritto come proposta: basta confermare,
correggere o completare. Una volta risposto a tutto, uso queste risposte
per scrivere le pagine vere e proprie (Privacy Policy + Termini di Servizio
per ciascun bot), pubblicate come pagine nascoste della web app
`deathfist-security-bot` (non linkate nel menu, raggiungibili solo via URL
diretto).

---

## Sezione A — Domande comuni a entrambi i bot

**A1. Titolare/contatto per richieste privacy**
Che email o contatto Discord vuoi mostrare per richieste privacy, cancellazione
dati, domande legali? (es. la tua email, un server Discord di supporto, un
tag Discord personale) Usr Discord: teolabar03

> 

**A2. Età minima**
Vuoi impostare un'età minima per l'uso dei bot? Discord stesso richiede 13
anni: va bene usare quella soglia, o ne vuoi una diversa? Va bene quella di discord

> 

**A3. Legge applicabile / giurisdizione**
In che paese risiedi/operi? (serve per la clausola "legge applicabile" nei
Termini di Servizio — es. Italia) Io risiedo e opero in italia

> 

**A4. Modifiche alle policy**
Come preferisci notificare eventuali modifiche future a queste pagine? (es.
"data di ultimo aggiornamento in fondo alla pagina", annuncio nel server
Discord, nessuna notifica particolare) Nessuna notifica particolare

> 

**A5. Dove verranno pubblicate le pagine**
Confermi che entrambe le coppie di pagine (Privacy + ToS per Tekken 8 Frame
Bot, Privacy + ToS per Deathfist) vivranno come route nascoste sulla web app
`deathfist-security-bot` già esistente? Hai già un dominio pubblico collegato
a questa web app (es. Railway/Render/dominio personale) o gira ancora in
locale? gireranno sulla macchina dove girano i bot che verrà esposta tramite le webapp con l'ip: nessun dominio verrà collegato.

> 

---

## Sezione B — Tekken 8 Frame Bot (basato su TLNBS2405/heihachi)

Dal repo GitHub ho verificato: licenza **MIT**, il bot fornisce dati sui
frame di Tekken 8 attingendo da **Wavu Wiki**, tramite comandi slash. La
licenza MIT permette l'uso come base per un bot proprio, a patto di
mantenere l'attribuzione all'autore originale (TLNBS2405).

**B1. Dati raccolti**
Dal repo non risulta che il bot salvi dati personali persistenti (sembra solo
rispondere a comandi/lookup in tempo reale). Puoi confermare che **non**
raccoglie né salva dati degli utenti (es. nessun database, nessun log
permanente di chi usa i comandi)? Se invece hai aggiunto qualcosa di tuo
(logging, analytics, salvataggio preferenze), descrivilo qui. Confermo che non tiene nulla

> 

**B2. Hosting**
Dove gira il bot (es. VPS tuo, Railway, Replit, PC locale)? Serve solo per
sapere se ci sono provider terzi da menzionare nella privacy policy. Sulla VPS dove stai operando

> 

**B3. Server Discord di supporto / invito**
Hai un server Discord di supporto o un link di invito pubblico per questo
bot da linkare nelle pagine? No

> 

**B4. Attribuzione**
Vuoi che scriva esplicitamente "basato su heihachi di TLNBS2405 (licenza
MIT)" e un credito a Wavu Wiki come fonte dei dati? Confermi che non c'è
nessuna collaborazione/affiliazione ufficiale con l'autore originale o con
Wavu Wiki (solo uso dei dati/codice pubblici)? Non ho nessuna affiliazione o collaborazione e voglio esplicitare i dati e il link della repo https://github.com/TLNBS2405/heihachi

> 

**B5. Differenze rispetto all'originale**
Hai modificato o esteso qualcosa rispetto al repo originale (nuovi comandi,
dati aggiuntivi, ecc.) che vada menzionato? Nessuna modifica per ora

> 

---

## Sezione C — Deathfist

Dal codice (`bot.py`, `web_app.py`, `database.py`) ho già individuato questi
dati trattati — confermami se è tutto corretto e completo:

- **Discord ID e username** di chi entra nel server (tabella `members`,
  sincronizzata automaticamente su join/leave/update).
- **Dati di iscrizione ai tornei**: ID Discord, nome Discord, ID Challonge,
  ID in-game Tekken opzionale, personaggio principale scelto (tabella
  `participants`).
- **Ruoli/preferenze roster**: alias, flag "pro", personaggi assegnati
  (tabelle `members`, `character_assignments`).
- **Contenuto dei messaggi**: letto in tempo reale per rilevare spam
  (3 messaggi identici in 10 secondi → timeout automatico 10 min), tenuto
  solo **in memoria** (non salvato su database) e cancellato subito dopo
  l'analisi.
- **Login amministratori**: OAuth2 Discord, sessione via cookie firmato,
  scade dopo 24h, ristretta a una lista di ID Discord admin.
- **Condivisione con terzi**: dati dei partecipanti inviati a **Challonge**
  (per gestire tornei/bracket) e a **Discord** (per OAuth login, letture
  profilo).

**C1. Conferma dati**
La lista sopra ti sembra completa e corretta? C'è altro che il bot
raccoglie/usa e che non ho individuato dal codice? Questo è tutto

> 

**C2. Conservazione dati**
Per quanto tempo tieni i dati dei tornei passati (`participants`,
`tournaments`) nel database? Li cancelli mai manualmente, o restano per
sempre finché non li rimuovi tu a mano? I dati sono legati all'utenza su discord, vengono mantenuti fintanto che l'utente rimane nel server discord associato.

> 

**C3. Richiesta di cancellazione dati**
Se un utente ti chiede di cancellare i suoi dati (es. dal torneo o dal
roster), come intendi gestirlo? (es. "scrivimi su Discord e lo faccio a
mano" — dato che al momento non esiste una funzione automatica di export o
cancellazione dati nel codice) Vengono cancellati manualmente quelli gestiti lato mio, mentre quelli di challonge non ci posso fare nulla

> 

**C4. Moderazione automatica**
Confermi che vuoi un disclaimer esplicito sul sistema anti-spam automatico
(timeout/kick/ban automatici gestiti anche da altri moderatori tramite i
pulsanti nel canale di log), inclusa la possibilità di errori (falsi
positivi) e chi contattare per contestare un'azione? La moderazione automatica non ha possibilità di reclami poiché attiva solo dei timeout che vengono poi gestiti manualmente da un operatore staff del server

> 

**C5. Minori/ruoli speciali**
Il flag "pro" e il roster dei personaggi: sono dati sensibili da trattare
con attenzione particolare, o sono solo dati di gioco/preferenze pubbliche
nel server (quindi meno critici ai fini privacy)? Sono dati pubblici nel server

> 

**C6. Server su cui gira**
Deathfist gira su un singolo server Discord (community personale) o è
pensato per essere aggiunto a più server da altri utenti/team? Cambia il modo
in cui scrivo la parte "a chi si applica questa policy". Girerà sul singolo server discord al momento installato

> 


