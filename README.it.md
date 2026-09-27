# SFTP Browser per iTerm2

🇬🇧 [English](README.md)

Pannello SFTP in stile **MobaXterm** dentro il Toolbelt di iTerm2. Usa la **stessa
connessione SSH** della sessione attiva: niente password o 2FA di nuovo.

Elenco con colonne ordinabili (nome, dimensione, data, proprietario,
gruppo, permessi) · albero delle cartelle · upload trascinando da Finder (anche cartelle) ·
download (anche ricorsivo) · **modifica e sincronizza** (doppio clic → editor locale →
ogni salvataggio viene caricato sul server) · rinomina, elimina, nuovo file/cartella,
chmod · **segui la cartella del terminale** · `cd` nel terminale · coda dei trasferimenti
con avanzamento e annullamento · tema chiaro/scuro.

## Come funziona

```
iTerm2 ──(API Python)──► sftp_browser (AutoLaunch)
  │  sessione attiva, tty, variabile "path"          │
  │                                                  ├─ trova il processo `ssh` sul tty
  │                                                  ├─ `ssh -G` → host, porta, ControlPath
  │                                                  ├─ `ssh -O check` → c'è un master?
  │                                                  └─ `ssh -o ControlMaster=no -s host sftp`
  │                                                        └─ client SFTP v3 asincrono (sftp.py)
  └─ Toolbelt ▸ SFTP (web view) ◄──HTTP/WebSocket── server locale 127.0.0.1:47823 (token)
```

Il client `ssh` del terminale non espone la sua connessione ad altri processi. L'unico
modo pulito per riusarla è il **multiplexing di OpenSSH** (ControlMaster): il pannello
apre un nuovo canale `sftp` sul socket del master, già autenticato.

## Installazione

Requisiti: macOS, iTerm2 3.5 o successivo, Python 3.9 o successivo (quello di Xcode Command Line Tools va bene).

```bash
git clone https://github.com/Scuottolinx/iterm2-sftp-plugin.git
cd iterm2-sftp-plugin
./install.sh
```

Per aggiornare: `git pull && ./install.sh`, poi riavvia lo script da iTerm2 ▸ Scripts.

1. iTerm2 ▸ Settings ▸ General ▸ Magic ▸ **Enable Python API**
2. Riavvia iTerm2 (lo script parte da solo da `Scripts/AutoLaunch`)
3. View ▸ **Show Toolbelt** ▸ spunta **SFTP**
4. In `~/.ssh/config` (poi ricollegati):

```
Host *
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
```

Senza ControlMaster il pannello lo segnala e offre di aprire una **nuova** connessione:
funziona solo con chiavi o agent, perché non può chiederti la password.

**Segui la cartella del terminale** funziona anche senza installare niente sul server.
Dopo ogni aggiornamento dello schermo (al massimo una volta al secondo) il plugin lancia,
sulla connessione già aperta, un piccolo script `sh` di sola lettura (`ps`, `readlink
/proc/<pid>/cwd`). Lo script trova la shell che sta sulla stessa connessione e ne legge la
cartella: funziona su Linux, anche vecchio (RHEL 5), e su BSD/macOS se c'è `lsof`. Se sul server c'è la
[Shell Integration](https://iterm2.com/documentation-shell-integration.html), il plugin
usa quella, che è immediata.

## Riconnessione con R (come MobaXterm)

Quando la connessione `ssh` di una scheda cade (Broken pipe, "closed by remote host",
timeout, connection refused…), il terminale mostra:

```
 ✖ Connessione a server persa  Premi R per riconnetterti (un altro tasto per ignorare)
```

Premi **R** per rilanciare lo stesso comando `ssh`. Qualsiasi altro tasto arriva alla shell come
sempre. Dopo un normale `exit`/`logout` non compare niente. Funziona su tutte le schede,
anche con il Toolbelt chiuso, e si può disattivare nelle impostazioni.

## `iterm-attach`: usare in iTerm2 una connessione aperta altrove

Utile quando un altro programma apre l'SSH in Terminal di macOS. Se quel programma usa il normale `ssh` e ControlMaster è attivo,
la connessione lascia un socket, e da lì si apre una scheda iTerm2 sulla stessa
connessione senza autenticarsi di nuovo:

```bash
iterm-attach        # sceglie la connessione (in automatico se ce n'è una sola)
iterm-attach web01  # quella il cui nome contiene "web01"
iterm-attach -l     # elenca soltanto
```

La nuova scheda è una shell separata sulla stessa connessione, non la stessa shell: non
vedi quello che c'è sullo schermo di Terminal. Il pannello SFTP e la riconnessione con R
funzionano anche lì. Se il programma usa un suo client SSH interno, non c'è nessuna
connessione OpenSSH da condividere.

## Proxy SSH che registrano le sessioni (PAM)

Alcuni proxy di accesso privilegiato **chiudono l'intera connessione** se ci si apre sopra un
secondo canale. Il pannello e `iterm-attach` li riconoscono (utenti UUID monouso, oppure gli
host indicati nelle impostazioni) e non aprono niente su quelle connessioni. Consiglio:
escludili da ControlMaster (`Host * !pam-proxy.example.com`).

## Impostazioni (⚙︎)

- cartella di download (predefinita `~/Downloads`) o "chiedi sempre"
- comando dell'editor: `open -t`, `open -a "Visual Studio Code"`, `open -a BBEdit`…
- azione del doppio clic: modifica e sincronizza / apri con l'app predefinita / scarica

Scorciatoie: ↑↓ Invio · ⌫ cartella superiore · ⌘⌫ elimina · F2 rinomina · F5 aggiorna ·
⌘A seleziona tutto · ⌘[ ⌘] indietro/avanti · digita per saltare a un nome.

## Limiti noti

- Si riusano solo le connessioni aperte **dopo** aver attivato ControlMaster.
- Il file system è quello dell'utente SSH: se nel terminale hai fatto `sudo su`,
  l'SFTP resta con l'utente di login.
- `ssh` dentro tmux/screen non viene rilevato: il tty della sessione è quello del client tmux.
- ControlPath deve stare sotto ~104 caratteri (limite dei socket Unix): `~/.ssh/cm-%C` va bene.
- La modifica sincronizzata sovrascrive il file remoto a ogni salvataggio (come MobaXterm).

## Sviluppo

```bash
python3 tests/test_sftp.py          # client SFTP contro /usr/libexec/sftp-server
python3 tests/test_reconnect.py     # caduta di connessione vs logout normale
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app/main.py --demo # UI senza iTerm2, sfoglia questo Mac
```

Log: `~/Library/Application Support/iTerm2/SFTPBrowser/plugin.log` oppure iTerm2 ▸ Scripts ▸ Manage ▸ Console. Disinstallazione: `./uninstall.sh`.

## Licenza

[MIT](LICENSE). Le dipendenze (installate da pip, non incluse) hanno le loro licenze: iterm2 (GPLv2), aiohttp (Apache-2.0), psutil (BSD-3-Clause).

## Sicurezza

Il server ascolta solo su `127.0.0.1`, richiede un token casuale (salvato in
`~/Library/Application Support/iTerm2/SFTPBrowser/token`, permessi 600) e rifiuta
Host diversi da `127.0.0.1`/`localhost` (protezione dal DNS rebinding), così le pagine
web aperte nel browser non possono pilotarlo.
